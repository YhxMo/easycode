"""Tool schemas and execution for task delegation and skill loading."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError

from easycode.policy import cap_permission

if TYPE_CHECKING:
    from easycode.agent.loop import Agent, ToolCall
    from easycode.agents import AgentSpec

DEFAULT_MAX_PARALLEL = 4
MAX_PARALLEL_TASKS = 6

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TaskSpec(BaseModel):
    """One parallel subtask."""

    name: NonEmptyStr = Field(description="short task label")
    prompt: NonEmptyStr = Field(description="self-contained task instructions")


class TaskArgs(BaseModel):
    """Arguments of the ``task`` tool."""

    agent: NonEmptyStr = Field(description="name of the agent to delegate to")
    prompt: NonEmptyStr = Field(description="self-contained task instructions")


class ParallelTasksArgs(BaseModel):
    """Arguments of the ``parallel_tasks`` tool."""

    tasks: list[TaskSpec] = Field(min_length=1, max_length=MAX_PARALLEL_TASKS)
    max_parallel: int = Field(default=DEFAULT_MAX_PARALLEL, ge=1, le=MAX_PARALLEL_TASKS)


class UseSkillArgs(BaseModel):
    """Arguments of the ``use_skill`` tool."""

    name: NonEmptyStr = Field(description="skill name to load")


class TodoItem(BaseModel):
    """One line of the session's task list."""

    text: NonEmptyStr = Field(description="what the step is")
    status: Literal["pending", "in_progress", "completed"] = Field(description="step state")


class UpdateTodosArgs(BaseModel):
    """Arguments of the ``update_todos`` tool."""

    todos: list[TodoItem] = Field(
        max_length=50,
        description="the complete list, replacing whatever was there before",
    )


def _parameters_schema(model: type[BaseModel]) -> dict:
    """JSON schema for a builtin tool, with nested ``$ref``s inlined.

    The runtime model is the single source of truth, while the wire format
    stays a self-contained object schema.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                return inline({k: v for k, v in target.items() if k != "title"})
            return {k: inline(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(schema)


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
        "parameters": _parameters_schema(ParallelTasksArgs),
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
        "parameters": _parameters_schema(TaskArgs),
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
        "parameters": _parameters_schema(UseSkillArgs),
    },
}

UPDATE_TODOS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_todos",
        "description": (
            "Replace the session's task list. Use it for multi-step work: send the "
            "whole list on every call, moving each step between pending, "
            "in_progress and completed as you go."
        ),
        "parameters": _parameters_schema(UpdateTodosArgs),
    },
}

ARG_MODELS: dict[str, type[BaseModel]] = {
    "parallel_tasks": ParallelTasksArgs,
    "task": TaskArgs,
    "update_todos": UpdateTodosArgs,
    "use_skill": UseSkillArgs,
}
BUILTIN_TOOLS = frozenset(ARG_MODELS)


async def run_task(agent: Agent, agent_name: str, prompt: str) -> str:
    """Runner for the ``task`` tool: delegate one job to a named subagent."""
    name = agent_name.strip().lower()
    if not agent.agents or not name:
        return json.dumps({"status": "error", "message": "unknown agent or no agents configured"})
    spec = agent.agents.get(name)
    if spec is None:
        return json.dumps(
            {
                "status": "error",
                "message": f"unknown agent: {name}",
                "known_agents": agent.agents.names(),
            },
            ensure_ascii=False,
        )
    if not spec.delegatable:
        return json.dumps(
            {"status": "error", "message": f"agent is not delegatable: {name}"},
            ensure_ascii=False,
        )
    sub: Agent | None = None
    try:
        sub = make_subagent(agent, spec)
        text = await sub.run_task(prompt)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if sub is not None:
            await sub.close_mcp()
    return json.dumps(
        {"status": "ok", "agent": spec.name, "model": spec.model, "result": text},
        ensure_ascii=False,
    )


async def run_use_skill(agent: Agent, name: str) -> str:
    """Runner for the ``use_skill`` tool: inject a skill body into the session."""
    name = name.strip().lower()
    if not agent.skills:
        return json.dumps({"status": "error", "message": "no skills configured"})
    skill = agent.skills.get(name)
    if skill is None:
        return json.dumps(
            {
                "status": "error",
                "message": f"unknown skill: {name}",
                "known_skills": agent.skills.names(),
            },
            ensure_ascii=False,
        )
    # inject the body into the conversation once (stays for the session); the
    # loop flushes it after this batch's tool results to keep the tool protocol
    agent._pending_system.append(f"[skill: {skill.name}]\n{skill.body}")
    return json.dumps(
        {"status": "ok", "skill": skill.name, "loaded": True},
        ensure_ascii=False,
    )


async def run_parallel(agent: Agent, tasks: list[TaskSpec], max_parallel: int) -> str:
    """Runner for the ``parallel_tasks`` tool: run independent subtasks concurrently."""
    sem = asyncio.Semaphore(max_parallel)

    async def run_one(task: TaskSpec) -> dict:
        async with sem:
            sub: Agent | None = None
            try:
                sub = make_subagent(agent)
                text = await sub.run_task(task.prompt)
                return {"name": task.name, "result": text}
            except Exception as exc:  # noqa: BLE001
                return {"name": task.name, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                if sub is not None:
                    await sub.close_mcp()

    results = await asyncio.gather(*(run_one(t) for t in tasks))
    return json.dumps(
        {"status": "ok", "count": len(results), "results": results}, ensure_ascii=False
    )


async def run_update_todos(agent: Agent, todos: list[TodoItem]) -> str:
    """Runner for the ``update_todos`` tool: publish the session's task list.

    The list is session state, not a file: the agent owns it, so a validated
    call replaces it outright and the loop then announces the new value to the
    UI. Nothing touches the workspace, so it needs no approval, and no other
    component has to write the same state back.
    """
    payload = [{"text": item.text, "status": item.status} for item in todos]
    agent.todos = payload
    completed = sum(1 for item in payload if item["status"] == "completed")
    return json.dumps(
        {"status": "ok", "count": len(payload), "completed": completed}, ensure_ascii=False
    )


async def run_builtin(agent: Agent, tc: ToolCall) -> str:
    """Dispatch one builtin tool call to its runner.

    Arguments are validated against the same Pydantic models the advertised
    schemas are generated from; a validation failure becomes a normal tool
    result so the model can correct itself and the history stays paired.
    """
    model = ARG_MODELS.get(tc.name)
    if model is None:
        return json.dumps({"status": "error", "message": f"unknown builtin tool: {tc.name}"})
    try:
        args = model.model_validate(tc.arguments)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "arguments"
        return json.dumps(
            {"status": "error", "message": f"invalid {model.__name__}: {where}: {first['msg']}"},
            ensure_ascii=False,
        )
    if tc.name == "parallel_tasks":
        return await run_parallel(agent, args.tasks, args.max_parallel)
    if tc.name == "task":
        return await run_task(agent, args.agent, args.prompt)
    if tc.name == "update_todos":
        return await run_update_todos(agent, args.todos)
    return await run_use_skill(agent, args.name)


def make_subagent(agent: Agent, spec: AgentSpec | None = None) -> Agent:
    """Build a subagent; ``spec`` (task tool) overrides model/system/tools/permission.

    Requires ``agent.subagent_factory`` (set by ``make_agent``): the factory
    resolves the model alias through the same credential path as the parent.
    Directly constructed agents without a factory cannot delegate.
    """
    if agent.subagent_factory is None:
        raise RuntimeError("sub-agent delegation is not configured (no subagent_factory)")
    model = spec.model if spec is not None and spec.model else None
    # the factory resolves aliases; without a spec model, inherit the parent's
    # alias so credentials and api_format stay correct
    sub = agent.subagent_factory(model or agent.model_alias or agent.provider.model)
    # A subagent can never exceed its parent: the parent's final tool set is
    # the ceiling, and an explicit spec.tools list narrows it further. Task
    # delegation itself is never inheritable (no recursive delegation).
    parent_tools = agent.available_tool_names() - {"task", "parallel_tasks"}
    if spec is not None and spec.tools is not None:
        sub.enabled_tools = set(spec.tools) & parent_tools
    else:
        sub.enabled_tools = set(parent_tools)
    sub.secondary_roots = list(agent.secondary_roots)
    sub.extra_safe_dirs = list(agent.extra_safe_dirs)
    sub.permission_mode = agent.permission_mode
    if spec is not None and spec.permission:
        sub.permission_mode = cap_permission(agent.permission_mode, spec.permission)
    sub.mcp_servers = agent.mcp_servers
    sub.mcp_owned = False
    # Only an identical sandbox context may share the parent's MCP process; a
    # differently sandboxed subagent creates (and later closes) its own.
    if agent.mcp_manager is not None and sub.path_context() == agent.path_context():
        sub.mcp_manager = agent.mcp_manager
    else:
        sub.mcp_manager = None
    sub.permission_rules = dict(agent.permission_rules)
    sub.approval_handler = agent.approval_handler
    sub.review_handler = agent.review_handler
    if spec is not None:
        if spec.system:
            sub.system_override = spec.system
        if spec.temperature is not None and hasattr(sub.provider, "kwargs"):
            sub.provider.kwargs.setdefault("temperature", spec.temperature)
    sub.history.set_system(sub._build_system())
    return sub


__all__ = [
    "BUILTIN_TOOLS",
    "DEFAULT_MAX_PARALLEL",
    "PARALLEL_TASKS_SCHEMA",
    "TASK_SCHEMA",
    "USE_SKILL_SCHEMA",
    "make_subagent",
    "run_builtin",
    "run_parallel",
    "run_task",
    "run_use_skill",
]
