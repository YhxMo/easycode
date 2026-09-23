"""Tool schemas and execution for task delegation and skill loading."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from easycode.policy import cap_permission

if TYPE_CHECKING:
    from easycode.agent.loop import Agent, ToolCall
    from easycode.agents import AgentSpec

DEFAULT_MAX_PARALLEL = 4

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
                            "prompt": {
                                "type": "string",
                                "description": "self-contained task instructions",
                            },
                        },
                        "required": ["name", "prompt"],
                    },
                },
                "max_parallel": {
                    "type": "integer",
                    "default": DEFAULT_MAX_PARALLEL,
                    "minimum": 1,
                    "maximum": 6,
                },
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


async def run_task(agent: Agent, tc: ToolCall) -> str:
    """Runner for the ``task`` tool: delegate one job to a named subagent."""
    name = str(tc.arguments.get("agent", "")).strip().lower()
    prompt = str(tc.arguments.get("prompt", "")).strip()
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
    if not prompt:
        return json.dumps({"status": "error", "message": "task prompt is empty"})
    try:
        sub = make_subagent(agent, spec)
        text = await sub.run_task(prompt)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
    return json.dumps(
        {"status": "ok", "agent": spec.name, "model": spec.model, "result": text},
        ensure_ascii=False,
    )


async def run_use_skill(agent: Agent, tc: ToolCall) -> str:
    """Runner for the ``use_skill`` tool: inject a skill body into the session."""
    name = str(tc.arguments.get("name", "")).strip().lower()
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
    # inject the body into the conversation once (stays for the session)
    agent.history.add({"role": "system", "content": f"[skill: {skill.name}]\n{skill.body}"})
    return json.dumps(
        {"status": "ok", "skill": skill.name, "loaded": True},
        ensure_ascii=False,
    )


async def run_parallel(agent: Agent, tasks: list[dict], max_parallel: int) -> str:
    """Runner for the ``parallel_tasks`` tool: run independent subtasks concurrently."""
    sem = asyncio.Semaphore(min(max_parallel, 6) or 1)

    async def run_one(task: dict) -> dict:
        async with sem:
            name = task.get("name", "task")
            prompt = task.get("prompt", "")
            try:
                sub = make_subagent(agent)
                text = await sub.run_task(prompt)
                return {"name": name, "result": text}
            except Exception as exc:  # noqa: BLE001
                return {"name": name, "error": f"{type(exc).__name__}: {exc}"}

    results = await asyncio.gather(*(run_one(t) for t in tasks))
    return json.dumps(
        {"status": "ok", "count": len(results), "results": results}, ensure_ascii=False
    )


async def run_builtin(agent: Agent, tc: ToolCall) -> str:
    """Dispatch one builtin tool call to its runner (previously ``_run_builtin``)."""
    if tc.name == "parallel_tasks":
        try:
            sub_tasks = tc.arguments.get("tasks", [])
            max_parallel = int(tc.arguments.get("max_parallel", DEFAULT_MAX_PARALLEL))
            return await run_parallel(agent, sub_tasks, max_parallel)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
    if tc.name == "task":
        return await run_task(agent, tc)
    if tc.name == "use_skill":
        return await run_use_skill(agent, tc)
    return json.dumps({"status": "error", "message": f"unknown builtin tool: {tc.name}"})


def make_subagent(agent: Agent, spec: AgentSpec | None = None) -> Agent:
    """Build a subagent; ``spec`` (task tool) overrides model/system/tools/permission.

    Uses ``agent.subagent_factory`` when provided (dependency inversion); the
    fallback builds an :class:`~easycode.agent.loop.Agent` directly via a
    deferred import to avoid a top-level circular reference.
    """
    model = None
    if spec is not None and spec.model:
        model = spec.model
    if agent.subagent_factory:
        sub = agent.subagent_factory(model or agent.provider.model)
    else:
        from easycode.agent.loop import Agent  # deferred: call-time, avoids cycle
        from easycode.models.litellm_provider import LiteLLMProvider

        provider = LiteLLMProvider(
            model or agent.provider.model, **getattr(agent.provider, "kwargs", {})
        )
        sub = Agent(
            provider=provider,
            registry=agent.registry,
            root=agent.root,
            enabled_tools=agent.enabled_tools,
            max_context_tokens=agent.max_context_tokens,
            compaction=dict(agent.compaction),
            model_limits=agent.model_limits,
            agents=agent.agents,
            skills=agent.skills,
        )
    sub.root = agent.root
    sub.secondary_roots = list(agent.secondary_roots)
    sub.extra_safe_dirs = list(agent.extra_safe_dirs)
    sub.mcp_servers = agent.mcp_servers
    sub.mcp_manager = agent.mcp_manager
    sub.permission_mode = agent.permission_mode
    sub.permission_rules = dict(agent.permission_rules)
    sub.approval_handler = agent.approval_handler
    sub.review_handler = agent.review_handler
    if spec is None:
        sub.history.set_system(sub._build_system())
        return sub
    if spec.tools is not None:
        sub.enabled_tools = set(spec.tools)
    if spec.permission:
        sub.permission_mode = cap_permission(agent.permission_mode, spec.permission)
    else:
        sub.permission_mode = agent.permission_mode
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
