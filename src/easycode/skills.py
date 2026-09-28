"""Skills: SKILL.md files with YAML frontmatter, loaded on demand.

Convention (opencode / Claude Code style)::

    .easycode/skills/{name}/SKILL.md     # per project (any workspace root)
    ~/.easycode/skills/{name}/SKILL.md   # personal, all projects

The directory name is the skill name (frontmatter ``name`` overrides the
display label). Only ``name`` + ``description`` are listed in the system
prompt (progressive disclosure); the body is injected into the conversation
when the model calls ``use_skill`` or the user runs ``/name``.

A skill is a package, not a file: its body is written against the directory it
was installed in, so loading it also states where that directory is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from easycode.credentials import data_home
from easycode.frontmatter import FrontmatterError, parse_spec

log = logging.getLogger(__name__)

#: Command names are reserved for MCP services; a skill may not shadow one.
MCP_COMMAND_PREFIX = "mcp:"


class SkillLoadError(ValueError):
    """A directory that cannot be loaded as a skill, with the reason why."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    source: str = "project"  # project > user on name conflict
    #: Absolute path of the SKILL.md this skill was read from. ``None`` only for
    #: a skill built by hand (tests), which therefore has no resource directory.
    path: Path | None = None

    @property
    def directory(self) -> Path | None:
        """The package root: what the body's relative paths resolve against."""
        return self.path.parent if self.path is not None else None


def personal_skills_dir() -> Path:
    """Where personal skills are installed (``~/.easycode/skills``)."""
    return data_home() / "skills"


def project_skills_dir(root: Path) -> Path:
    """Where one project's skills are installed (``<root>/.easycode/skills``)."""
    return Path(root) / ".easycode" / "skills"


def load_skill(dir_path: Path, source: str) -> Skill:
    """Load one skill directory strictly; anything unusable raises.

    Discovery catches what this raises so one broken package cannot hide the
    rest, while the importer and the management API call it directly: there a
    bad file has to be reported, never quietly skipped.
    """
    md = dir_path / "SKILL.md"
    if not md.is_file():
        raise SkillLoadError(f"{dir_path} 里没有 SKILL.md")
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLoadError(f"无法读取 {md}: {exc}") from exc
    try:
        meta, body = parse_spec(text, default_name=dir_path.name, required=("description",))
    except FrontmatterError as exc:
        raise SkillLoadError(f"{md}: {exc}") from exc
    return Skill(
        name=str(meta["name"]).strip().lower(),
        description=str(meta["description"]).strip(),
        body=body.strip(),
        source=source,
        path=md.resolve(),
    )


def skill_context(skill: Skill) -> str:
    """What loading a skill puts into the conversation.

    It names the resource directory as well as the body: ``references/guide.md``
    only resolves once the model knows what it is relative to, and the skill may
    have been installed anywhere. Nothing in the body is executed here.
    """
    lines = [f"# Skill: {skill.name}", ""]
    if skill.path is not None:
        lines += [
            f"Skill file: {skill.path}",
            f"Resource directory: {skill.path.parent}",
            (
                "Relative paths in this skill's instructions are relative to that "
                "resource directory; resolve them against it before reading or "
                "running anything."
            ),
            "",
        ]
    lines.append(skill.body)
    return "\n".join(lines)


def personal_skills(user_dir: Path | None = None) -> list[Skill]:
    """Skills in the personal directory (``~/.easycode/skills`` by default)."""
    root = user_dir or personal_skills_dir()
    if not root.is_dir():
        return []
    return [skill for skill in (_load_skill(d, "user") for d in sorted(root.iterdir())) if skill]


def project_skills(roots: list[Path]) -> list[Skill]:
    """Skills defined under these roots' ``.easycode/skills``, in root order."""
    out: list[Skill] = []
    for root in roots:
        proj = project_skills_dir(root)
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
    """Discovery's tolerant wrapper: skip what cannot be loaded, with a reason."""
    try:
        return load_skill(dir_path, source)
    except Exception as exc:  # noqa: BLE001 - one bad package must not break discovery
        log.warning("skip skill %s: %s", dir_path, exc)
        return None
