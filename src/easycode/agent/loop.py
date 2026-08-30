"""The agentic loop: stream, execute tools, feed results back, repeat."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from easycode.agent import builtin_tools
from easycode.agent.builtin_tools import (
    BUILTIN_TOOLS,
    DEFAULT_MAX_PARALLEL,
    PARALLEL_TASKS_SCHEMA,
    TASK_SCHEMA,
    USE_SKILL_SCHEMA,
)
from easycode.agent.compaction import (
    COMPACTION_DEFAULTS,
    PRUNE_MINIMUM,
    PRUNE_PROTECT,
    PRUNED_OUTPUT,
    PROTECTED_TOOL_OUTPUTS,
    BudgetExceededError,
    Compactor,
)
from easycode.agent.context import History
from easycode.agent.rollback import TurnRollback
from easycode.agent.summarizer import Summarizer
from easycode.agent.system import build_system_prompt, find_agents_rules
from easycode.approval import (
    PERM_ASK,
    PERM_AUTO_REVIEW,
    approval_reason,
    definitive_deny_reason,
    grant_for_toolcall,
    needs_approval,
)
from easycode.models.base import Provider, StreamEvent, ToolCall
from easycode.policy import (
    APPROVAL_NEVER,
    REVIEWER_AUTO,
    ExecutionPolicy,
    permission_rule_action,
)
from easycode.reviewer import ReviewDecision
from easycode.tools.registry import ToolRegistry
from easycode.workspace import PathContext, ToolGrant

MAX_TOOL_ITERATIONS = 12

#: How long a cancellation/abort waits for in-flight ``to_thread`` tool futures
#: to finish before rolling back, so a late file write lands before the snapshot
#: pre-state is restored (late-write barrier).
TOOL_DRAIN_TIMEOUT = 10.0


@dataclass
class AgentEvent:
    """Event yielded to the UI as a turn progresses."""

    kind: str  # "text" | "tool_start" | "tool_result" | "error" | "done" | "cancelled"
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: str | None = None
    error: str | None = None


@dataclass
class Agent:
    provider: Provider
    registry: ToolRegistry
    root: Path
    history: History = field(default_factory=History)
    enabled_tools: set[str] | None = None
    hook: Callable[[str, ToolCall, str], None] | None = None
    summarizer: Summarizer | None = None
    subagent_factory: Callable[[str], "Agent"] | None = None
    include_parallel_tool: bool = True
    secondary_roots: list[Path] = field(default_factory=list)
    extra_safe_dirs: list[Path] = field(default_factory=list)
    permission_mode: str = PERM_ASK
    permission_rules: dict[str, Any] = field(default_factory=dict)
    approval_handler: Callable[[ToolCall], Awaitable[bool | ToolGrant | None]] | None = None
    review_handler: Callable[[ToolCall, str, list[dict]], Awaitable[ReviewDecision | bool]] | None = None
    _review_items: list[dict] = field(default_factory=list)
    _review_decisions: list[bool] = field(default_factory=list)
    _consecutive_review_denials: int = 0
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    mcp_manager: "MCPSessionManager | None" = None
    include_mcp_tools: bool = True
    max_context_tokens: int = 32_000
    compaction: dict[str, Any] = field(default_factory=dict)
    model_limits: dict[str, int] | None = None
    snapshot_manager: "FileSnapshotManager | None" = None
    _redo_stack: list[list[dict]] = field(default_factory=list)
    agents: "AgentRegistry | None" = None
    skills: "SkillRegistry | None" = None
    system_override: str | None = None
    # in-flight tool threads + whether a shell ran this turn, so a
    # cancellation can drain them (late-write barrier) and report the un-doable
    # shell side effects instead of pretending the turn was cleanly rolled back.
    _pending_tool_tasks: list[tuple[str, asyncio.Future]] = field(default_factory=list)
    _shell_tools_ran: bool = False
    _last_cancel_note: str | None = None
    # Names of tracked tools that were still in-flight when the drain
    # window timed out, so cancellation can report that their writes may land
    # *after* the snapshot rollback (not just shell side effects).
    _undrained_tools: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.compaction = {**COMPACTION_DEFAULTS, **(self.compaction or {})}
        self._compactor = Compactor(self)
        self._rollback = TurnRollback(self)
        self.history.max_tokens = self._usable_tokens()
        self.history.set_system(self._build_system())

    def _build_system(self) -> str:
        rules = find_agents_rules(self.root)
        skills_desc = "\n".join(self.skills.desc_lines()) if self.skills else ""
        agents_desc = "\n".join(self.agents.desc_lines()) if self.agents else ""
        base = build_system_prompt(
            self.root, self._tools_desc(), rules, skills_desc=skills_desc, agents_desc=agents_desc
        )
        if self.system_override:
            base = f"{self.system_override.strip()}\n\n{base}"
        return base

    async def init_mcp(self) -> None:
        """Connect configured MCP servers on first use; safe to call repeatedly."""
        if not self.mcp_servers or self.mcp_manager is not None:
            return
        from easycode.mcp import MCPSessionManager

        self.mcp_manager = MCPSessionManager(self.mcp_servers, self.path_context())
        await self.mcp_manager.start()
        if self.mcp_manager.tool_schemas():
            self.history.set_system(self._build_system())

    def path_context(self) -> PathContext:
        """Sandbox context for this agent: primary + secondary roots + safe dirs."""
        return PathContext(
            primary=self.root,
            secondary=[Path(p) for p in self.secondary_roots],
            extra_safe_dirs=[Path(p) for p in self.extra_safe_dirs],
            sandbox_mode=self.execution_policy.sandbox_mode,
        )

    @property
    def execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy.from_preset(self.permission_mode)

    def _permission_rule(self, tc: ToolCall) -> str | None:
        """Resolve configured rules against the command/path/tool identity."""
        if tc.name == "execute_shell":
            value = tc.arguments.get("command", "")
        elif tc.name in {"read_file", "write_file", "edit_file", "glob", "grep"}:
            value = tc.arguments.get("path", tc.arguments.get("pattern", ""))
        else:
            value = tc.name
        return permission_rule_action(self.permission_rules, tc.name, str(value))

    def _tools_desc(self) -> str:
        lines = []
        for schema in self.tool_schemas():
            fn = schema["function"]
            lines.append(f"- {fn['name']}: {fn['description']}")
        return "\n".join(lines)

    def tool_schemas(self) -> list[dict]:
        schemas = self.registry.schemas(self.enabled_tools)
        if self.include_parallel_tool and (self.enabled_tools is None or "parallel_tasks" in self.enabled_tools):
            schemas = [*schemas, PARALLEL_TASKS_SCHEMA]
        if self.agents is not None and self.agents.names():
            schemas = [*schemas, TASK_SCHEMA]
        if self.skills is not None and self.skills.names():
            schemas = [*schemas, USE_SKILL_SCHEMA]
        if self.mcp_manager and self.include_mcp_tools:
            schemas = [*schemas, *self.mcp_manager.tool_schemas()]
        return schemas

    def _tool_schema_tokens(self) -> int:
        """Estimated token cost of the tool schemas sent on every completion.

        The budget accounts for these so a large tool set (many MCP tools, a
        bloated schema) trips compaction instead of silently exceeding the
        provider window.
        """
        schemas = self.tool_schemas()
        if not schemas:
            return 0
        return self.history.estimate_text_tokens(json.dumps(schemas, ensure_ascii=False))

    def message_payload(self) -> list[dict]:
        return self.history.payload()

    def _usable_tokens(self) -> int:
        """Usable context budget = model window − reserved output buffer (see Compactor)."""
        return self._compactor._usable_tokens()

    def _preserve_recent_tokens(self) -> int:
        return self._compactor._preserve_recent_tokens()

    async def _summarize(self, messages: list[dict]) -> str | None:
        return await self._compactor._summarize(messages)

    def _prune_tool_outputs(self) -> None:
        """Clear the outputs of old completed tool calls to free context (opencode prune)."""
        self._compactor._prune_tool_outputs()

    async def _condense_if_over_budget(self) -> None:
        """Compact history when over budget (honors ``compaction.auto``)."""
        await self._compactor._condense_if_over_budget()

    def _raise_if_over_budget(self) -> None:
        """Final budget gate (invariant #5): fail before the provider call."""
        self._compactor._raise_if_over_budget()

    async def respond(
        self, user_input: str, max_iterations: int = MAX_TOOL_ITERATIONS
    ) -> AsyncIterator[AgentEvent]:
        """Run one user request through the tool-use loop, yielding UI events."""
        await self.init_mcp()
        if self.snapshot_manager:
            self.snapshot_manager.begin_turn()
        self._redo_stack.clear()  # a new turn invalidates any pending redo
        start_len = len(self.history.messages)
        self.history.add_user(user_input)
        self._review_items = []
        self._review_decisions = []
        self._consecutive_review_denials = 0
        # per-turn tool tracking must start clean (a prior abort may have
        # left drained futures in the executor, but nothing pending here).
        self._pending_tool_tasks = []
        self._shell_tools_ran = False
        self._last_cancel_note = None
        self._undrained_tools = set()
        try:
            async for ev in self._turn(user_input, max_iterations):
                yield ev
        except (asyncio.CancelledError, KeyboardInterrupt):
            # roll back this turn's partial messages so history stays valid, and
            # restore file pre-state after draining any in-flight tool thread.
            restored = await self._abort_turn(start_len)
            note = self._residual_risk_note()
            self._last_cancel_note = note
            try:
                if note:
                    yield AgentEvent(kind="error", error=note)
                yield AgentEvent(kind="cancelled")
            except BaseException:
                pass
            raise
        except Exception as exc:  # noqa: BLE001 - any non-cancel failure must not leave half state
            # A provider (or any loop step) that raises mid-turn after yielding
            # tool_calls would otherwise leave [user] in history and a dangling
            # snapshot record. Roll back the same way cancellation does, surface
            # the error to the UI, then re-raise so the caller can react.
            restored = await self._abort_turn(start_len)
            note = self._residual_risk_note()
            self._last_cancel_note = note
            try:
                yield AgentEvent(
                    kind="error",
                    error=f"{type(exc).__name__}: {exc}"
                    + (f"\n{note}" if note else ""),
                )
            except BaseException:
                pass
            raise

    async def _abort_turn(self, start_len: int) -> list[str]:
        """Abandon the current turn: drain in-flight tools, roll back files.

        Waits (bounded) for any ``to_thread`` tool futures so a late write lands
        before the snapshot pre-state is restored, then restores the pre-state
        (``rollback_turn``) and drops the turn's partial history. Returns the
        restored paths (best-effort).
        """
        await self._drain_pending_tools()
        restored: list[str] = []
        if self.snapshot_manager:
            try:
                restored = self.snapshot_manager.rollback_turn()
            except Exception:  # noqa: BLE001 - best-effort rollback must not mask the abort
                restored = []
        del self.history.messages[start_len:]
        return restored

    async def _drain_pending_tools(self) -> None:
        """Wait (bounded) for in-flight sync tool threads ahead of rollback.

        ``asyncio.to_thread`` / ``run_in_executor`` runs the tool in a worker
        thread that cannot be cancelled; a cancellation that returns before the
        thread wrote the file would let that write land *after* the snapshot
        restore, re-dirtying the file. Draining here turns that race into a
        deterministic order: thread finishes → write lands → pre-state restored.
        """
        if not self._pending_tool_tasks:
            return
        # Snapshot before waiting: the time-out cancellation below cancels the
        # child futures, and each cancelled future's done callback empties
        # ``_pending_tool_tasks``. So "unfinished" must be decided from a copy
        # taken before we start waiting.
        snapshot = list(self._pending_tool_tasks)
        pending = [f for _, f in snapshot]
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=TOOL_DRAIN_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # A tool that outlives the drain is dropped, not rolled back; the
            # caller still restores the snapshot and reports the residual risk.
            # Record tracked tools that are still in-flight (their future
            # was cancelled by the timeout, or is not done) so the residual note
            # covers non-shell tools whose write may land after the snapshot
            # restore (not just shell side effects).
            self._undrained_tools.update(
                name for name, f in snapshot if f.cancelled() or not f.done()
            )
        self._pending_tool_tasks.clear()

    def _residual_risk_note(self) -> str | None:
        """Explain cancellation residue that was NOT peacefully rolled back.

        Two sources: (1) an executed shell command whose file effects cannot be
        undone, and (2) tracked tools that were still in-flight when the
        drain window timed out, so their writes may land *after* the snapshot
        restore. Either one means the turn was not cleanly undone.
        """
        parts: list[str] = []
        if self._shell_tools_ran:
            parts.append(
                "cancelled: 本回合已执行 shell 命令，其文件副作用无法自动回滚；"
                "请手工核对受影响文件 (shell side-effects cannot be rolled back; "
                "files written by the shell are left as-is)."
            )
        if self._undrained_tools:
            names = ", ".join(sorted(self._undrained_tools))
            parts.append(
                f"cancelled: 工具 {names} 未在取消窗口内完成，"
                f"其写入可能稍后落地 (tool {names} did not finish within the cancel "
                "window; any file write it made may still land after the rollback)."
            )
        return "\n".join(parts) if parts else None

    async def _turn(self, user_input: str, max_iterations: int) -> AsyncIterator[AgentEvent]:
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            await self._condense_if_over_budget()
            self._raise_if_over_budget()
            schemas = self.tool_schemas()
            final_text: list[str] = []
            tool_calls: list[ToolCall] | None = None
            error: str | None = None

            async for ev in self.provider.stream(self.history.payload(), schemas):
                if ev.kind == "text" and ev.content:
                    final_text.append(ev.content)
                    yield AgentEvent(kind="text", content=ev.content)
                elif ev.kind == "tool_calls":
                    tool_calls = ev.tool_calls
                elif ev.kind == "error":
                    error = ev.error

            if error:
                yield AgentEvent(kind="error", error=error)
                self.history.add_assistant("".join(final_text))
                break

            if not tool_calls:
                text = "".join(final_text)
                self.history.add_assistant(text)
                if not text.strip():
                    yield AgentEvent(
                        kind="error",
                        error="模型没有返回任何内容（工具可能已执行，请重试或换个说法）",
                    )
                if self.permission_mode == PERM_AUTO_REVIEW and self._review_items:
                    yield AgentEvent(
                        kind="review",
                        content=json.dumps({"changes": self._review_items}, ensure_ascii=False),
                    )
                yield AgentEvent(kind="done")
                break

            assistant_msg: dict = {
                "role": "assistant",
                "content": "".join(final_text) or "",
                "tool_calls": [tc.to_message_content() for tc in tool_calls],
            }
            self.history.add(assistant_msg)

            for tc in tool_calls:
                ctx = self.path_context()
                if self.snapshot_manager:
                    self.snapshot_manager.note_tool(tc.name, tc.arguments, ctx)
                policy = self.execution_policy
                grant: ToolGrant | None = None
                # SC: a clearly destructive shell command is denied outright and
                # can never be lifted by a missing approval handler or an
                # auto-reviewer; approval/review is skipped entirely for it.
                deny_reason = definitive_deny_reason(tc, ctx)
                rule_action = self._permission_rule(tc)
                requires_approval = False
                if deny_reason is None and rule_action == "deny":
                    deny_reason = f"权限规则拒绝工具调用: {tc.name}"
                if deny_reason is None:
                    requires_approval = policy.approval_policy != APPROVAL_NEVER and (
                        needs_approval(tc, ctx, self.permission_mode)
                        or bool(self.mcp_manager and self.mcp_manager.requires_approval(tc.name))
                    )
                    if rule_action == "ask":
                        requires_approval = True
                    elif rule_action == "allow":
                        requires_approval = False
                        grant = grant_for_toolcall(tc, ctx)
                approved = True
                denial_reason = "rejected by user"
                breaker_tripped = False
                if deny_reason is not None:
                    approved = False
                    denial_reason = deny_reason
                elif requires_approval:
                    reason = (
                        self.mcp_manager.approval_reason(tc.name)
                        if self.mcp_manager and self.mcp_manager.requires_approval(tc.name)
                        else approval_reason(tc, ctx)
                    )
                    if policy.approvals_reviewer == REVIEWER_AUTO:
                        decision = await self._auto_review(tc, reason)
                        approved = decision.approve
                        denial_reason = decision.rationale
                        breaker_tripped = self._record_review_decision(approved)
                        yield AgentEvent(
                            kind="review",
                            tool_call=tc,
                            content=json.dumps(
                                {
                                    "phase": "pre-execution",
                                    "approved": decision.approve,
                                    "rationale": decision.rationale,
                                    "tool": tc.name,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    else:
                        yield AgentEvent(kind="approval", tool_call=tc, content=reason)
                        decision = await self.approval_handler(tc) if self.approval_handler else False
                        approved, grant = self._apply_approval_decision(decision, tc, ctx)
                yield AgentEvent(kind="tool_start", tool_call=tc)
                if not approved:
                    result = json.dumps(
                        {
                            "status": "error",
                            "message": f"tool call rejected: {tc.name}: {denial_reason}",
                            "rejected": True,
                            "category": "policy" if rule_action == "deny" else ("destructive" if deny_reason is not None else "approval"),
                            "reason": denial_reason,
                        },
                        ensure_ascii=False,
                    )
                elif requires_approval:
                    if grant is None:
                        grant = grant_for_toolcall(tc, ctx)
                    result = await self._dispatch_tool(tc, grant=grant)
                else:
                    result = await self._dispatch_tool(tc, grant=grant)
                if self.hook:
                    self.hook(tc.name, tc, result)
                if self.permission_mode == PERM_AUTO_REVIEW:
                    self._collect_review(tc, result)
                self.history.add_tool(tc.id, tc.name, self._model_view(tc.name, result))
                yield AgentEvent(kind="tool_result", tool_call=tc, tool_result=result)
                if breaker_tripped:
                    yield AgentEvent(
                        kind="error",
                        error="automatic review denial limit reached; turn interrupted",
                    )
                    yield AgentEvent(kind="done")
                    return
        else:
            yield AgentEvent(kind="error", error=f"hit max tool iterations ({max_iterations})")
            yield AgentEvent(kind="done")

    def _collect_review(self, tc: ToolCall, result: str) -> None:
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return
        if data.get("status") != "ok":
            return
        path = data.get("path")
        diff = data.get("diff")
        if path:
            self._review_items.append({"tool": tc.name, "path": path, "diff": diff or ""})

    def _apply_approval_decision(
        self, decision: bool | ToolGrant | None, tc: ToolCall, ctx: PathContext
    ) -> tuple[bool, ToolGrant | None]:
        """Normalise an approval handler result into (approved, grant).

        ``True``/``False``/``None`` keep the legacy boolean contract (the precise
        grant is then derived from the tool call by the caller); a :class:`ToolGrant`
        is passed through verbatim so an approval can convey exactly which
        capability/writable roots it grants (P0-1).
        """
        if decision is None or decision is False:
            return False, None
        if decision is True:
            return True, None
        return True, decision

    async def _auto_review(self, tc: ToolCall, reason: str) -> ReviewDecision:
        if self.review_handler is None:
            return ReviewDecision(False, "automatic reviewer is not configured")
        try:
            result = await self.review_handler(tc, reason, list(self.history.messages))
            if isinstance(result, ReviewDecision):
                return result
            return ReviewDecision(bool(result), "automatic reviewer decision")
        except Exception as exc:  # noqa: BLE001 - fail closed at the boundary
            return ReviewDecision(False, f"automatic review failed: {type(exc).__name__}: {exc}")

    def _record_review_decision(self, approved: bool) -> bool:
        self._review_decisions.append(approved)
        self._review_decisions = self._review_decisions[-50:]
        if approved:
            self._consecutive_review_denials = 0
        else:
            self._consecutive_review_denials += 1
        return self._consecutive_review_denials >= 3 or self._review_decisions.count(False) >= 10

    def _model_view(self, name: str, result: str) -> str:
        """Compact model-facing view of a tool result.

        The full result (including the diff) still streams to the UI and the
        review/hook channels, but the model does not need to re-read the diff
        it just produced — align with opencode where edit/write return a short
        confirmation and keep the diff out of the LLM-visible output.
        """
        if name not in ("write_file", "edit_file"):
            return result
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return result
        if not isinstance(data, dict) or data.get("status") != "ok":
            return result
        compact: dict[str, Any] = {"status": "ok", "path": data.get("path")}
        if data.get("dry_run"):
            compact["dry_run"] = True
            compact["message"] = "preview only, file unchanged"
        return json.dumps(compact, ensure_ascii=False)

    async def _dispatch_tool(self, tc: ToolCall, force_allowed: bool = False, grant: ToolGrant | None = None) -> str:
        # record that a shell ran this turn so cancellation can report the
        # residual risk (its file side effects cannot be reliably rolled back).
        if tc.name == "execute_shell":
            self._shell_tools_ran = True
        if self.mcp_manager and self.mcp_manager.has_tool(tc.name):
            return await self.mcp_manager.call(tc.name, tc.arguments)
        if tc.name in BUILTIN_TOOLS:
            return await self._run_builtin(tc)
        return await self._run_tool(tc, force_allowed=force_allowed, grant=grant)

    async def _run_builtin(self, tc: ToolCall) -> str:
        return await builtin_tools.run_builtin(self, tc)

    async def _run_task(self, tc: ToolCall) -> str:
        return await builtin_tools.run_task(self, tc)

    async def _run_use_skill(self, tc: ToolCall) -> str:
        return await builtin_tools.run_use_skill(self, tc)

    async def _run_parallel(self, tasks: list[dict], max_parallel: int) -> str:
        return await builtin_tools.run_parallel(self, tasks, max_parallel)

    def _make_subagent(self, spec: "AgentSpec | None" = None) -> "Agent":
        """Build a subagent; ``spec`` (task tool) overrides model/system/tools/permission."""
        return builtin_tools.make_subagent(self, spec)

    async def run_task(self, prompt: str, max_iterations: int = MAX_TOOL_ITERATIONS) -> str:
        """Run a standalone subtask with a fresh history; return the final text.

        Raises RuntimeError when the provider reports an error mid-turn.
        """
        parts: list[str] = []
        errors: list[str] = []
        async for ev in self.respond(prompt, max_iterations=max_iterations):
            if ev.kind == "text" and ev.content:
                parts.append(ev.content)
            elif ev.kind == "error" and ev.error:
                errors.append(ev.error)
        if errors:
            raise RuntimeError("; ".join(errors))
        return "".join(parts)

    # --- undo / redo (session rollback) -------------------------------------

    def undo_available(self) -> bool:
        return self._rollback.undo_available()

    def redo_available(self) -> bool:
        return self._rollback.redo_available()

    def undo_turn(self) -> dict:
        """Undo the last user turn: drop its messages and restore files.

        Returns a summary dict; raises RuntimeError when there is nothing to undo.
        """
        return self._rollback.undo_turn()

    def redo_turn(self) -> dict:
        """Redo the last undone turn: re-append its messages and reapply files."""
        return self._rollback.redo_turn()

    def undo_to_user(self, nth: int) -> dict:
        """Undo everything back to just before the ``nth`` user message (1-based).

        The ``nth`` prompt and everything after it are removed and their file
        changes rolled back. Pending redo state is discarded (a batch undo
        cannot be re-applied in one step). Raises RuntimeError when ``nth``
        is out of range.
        """
        return self._rollback.undo_to_user(nth)

    async def _run_tool(self, tc: ToolCall, force_allowed: bool = False, grant: ToolGrant | None = None) -> str:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            None,
            self.registry.execute,
            tc.name,
            tc.arguments,
            self.root,
            self.path_context(),
            force_allowed,
            grant,
        )
        # track the in-flight thread so a cancellation can drain it before
        # rolling back the snapshot (late-write barrier). The done callback
        # removes it once the thread actually finishes.
        entry = (tc.name, fut)
        self._pending_tool_tasks.append(entry)
        fut.add_done_callback(lambda _f: self._discard_pending(entry))
        try:
            return await fut
        except asyncio.CancelledError:
            # We abandon the in-flight thread without waiting; it keeps
            # running and any file write it makes can land *after* the snapshot
            # rollback. Cancelling the await also cancels ``fut`` (whose done
            # callback removes the entry from ``_pending_tool_tasks``), so the
            # drain can no longer see it — record it here so the residual note
            # warns about the late write instead of staying silent.
            self._undrained_tools.add(tc.name)
            raise
        except Exception as exc:  # noqa: BLE001 - report tool failures to the model
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})

    def _discard_pending(self, entry: tuple[str, asyncio.Future]) -> None:
        try:
            self._pending_tool_tasks.remove(entry)
        except ValueError:
            pass
