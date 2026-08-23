"""The agentic loop: stream, execute tools, feed results back, repeat."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from easycode.agent.context import History
from easycode.agent.summarizer import LLMSummarizer, Summarizer
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
    cap_permission,
    permission_rule_action,
)
from easycode.reviewer import ReviewDecision
from easycode.tools.registry import ToolRegistry
from easycode.workspace import PathContext, ToolGrant

MAX_TOOL_ITERATIONS = 12
DEFAULT_MAX_PARALLEL = 4

#: How long a cancellation/abort waits for in-flight ``to_thread`` tool futures
#: to finish before rolling back, so a late file write lands before the snapshot
#: pre-state is restored (late-write barrier, MS-6).
TOOL_DRAIN_TIMEOUT = 10.0

#: Context-compaction defaults (aligned with opencode `compaction` config).
COMPACTION_DEFAULTS: dict[str, Any] = {
    "auto": True,
    "buffer": 20_000,
    "preserve_recent_tokens": None,
    "tail_turns": None,
    "prune": True,
    "summary_max_chars": 8_000,
}

#: clear old tool outputs once >PRUNE_PROTECT tokens accumulate (min PRUNE_MINIMUM freed)
PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
PRUNED_OUTPUT = "[Tool output cleared]"
PROTECTED_TOOL_OUTPUTS = {"use_skill", "task"}

PARALLEL_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "parallel_tasks",
        "description": (
            "Run independent subtasks concurrently using sub-agents, then return "
            "their results. Use for tasks that do not depend on each other (e.g. "
            "read & analyze several files, draft several small functions). "
            "Each task runs with its own fresh context sharing the same tools and workspace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "short task label"},
                            "prompt": {"type": "string", "description": "self-contained task instructions"},
                        },
                        "required": ["name", "prompt"],
                    },
                },
                "max_parallel": {"type": "integer", "default": DEFAULT_MAX_PARALLEL, "minimum": 1, "maximum": 6},
            },
            "required": ["tasks"],
        },
    },
}

TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Delegate one job to a named subagent (see 'Delegatable agents' in "
            "the system prompt). The subagent runs with its own fresh context, "
            "its own system prompt, and possibly its own model and tool set; "
            "only its final answer comes back. Use when the job matches an "
            "agent's description; prefer parallel_tasks for several independent "
            "generic subtasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "name of the agent to delegate to"},
                "prompt": {"type": "string", "description": "self-contained task instructions"},
            },
            "required": ["agent", "prompt"],
        },
    },
}

USE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": (
            "Load a skill (see 'Available skills' in the system prompt) into the "
            "conversation by name. The skill body is injected once and stays in "
            "context for the rest of the session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "skill name to load"},
            },
            "required": ["name"],
        },
    },
}

BUILTIN_TOOLS = {"parallel_tasks", "task", "use_skill"}


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
    # MS-6: in-flight tool threads + whether a shell ran this turn, so a
    # cancellation can drain them (late-write barrier) and report the un-doable
    # shell side effects instead of pretending the turn was cleanly rolled back.
    _pending_tool_tasks: list[tuple[str, asyncio.Future]] = field(default_factory=list)
    _shell_tools_ran: bool = False
    _last_cancel_note: str | None = None

    def __post_init__(self) -> None:
        self.compaction = {**COMPACTION_DEFAULTS, **(self.compaction or {})}
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
        provider window (MS-8).
        """
        schemas = self.tool_schemas()
        if not schemas:
            return 0
        return self.history.estimate_text_tokens(json.dumps(schemas, ensure_ascii=False))

    def message_payload(self) -> list[dict]:
        return self.history.payload()

    def _usable_tokens(self) -> int:
        """Usable context budget = model window − reserved output buffer.

        When the model's limits are unknown (fallback), the whole
        ``max_context_tokens`` is usable — there is no output figure to reserve.
        """
        limits = self.model_limits
        if not limits:
            return self.max_context_tokens
        context = limits["context"]
        output = limits["output"] or 0
        buffer = int(self.compaction.get("buffer") or 0)
        reserved = min(buffer, output) if output else buffer
        return max(0, context - reserved)

    def _preserve_recent_tokens(self) -> int:
        explicit = self.compaction.get("preserve_recent_tokens")
        if explicit is not None:
            return int(explicit)
        return max(2_000, min(15_000, int(self._usable_tokens() * 0.25)))

    async def _summarize(self, messages: list[dict]) -> str | None:
        if self.summarizer is None:
            return None
        if isinstance(self.summarizer, LLMSummarizer):
            return await self.summarizer.summarize(messages, previous_summary=self.history.summary)
        return await self.summarizer(messages)

    def _prune_tool_outputs(self) -> None:
        """Clear the outputs of old completed tool calls to free context (opencode prune).

        Protects the most recent two user turns, existing summaries, and the
        ``use_skill``/``task`` tools; only clears once there are at least
        ``PRUNE_PROTECT`` tokens of older tool output and the freed amount
        exceeds ``PRUNE_MINIMUM``.
        """
        if not self.compaction.get("prune", True):
            return
        turns = 0
        total = 0
        pruned = 0
        to_clear: list[dict] = []
        for m in reversed(self.history.messages):
            role = m.get("role")
            if role == "user":
                turns += 1
            if turns < 2:
                continue
            if self.history.is_summary(m):
                break
            if role != "tool":
                continue
            if m.get("name") in PROTECTED_TOOL_OUTPUTS:
                continue
            content = str(m.get("content") or "")
            if not content or content == PRUNED_OUTPUT:
                continue
            size = self.history.estimate_messages_tokens([m])
            total += size
            if total <= PRUNE_PROTECT:
                continue
            pruned += size
            to_clear.append(m)
        if pruned > PRUNE_MINIMUM:
            for m in to_clear:
                m["content"] = PRUNED_OUTPUT

    async def _condense_if_over_budget(self) -> None:
        """Compact history when over budget (MS-3: honors ``compaction.auto``).

        When ``auto`` is False the user opted out of automatic compaction, so
        nothing is summarized or trimmed here even when the budget is exceeded.
        ``extra`` is the token cost of the tool schemas sent on every call, so a
        large tool set also counts toward the budget (MS-8).
        """
        if not self.compaction.get("auto", True):
            return
        extra = self._tool_schema_tokens()
        if not self.history.over_budget(extra):
            return
        self._prune_tool_outputs()
        if not self.history.over_budget(extra):
            return
        tail_start = self.history.select_tail_start(
            self._preserve_recent_tokens(), self.compaction.get("tail_turns")
        )
        if tail_start is None or self.summarizer is None:
            self.history.trim()
            return
        summary = await self._summarize(self.history.messages[:tail_start])
        if summary is None or not self.history.condense_from(summary, tail_start):
            self.history.trim()

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
        # MS-6: per-turn tool tracking must start clean (a prior abort may have
        # left drained futures in the executor, but nothing pending here).
        self._pending_tool_tasks = []
        self._shell_tools_ran = False
        self._last_cancel_note = None
        try:
            async for ev in self._turn(user_input, max_iterations):
                yield ev
        except (asyncio.CancelledError, KeyboardInterrupt):
            # roll back this turn's partial messages so history stays valid, and
            # restore file pre-state (MS-6) after draining any in-flight tool thread.
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
        except Exception as exc:  # noqa: BLE001 - MS-9: any non-cancel failure must not leave half state
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
        restored paths (best-effort). MS-6.
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
        pending = [f for _, f in self._pending_tool_tasks]
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=TOOL_DRAIN_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # A tool that outlives the drain is dropped, not rolled back; the
            # caller still restores the snapshot and reports the residual risk.
            pass
        self._pending_tool_tasks.clear()

    def _residual_risk_note(self) -> str | None:
        """Explain that an executed shell command's file effects were NOT undone."""
        if not self._shell_tools_ran:
            return None
        return (
            "cancelled: 本回合已执行 shell 命令，其文件副作用无法自动回滚；"
            "请手工核对受影响文件 (shell side-effects cannot be rolled back; "
            "files written by the shell are left as-is)."
        )

    async def _turn(self, user_input: str, max_iterations: int) -> AsyncIterator[AgentEvent]:
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            await self._condense_if_over_budget()
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
        # MS-6: record that a shell ran this turn so cancellation can report the
        # residual risk (its file side effects cannot be reliably rolled back).
        if tc.name == "execute_shell":
            self._shell_tools_ran = True
        if self.mcp_manager and self.mcp_manager.has_tool(tc.name):
            return await self.mcp_manager.call(tc.name, tc.arguments)
        if tc.name in BUILTIN_TOOLS:
            return await self._run_builtin(tc)
        return await self._run_tool(tc, force_allowed=force_allowed, grant=grant)

    async def _run_builtin(self, tc: ToolCall) -> str:
        if tc.name == "parallel_tasks":
            try:
                sub_tasks = tc.arguments.get("tasks", [])
                max_parallel = int(tc.arguments.get("max_parallel", DEFAULT_MAX_PARALLEL))
                return await self._run_parallel(sub_tasks, max_parallel)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
        if tc.name == "task":
            return await self._run_task(tc)
        if tc.name == "use_skill":
            return await self._run_use_skill(tc)
        return json.dumps({"status": "error", "message": f"unknown builtin tool: {tc.name}"})

    async def _run_task(self, tc: ToolCall) -> str:
        name = str(tc.arguments.get("agent", "")).strip().lower()
        prompt = str(tc.arguments.get("prompt", "")).strip()
        if not self.agents or not name:
            return json.dumps({"status": "error", "message": "unknown agent or no agents configured"})
        spec = self.agents.get(name)
        if spec is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"unknown agent: {name}",
                    "known_agents": self.agents.names(),
                },
                ensure_ascii=False,
            )
        if not prompt:
            return json.dumps({"status": "error", "message": "task prompt is empty"})
        try:
            sub = self._make_subagent(spec)
            text = await sub.run_task(prompt)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
        return json.dumps(
            {"status": "ok", "agent": spec.name, "model": spec.model, "result": text},
            ensure_ascii=False,
        )

    async def _run_use_skill(self, tc: ToolCall) -> str:
        name = str(tc.arguments.get("name", "")).strip().lower()
        if not self.skills:
            return json.dumps({"status": "error", "message": "no skills configured"})
        skill = self.skills.get(name)
        if skill is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"unknown skill: {name}",
                    "known_skills": self.skills.names(),
                },
                ensure_ascii=False,
            )
        # inject the body into the conversation once (stays for the session)
        self.history.add({"role": "system", "content": f"[skill: {skill.name}]\n{skill.body}"})
        return json.dumps(
            {"status": "ok", "skill": skill.name, "loaded": True},
            ensure_ascii=False,
        )

    async def _run_parallel(self, tasks: list[dict], max_parallel: int) -> str:
        sem = asyncio.Semaphore(min(max_parallel, 6) or 1)

        async def run_one(task: dict) -> dict:
            async with sem:
                name = task.get("name", "task")
                prompt = task.get("prompt", "")
                try:
                    sub = self._make_subagent()
                    text = await sub.run_task(prompt)
                    return {"name": name, "result": text}
                except Exception as exc:  # noqa: BLE001
                    return {"name": name, "error": f"{type(exc).__name__}: {exc}"}

        results = await asyncio.gather(*(run_one(t) for t in tasks))
        return json.dumps({"status": "ok", "count": len(results), "results": results}, ensure_ascii=False)

    def _make_subagent(self, spec: "AgentSpec | None" = None) -> "Agent":
        """Build a subagent; ``spec`` (task tool) overrides model/system/tools/permission."""
        model = None
        if spec is not None and spec.model:
            model = spec.model
        if self.subagent_factory:
            sub = self.subagent_factory(model or self.provider.model)
        else:
            from easycode.models.litellm_provider import LiteLLMProvider

            if model and hasattr(self.provider, "kwargs"):
                provider = LiteLLMProvider(model, **self.provider.kwargs)
            else:
                provider = LiteLLMProvider(model or self.provider.model)
            sub = Agent(
                provider=provider,
                registry=self.registry,
                root=self.root,
                enabled_tools=self.enabled_tools,
                secondary_roots=list(self.secondary_roots),
                extra_safe_dirs=list(self.extra_safe_dirs),
                permission_mode=self.permission_mode,
                permission_rules=dict(self.permission_rules),
                mcp_servers=self.mcp_servers,
                mcp_manager=self.mcp_manager,
                max_context_tokens=self.max_context_tokens,
                compaction=dict(self.compaction),
                model_limits=self.model_limits,
                agents=self.agents,
                skills=self.skills,
                approval_handler=self.approval_handler,
                review_handler=self.review_handler,
            )
        sub.root = self.root
        sub.secondary_roots = list(self.secondary_roots)
        sub.extra_safe_dirs = list(self.extra_safe_dirs)
        sub.mcp_servers = self.mcp_servers
        sub.mcp_manager = self.mcp_manager
        sub.permission_mode = self.permission_mode
        sub.permission_rules = dict(self.permission_rules)
        sub.approval_handler = self.approval_handler
        sub.review_handler = self.review_handler
        if spec is None:
            sub.history.set_system(sub._build_system())
            return sub
        if spec.tools is not None:
            sub.enabled_tools = set(spec.tools)
        if spec.permission:
            sub.permission_mode = cap_permission(self.permission_mode, spec.permission)
        else:
            sub.permission_mode = self.permission_mode
        if spec.system:
            sub.system_override = spec.system
        if spec.temperature is not None and hasattr(sub.provider, "kwargs"):
            sub.provider.kwargs.setdefault("temperature", spec.temperature)
        sub.history.set_system(sub._build_system())
        return sub

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
        return self.history.last_user_index() >= 0

    def redo_available(self) -> bool:
        return bool(self._redo_stack)

    def undo_turn(self) -> dict:
        """Undo the last user turn: drop its messages and restore files.

        Returns a summary dict; raises RuntimeError when there is nothing to undo.
        """
        if not self.undo_available():
            raise RuntimeError("nothing to undo")
        removed = self.history.pop_user_turn()
        self._redo_stack.append(removed)
        if self.snapshot_manager and self.snapshot_manager.can_undo():
            return self.snapshot_manager.undo_turn()
        return {"restored": [], "message_only": True}

    def redo_turn(self) -> dict:
        """Redo the last undone turn: re-append its messages and reapply files."""
        if not self._redo_stack:
            raise RuntimeError("nothing to redo")
        self.history.messages.extend(self._redo_stack.pop())
        if self.snapshot_manager and self.snapshot_manager.can_redo():
            return self.snapshot_manager.redo_turn()
        return {"restored": [], "message_only": True}

    def undo_to_user(self, nth: int) -> dict:
        """Undo everything back to just before the ``nth`` user message (1-based).

        The ``nth`` prompt and everything after it are removed and their file
        changes rolled back. Pending redo state is discarded (a batch undo
        cannot be re-applied in one step). Raises RuntimeError when ``nth``
        is out of range.
        """
        count = sum(1 for m in self.history.messages if m.get("role") == "user")
        if nth < 1 or nth > count:
            raise RuntimeError(f"invalid user message index: {nth} (有 {count} 条用户消息)")
        summary: dict = {"restored": []}
        while count >= nth and self.undo_available():
            removed = self.history.pop_user_turn()
            self._redo_stack.clear()
            if self.snapshot_manager and self.snapshot_manager.can_undo():
                part = self.snapshot_manager.undo_turn()
                summary["restored"].extend(part.get("restored", []))
            count -= 1
        if not summary["restored"]:
            summary["message_only"] = True
        return summary

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
        # MS-6: track the in-flight thread so a cancellation can drain it before
        # rolling back the snapshot (late-write barrier). The done callback
        # removes it once the thread actually finishes.
        entry = (tc.name, fut)
        self._pending_tool_tasks.append(entry)
        fut.add_done_callback(lambda _f: self._discard_pending(entry))
        try:
            return await fut
        except Exception as exc:  # noqa: BLE001 - report tool failures to the model
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})

    def _discard_pending(self, entry: tuple[str, asyncio.Future]) -> None:
        try:
            self._pending_tool_tasks.remove(entry)
        except ValueError:
            pass
