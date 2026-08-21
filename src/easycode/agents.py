"""Custom agents: markdown files with YAML frontmatter.

Convention (aligned with opencode / Claude Code)::

    .easycode/agents/{name}.md        # per project (primary + secondary roots)
    ~/.easycode/agents/{name}.md      # personal, all projects

Frontmatter:

- ``description`` (required): when to delegate to this agent
- ``name`` (optional): defaults to the file name
- ``model`` (optional): model alias; defaults to the invoking agent's model
- ``tools`` (optional): allow-list of tool names; defaults to all enabled
- ``permission`` (optional): ask | auto-review | allow-all
- ``mode`` (optional): subagent (default) | primary | all
- ``temperature`` (optional): provider temperature

Body = the agent's system prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from easycode.frontmatter import parse_spec

log = logging.getLogger(__name__)

MODES = ("subagent", "primary", "all")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model: str | None = None
    tools: list[str] | None = None
    permission: str | None = None
    mode: str = "subagent"
    temperature: float | None = None
    system: str = ""
    source: str = "project"  # project > user on name conflict

    @property
    def delegatable(self) -> bool:
        return self.mode in ("subagent", "all")


class AgentRegistry:
    """Named agent specs resolved from project + user markdown files."""

    def __init__(self, specs: dict[str, AgentSpec] | None = None) -> None:
        self._specs: dict[str, AgentSpec] = dict(specs or {})

    @classmethod
    def discover(cls, roots: list[Path], user_dir: Path | None = None) -> "AgentRegistry":
        """Discover agents: project dirs win over the personal dir on conflicts."""
        reg = cls()
        user = (user_dir or Path.home() / ".easycode" / "agents")
        for p in sorted(user.glob("*.md")) if user.is_dir() else []:
            spec = _load_spec(p, source="user")
            if spec:
                reg._specs[spec.name] = spec
        for root in roots:
            proj = root / ".easycode" / "agents"
            for p in sorted(proj.glob("*.md")) if proj.is_dir() else []:
                spec = _load_spec(p, source="project")
                if spec:
                    reg._specs[spec.name] = spec
        return reg

    def get(self, name: str) -> AgentSpec | None:
        return self._specs.get(name.strip().lower())

    def names(self) -> list[str]:
        return sorted(self._specs)

    def list(self) -> list[AgentSpec]:
        return [self._specs[n] for n in self.names()]

    def desc_lines(self) -> list[str]:
        return [f"- {s.name}: {s.description}" for s in self.list() if s.delegatable]


def _load_spec(path: Path, source: str) -> AgentSpec | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_spec(text, default_name=path.stem, required=("description",))
    except Exception as exc:  # noqa: BLE001 - one bad file must not break discovery
        log.warning("skip agent file %s: %s", path, exc)
        return None
    mode = str(meta.get("mode") or "subagent").strip().lower()
    if mode not in MODES:
        mode = "subagent"
    tools = meta.get("tools")
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    tools = [str(t) for t in (tools or [])]
    temperature = meta.get("temperature")
    model_raw = (meta.get("model") or "").strip()
    return AgentSpec(
        name=str(meta["name"]).strip().lower(),
        description=str(meta["description"]).strip(),
        model=model_raw or None,
        tools=tools or None,
        permission=str(meta.get("permission") or "").strip().lower() or None,
        mode=mode,
        temperature=float(temperature) if temperature is not None else None,
        system=body.strip(),
        source=source,
    )