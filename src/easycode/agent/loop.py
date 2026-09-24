"""The agentic loop: stream, execute tools, feed results back, repeat."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from easycode.agent import builtin_tools
from easycode.agent.builtin_tools import (
    BUILTIN_TOOLS,
    PARALLEL_TASKS_SCHEMA,
    TASK_SCHEMA,
    USE_SKILL_SCHEMA,
)
from easycode.agent.compaction import COMPACTION_DEFAULTS, Compactor
from easycode.agent.context import History
from easycode.agent.summarizer import Summarizer
from easycode.agent.system import build_system_prompt, find_agents_rules
from easycode.approval import (
    approval_reason,
    definitive_deny_reason,
    grant_for_toolcall,
    needs_approval,
)
from easycode.models.base import Provider, ToolCall
from easycode.policy import (
    APPROVAL_NEVER,
    PERM_ASK,
    PERM_AUTO_REVIEW,
    REVIEWER_AUTO,
    ExecutionPolicy,
    permission_rule_action,
)
from easycode.reviewer import ReviewDecision
from easycode.tools.registry import ToolRegistry
from easycode.workspace import PathContext, ToolGrant

if TYPE_CHECKING:
    from easycode.agents import AgentRegistry
    from easycode.mcp import MCPSessionManager
    from easycode.skills import SkillRegistry

MAX_TOOL_ITERATIONS = 12


@dataclass
class AgentEvent:
    """Event yielded to the UI as a turn progresses."""

    kind: str  # "text" | "tool_start" | "tool_result" | "error" | "done" | "cancelled"
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: str | None = None
    error: str | None = None


def file_change(name: str, result: str) -> dict | None:
    """Extract a real file change from a tool result, else ``None``.

    Only ``write_file``/``edit_file`` results that succeeded, were not a
    dry-run preview, and carry a path count as changes.
    """
    if name not in ("write_file", "edit_file"):
        return None
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or data.get("dry_run"):
        return None
    path = data.get("path")
    if not path:
        return None
    return {"tool": name, "path": path, "diff": data.get("diff") or ""}


@dataclass
class Agent:
    provider: Provider
    registry: ToolRegistry
    root: Path
    history: History = field(default_factory=History)
    enabled_tools: set[str] | None = None
    model_alias: str | None = None
    summarizer: Summarizer | None = None
    subagent_factory: Callable[[str], Agent] | None = None
    secondary_roots: list[Path] = field(default_factory=list)
    extra_safe_dirs: list[Path] = field(default_factory=list)
    permission_mode: str = PERM_ASK
    permission_rules: dict[str, Any] = field(default_factory=dict)
    approval_handler: Callable[[ToolCall, str], Awaitable[bool]] | None = None
    review_handler: (
        Callable[[ToolCall, str, list[dict]], Awaitable[ReviewDecision | bool]] | None
    ) = None
    _review_items: list[dict] = field(default_factory=list)
    _review_decisions: list[bool] = field(default_factory=list)
    _consecutive_review_denials: int = 0
    _pending_system: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    mcp_manager: MCPSessionManager | None = None
    max_context_tokens: int = 32_000
    compaction: dict[str, Any] = field(default_factory=dict)
    model_limits: dict[str, int] | None = None
    agents: AgentRegistry | None = None
    skills: SkillRegistry | None = None
    system_override: str | None = None

    def __post_init__(self) -> None:
        self.compaction = {**COMPACTION_DEFAULTS, **(self.compaction or {})}
        self.compactor = Compactor(self.compaction)
        self.history.max_tokens = self.compactor.usable_tokens(
            self.max_context_tokens, self.model_limits
        )
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
        if self.enabled_tools is None or "parallel_tasks" in self.enabled_tools:
            schemas = [*schemas, PARALLEL_TASKS_SCHEMA]
        if self.agents is not None and self.agents.names():
            schemas = [*schemas, TASK_SCHEMA]
        if self.skills is not None and self.skills.names():
            schemas = [*schemas, USE_SKILL_SCHEMA]
        if self.mcp_manager:
            schemas = [*schemas, *self.mcp_manager.tool_schemas()]
        return schemas

    def all_tool_names(self) -> set[str]:
        """Every registry tool name (MCP tools are governed by mcp_servers)."""
        return {schema["function"]["name"] for schema in self.registry.schemas(None)}

    def _tool_schema_tokens(self, schemas: list[dict]) -> int:
        """Estimated token cost of the tool schemas sent on every completion.

        The budget accounts for these so a large tool set (many MCP tools, a
        bloated schema) trips compaction instead of silently exceeding the
        provider window.
        """
        if not schemas:
            return 0
        return self.history.estimate_text_tokens(json.dumps(schemas, ensure_ascii=False))

    async def respond(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """Run a user turn; keep completed operations in history if it is interrupted."""
        await self.init_mcp()
        self.history.add_user(user_input)
        self._review_items = []
        self._review_decisions = []
        self._consecutive_review_denials = 0
        try:
            async with aclosing(self._turn()) as events:
                async for event in events:
                    yield event
        except Exception as exc:
            yield AgentEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            raise

    async def _turn(self) -> AsyncIterator[AgentEvent]:
        for _ in range(MAX_TOOL_ITERATIONS):
            schemas = self.tool_schemas()
            allowed = {schema["function"]["name"] for schema in schemas}
            await self.compactor.prepare(
                self.history, self.summarizer, self._tool_schema_tokens(schemas)
            )
            text: list[str] = []
            tool_calls: list[ToolCall] = []
            error: str | None = None
            try:
                async for event in self.provider.stream(self.history.payload(), schemas):
                    if event.kind == "text" and event.content:
                        text.append(event.content)
                        yield AgentEvent(kind="text", content=event.content)
                    elif event.kind == "tool_calls":
                        tool_calls = event.tool_calls or []
                    elif event.kind == "error":
                        error = event.error
            except BaseException:
                if text:
                    self.history.add_assistant("".join(text))
                raise

            if error or not tool_calls:
                self.history.add_assistant("".join(text))
                if error or not "".join(text).strip():
                    yield AgentEvent(kind="error", error=error or "模型没有返回内容，请重试。")
                if self.permission_mode == PERM_AUTO_REVIEW and self._review_items:
                    yield AgentEvent(
                        kind="review",
                        content=json.dumps({"changes": self._review_items}, ensure_ascii=False),
                    )
                yield AgentEvent(kind="done")
                return

            self.history.add_assistant(
                "".join(text), [tc.to_message_content() for tc in tool_calls]
            )
            pending = list(tool_calls)
            try:
                for tc in tool_calls:
                    async for event in self._execute_tool(tc, allowed):
                        if event.kind == "tool_result":
                            pending.remove(tc)
                        yield event
                        if event.kind == "done":
                            return
            finally:
                # Every declared call needs a result, even when a batch is interrupted.
                for tc in pending:
                    self.history.add_tool(
                        tc.id,
                        tc.name,
                        json.dumps(
                            {
                                "status": "error",
                                "message": "Turn interrupted. If this tool started, its effects may remain; inspect the workspace before retrying.",
                            }
                        ),
                    )
                # Skill bodies load while tools run; inject them only after the
                # batch's results so assistant tool_calls stay adjacent to their
                # tool messages (OpenAI-compatible providers require this).
                for content in self._pending_system:
                    self.history.add({"role": "system", "content": content})
                self._pending_system.clear()
        yield AgentEvent(kind="error", error=f"hit max tool iterations ({MAX_TOOL_ITERATIONS})")
        yield AgentEvent(kind="done")

    async def _execute_tool(
        self, tc: ToolCall, allowed: set[str]
    ) -> AsyncIterator[AgentEvent]:
        """Authorize, execute, and record one tool call.

        ``allowed`` is the exact tool-name set advertised to the model for this
        turn; a call outside it is rejected before any approval or execution
        path, so the whitelist cannot be bypassed by a hallucinated name.
        """
        ctx = self.path_context()
        policy = self.execution_policy
        grant: ToolGrant | None = None
        not_enabled = tc.name not in allowed
        deny_reason = (
            f"tool not enabled: {tc.name}"
            if not_enabled
            else definitive_deny_reason(tc, ctx)
        )
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
                approved = (
                    bool(await self.approval_handler(tc, reason)) if self.approval_handler else False
                )
        yield AgentEvent(kind="tool_start", tool_call=tc)
        if not approved:
            result = json.dumps(
                {
                    "status": "error",
                    "message": f"tool call rejected: {tc.name}: {denial_reason}",
                    "rejected": True,
                    "category": "policy"
                    if (not_enabled or rule_action == "deny")
                    else ("destructive" if deny_reason is not None else "approval"),
                    "reason": denial_reason,
                },
                ensure_ascii=False,
            )
        else:
            if requires_approval and grant is None:
                grant = grant_for_toolcall(tc, ctx)
            result = await self._dispatch_tool(tc, grant=grant)
        if self.permission_mode == PERM_AUTO_REVIEW:
            self._collect_review(tc, result)
        self.history.add_tool(tc.id, tc.name, self._model_view(tc.name, result))
        yield AgentEvent(kind="tool_result", tool_call=tc, tool_result=result)
        if breaker_tripped:
            yield AgentEvent(
                kind="error", error="automatic review denial limit reached; turn interrupted"
            )
            yield AgentEvent(kind="done")

    def _collect_review(self, tc: ToolCall, result: str) -> None:
        if change := file_change(tc.name, result):
            self._review_items.append(change)

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
        review channel, but the model does not need to re-read the diff
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

    async def run_task(self, prompt: str) -> str:
        """Run a standalone subtask with a fresh history; return the final text.

        Raises RuntimeError when the provider reports an error mid-turn.
        """
        parts: list[str] = []
        errors: list[str] = []
        async for ev in self.respond(prompt):
            if ev.kind == "text" and ev.content:
                parts.append(ev.content)
            elif ev.kind == "error" and ev.error:
                errors.append(ev.error)
        if errors:
            raise RuntimeError("; ".join(errors))
        return "".join(parts)

    async def _dispatch_tool(self, tc: ToolCall, grant: ToolGrant | None = None) -> str:
        if self.mcp_manager and self.mcp_manager.has_tool(tc.name):
            return await self.mcp_manager.call(tc.name, tc.arguments)
        if tc.name in BUILTIN_TOOLS:
            return await builtin_tools.run_builtin(self, tc)
        return await asyncio.to_thread(
            self.registry.execute,
            tc.name,
            tc.arguments,
            self.root,
            ctx=self.path_context(),
            grant=grant,
        )
