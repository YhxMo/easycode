"""HTTP endpoints for skills: what is installed, and installing a folder.

A skill is a package on disk, so this is a management surface for one part of
the filesystem: the two directories easycode discovers skills from. The paths a
request may name are decided here, never taken from the body — the browser can
send a source folder, it cannot choose where a skill lands.

Installed skills are reported with their own source: a personal skill and a
project skill that share a command name are both listed, and ``effective`` says
which one the existing discovery order would resolve to.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easycode.config import Config
from easycode.extensions.mcp.config import known_projects
from easycode.extensions.skill_import import SkillConflictError, SkillImportError, import_skill
from easycode.extensions.skills import (
    Skill,
    SkillRegistry,
    load_skill,
    personal_skills_dir,
    project_skills_dir,
)
from easycode.web.routes.workspaces import _normalise_root
from easycode.web.session import Session, SessionStore, idle_sessions, project_key, run_mutation

log = logging.getLogger("easycode.web.skills")


class ImportSkillRequest(BaseModel):
    source_path: str
    scope: Literal["personal", "project"] = "project"
    root: str | None = None


def register_skills(app: FastAPI, cfg: Config, store: SessionStore) -> None:
    def _resolve_root(requested: str | None) -> str:
        """The project a request may name; never an arbitrary directory.

        An omitted root means the default project, which every session without a
        directory of its own runs in.
        """
        root = _normalise_root(requested) or str(Path(cfg.root).expanduser().resolve())
        if root not in {p["root"] for p in known_projects(cfg, store)}:
            raise HTTPException(422, "需要一个已登记的项目目录")
        return root

    def _roots(project_root: str) -> list[Path]:
        """Where skills come from for one project: primary, then secondaries."""
        secondary = store._resolve_secondary(project_root, None)
        return [Path(project_root), *(Path(p) for p in secondary)]

    def _install_root(project_root: str, scope: str) -> Path:
        return personal_skills_dir() if scope == "personal" else project_skills_dir(project_root)

    def _collect(directory: Path, source: str, errors: list[str]) -> list[tuple[Skill, str]]:
        """Every loadable skill directly under one skills directory.

        A package that cannot be read is reported rather than dropped: a skill
        that has stopped working is what the reader came here to find out.
        """
        if not directory.is_dir():
            return []
        out: list[tuple[Skill, str]] = []
        for entry in sorted(directory.iterdir()):
            try:
                out.append(
                    (load_skill(entry, source), "personal" if source == "user" else "project")
                )
            except Exception as exc:  # noqa: BLE001 - one bad package must not hide the rest
                errors.append(f"{entry}: {exc}")
        return out

    def _snapshot(project_root: str) -> dict[str, Any]:
        errors: list[str] = []
        found: list[tuple[Skill, str]] = []
        # The same precedence agents use — project overrides personal, later
        # roots override earlier ones — but resolved from the loadables already
        # in hand, so one request reads each SKILL.md once.
        roots = _roots(project_root)
        found += _collect(personal_skills_dir(), "user", errors)
        for root in roots:
            found += _collect(project_skills_dir(root), "project", errors)
        effective: dict[str, Skill] = {}
        for skill, _scope in found:
            effective[skill.name] = skill
        skills = [_view(skill, scope, effective) for skill, scope in found]
        skills.sort(key=lambda row: (row["name"], row["scope"], row["directory"]))
        return {
            "root": project_root,
            "enabled": cfg.skills_enabled,
            "install_roots": {
                "personal": str(_install_root(project_root, "personal")),
                "project": str(_install_root(project_root, "project")),
            },
            "skills": skills,
            "errors": errors,
        }

    def _view(skill: Skill, scope: str, effective: dict[str, Skill]) -> dict[str, Any]:
        return {
            "name": skill.name,
            "description": skill.description,
            "scope": scope,
            "path": str(skill.path),
            "directory": str(skill.directory),
            # An entry shadowed by the same name in a higher scope is listed
            # too, and says so instead of disappearing.
            "effective": effective.get(skill.name) == skill,
        }

    def _targets(scope: str, project_root: str) -> list[Session]:
        """Sessions whose skill registry this install changes.

        A personal skill is discoverable everywhere. A project skill belongs to
        the project's own discovery roots, so a session that merely lists the
        directory as a secondary root picks it up as well.
        """
        if scope != "project":
            return store.list()
        key = project_key(project_root)
        default_root = str(Path(cfg.root).expanduser().resolve())
        out: list[Session] = []
        for sess in store.list():
            keys = {project_key(_normalise_root(sess.root) or default_root)}
            keys |= {project_key(_normalise_root(p)) for p in sess.secondary_roots or []}
            if key in keys:
                out.append(sess)
        return out

    @app.get("/api/skills")
    async def list_skills(root: str | None = None) -> dict:
        """Installed skills for one project, with their scope and their errors."""
        # Reading two directories is I/O: it must not hold the event loop while
        # a turn is streaming.
        return await asyncio.to_thread(_snapshot, _resolve_root(root))

    @app.post("/api/skills/import", status_code=201)
    async def import_skill_endpoint(req: ImportSkillRequest) -> dict:
        """Install a local folder as a skill, then refresh the affected sessions.

        A published install is a success even when the refresh that follows
        fails: the files are on disk, and saying otherwise would invite the user
        to import the same package again. The refresh is reported as a warning,
        and the next message in an affected session re-runs it anyway.
        """
        source = Path(req.source_path).expanduser()
        warnings: list[str] = []
        try:
            # The config guard serializes installs against each other and against
            # a model/project change; the session locks keep the agents being
            # rebuilt from being the ones a running turn is using.
            async with store.config_change():
                project_root = _resolve_root(req.root)
                destination = _install_root(project_root, req.scope)
                targets = _targets(req.scope, project_root)
                async with idle_sessions(targets):
                    try:
                        installed = await run_mutation(
                            import_skill, source, destination, req.scope
                        )
                    finally:
                        # Even when this request is cancelled after the worker
                        # published, the agents must stop describing the state
                        # before it. Rediscovery is synchronous, so nothing here
                        # can be interrupted halfway.
                        warnings = _refresh(targets)
                    registry = SkillRegistry.discover(_roots(project_root))
                    effective = {s.name: s for s in registry.list()}
                    return {
                        "imported": _view(installed, req.scope, effective),
                        "root": project_root,
                        "warnings": warnings,
                    }
        except SkillConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SkillImportError as exc:
            # Bad input, bad frontmatter, a link out of the package, a limit:
            # all of it is the request's problem, not the server's.
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"导入失败: {exc}") from exc

    def _refresh(targets: list[Session]) -> list[str]:
        """Re-read skills for the affected sessions, collecting what failed.

        One session that cannot be refreshed must not stop the others: the
        import already happened, so the rest of the sessions stay in step.
        """
        out: list[str] = []
        for sess in targets:
            try:
                sess.agent.rediscover_extensions(with_skills=cfg.skills_enabled)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                log.warning("刷新会话 %s 的扩展失败: %s", sess.id, exc)
                out.append(f"已导入，但会话 {sess.id} 的扩展刷新失败: {exc}")
        return out
