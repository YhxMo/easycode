"""Skills: SKILL.md files with YAML frontmatter, loaded on demand.

Convention (opencode / Claude Code style)::

    .easycode/skills/{name}/SKILL.md     # per project (any workspace root)
    ~/.easycode/skills/{name}/SKILL.md   # personal, all projects

The directory name is the skill name (frontmatter ``name`` overrides the
display label). Only ``name`` + ``description`` are listed in the system
prompt (progressive disclosure); the body is injected into the conversation
when the model calls ``use_skill`` or the user runs ``/name``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from easycode.frontmatter import parse_spec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    source: str = "project"  # project > user on name conflict


def personal_skills(user_dir: Path | None = None) -> list[Skill]:
    """Skills in the personal directory (``~/.easycode/skills`` by default)."""
    root = user_dir or Path.home() / ".easycode" / "skills"
    if not root.is_dir():
        return []
    return [skill for skill in (_load_skill(d, "user") for d in sorted(root.iterdir())) if skill]


def project_skills(roots: list[Path]) -> list[Skill]:
    """Skills defined under these roots' ``.easycode/skills``, in root order."""
    out: list[Skill] = []
    for root in roots:
        proj = root / ".easycode" / "skills"
        for d in sorted(proj.iterdir()) if proj.is_dir() else []:
            skill = _load_skill(d, source="project")
            if skill:
                out.append(skill)
    return out


class SkillRegistry:
    """Skills resolved from project + personal directories."""

    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = dict(skills or {})

    @classmethod
    def discover(cls, roots: list[Path], user_dir: Path | None = None) -> SkillRegistry:
        reg = cls()
        for skill in [*personal_skills(user_dir), *project_skills(roots)]:
            reg._skills[skill.name] = skill  # project overrides user
        return reg

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name.strip().lower())

    def names(self) -> list[str]:
        return sorted(self._skills)

    def list(self) -> list[Skill]:
        return [self._skills[n] for n in self.names()]

    def desc_lines(self) -> list[str]:
        return [f"- {s.name}: {s.description}" for s in self.list()]


def _load_skill(dir_path: Path, source: str) -> Skill | None:
    md = dir_path / "SKILL.md"
    if not md.is_file():
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_spec(text, default_name=dir_path.name, required=("description",))
    except Exception as exc:  # noqa: BLE001 - one bad file must not break discovery
        log.warning("skip skill %s: %s", dir_path, exc)
        return None
    return Skill(
        name=str(meta["name"]).strip().lower(),
        description=str(meta["description"]).strip(),
        body=body.strip(),
        source=source,
    )