"""Path ownership: classify filesystem paths into sandbox categories.

Categories:

- ``workspace``: under the primary or secondary working directories
- ``temp``: under the system temporary directory (``tempfile.gettempdir()``)
- ``system``: under the data dir (``~/.easycode/``, sessions/credentials...)
- ``external``: anything else

Everything but ``external`` is exempt from approval prompts (plus any
``extra_safe_dirs``). The approval layer (P5-2) uses the same context.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from easycode.credentials import data_home

CATEGORIES = ("workspace", "temp", "system", "external")


@dataclass(frozen=True)
class PathContext:
    """Sandbox roots + safe dirs for one agent/session."""

    primary: Path
    secondary: list[Path] = field(default_factory=list)
    extra_safe_dirs: list[Path] = field(default_factory=list)

    @property
    def roots(self) -> list[Path]:
        """Primary + secondary roots, resolved, deduplicated."""
        out: list[Path] = []
        seen: set[Path] = set()
        for r in [self.primary, *self.secondary]:
            resolved = r.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out

    def safe_dirs(self) -> list[Path]:
        """All directories exempt from approval: roots + temp + ~/.easycode + extras."""
        out: list[Path] = [*self.roots]
        for d in [Path(tempfile.gettempdir()), data_home(), *self.extra_safe_dirs]:
            resolved = d.resolve()
            if not any(resolved.is_relative_to(p) or p.is_relative_to(resolved) for p in out):
                out.append(resolved)
        return out

    def in_allowed(self, path: Path) -> bool:
        """True if ``path`` lies under any safe directory."""
        p = path.resolve()
        return any(p.is_relative_to(d) for d in self.safe_dirs())

    def classify(self, path: Path) -> str:
        """Categorize a path: workspace | temp | system | external."""
        p = path.resolve()
        if any(p.is_relative_to(r) for r in self.roots):
            return "workspace"
        if p.is_relative_to(data_home().resolve()):
            return "system"
        if p.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            return "temp"
        return "external"

    def resolve(self, path: str | Path) -> Path:
        """Turn a tool path into a concrete path.

        Relative paths try each root in order (first that contains it);
        absolute paths pass through.
        """
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        for r in self.roots:
            cand = (r / p).resolve()
            if cand.is_relative_to(r):
                return cand
        return (self.primary / p).resolve()

    def display(self, path: Path) -> str:
        """Human/agent-friendly path: relative to its root, else absolute."""
        p = path.resolve()
        for r in self.roots:
            if p.is_relative_to(r):
                return str(p.relative_to(r))
        return str(p)


def classify_path(path: Path, ctx: PathContext) -> str:
    """Module-level helper: ``classify_path(p, ctx) -> "workspace"|"temp"|"system"|"external"``."""
    return ctx.classify(path)


def in_allowed(path: Path, ctx: PathContext) -> bool:
    """Module-level helper: is ``path`` inside any approval-exempt directory."""
    return ctx.in_allowed(path)