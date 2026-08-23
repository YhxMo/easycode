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
from easycode.policy import SANDBOX_DANGER_FULL_ACCESS, SANDBOX_READ_ONLY, SANDBOX_WORKSPACE_WRITE

CATEGORIES = ("workspace", "temp", "system", "external")

#: Directories that may never be registered as a workspace/temporary root.
SENSITIVE_ROOT_NAMES = {".git", ".easycode"}


@dataclass(frozen=True)
class ToolGrant:
    """Precise, single-call authorization conveyed by an approval.

    Replaces the fuzzy cross-tool boolean ``force_allowed``: an approval now
    states *what* it grants — network and/or a set of explicit external
    writable roots — instead of one anonymous flag. The roots are already
    canonical (absolute, resolved) and validated; each is added to the sandbox
    as its own ``-D`` parameter + subpath rule, and only for this one call.
    Nothing here is ever written back to Agent/Session/Config.
    """

    network_allowed: bool = False
    writable_roots: tuple[Path, ...] = ()


def resolve_workspace_path(raw: str | Path, base: Path | None = None) -> Path:
    """Resolve a configured workspace path to an absolute canonical path.

    Relative paths are anchored to ``base`` (the config-file directory) when
    given, otherwise to CWD — callers that must not drift (Web/Session) pass an
    explicit absolute ``base`` so a relative path is never silently resolved
    against the process CWD (P1-1).
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        anchor = base or Path.cwd()
        p = anchor / p
    return p.resolve()


def validate_writable_roots(raw: list[str], base: Path | None) -> tuple[list[Path], str | None]:
    """Strict validation for a shell tool's explicit ``writable_roots``.

    Used to authorize external-write access for ``execute_shell`` (P0-1). An
    entry must be an absolute, canonical, existing, non-sensitive directory
    (never a plain file, missing path, ``.git``/``.easycode``, or something under
    the data home). Relative paths are rejected outright — we never resolve the
    model's declaration against a drifting CWD. Returns ``(paths, error)`` where
    a non-None ``error`` means the caller must fail closed.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for raw_item in raw or []:
        s = str(raw_item).strip()
        if not s:
            continue
        p = Path(s).expanduser()
        if not p.is_absolute():
            return out, f"writable root must be an absolute path: {s}"
        p = p.resolve()
        err = root_error(p)
        if err is not None:
            return out, err
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, None


def normalise_secondary(secondary: list[str] | None, base: Path | None) -> tuple[list[Path], str | None]:
    """Resolve + validate + de-duplicate a secondary/extra_safe list (P1-1).

    Returns ``(paths, error)``; when ``error`` is non-None the caller must
    reject the registration (422). Every entry must be an existing directory
    and never a sensitive root.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in secondary or []:
        s = str(raw).strip()
        if not s:
            continue
        p = resolve_workspace_path(s, base)
        err = root_error(p)
        if err is not None:
            return out, err
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, None


def root_error(path: Path, *, require_dir: bool = True) -> str | None:
    """Validation for a workspace/temporary root candidate.

    Returns a human-readable error string when ``path`` is illegal as a root,
    else ``None``. A legal root must be absolute+canonical, must exist (unless
    ``require_dir`` is False), must be a directory (not a plain file), and must
    never be a sensitive dir (``.git``/``.easycode``/credentials) itself or live
    under the data home. P1-1/P0-2.
    """
    p = path.resolve()
    if p.name in SENSITIVE_ROOT_NAMES:
        return f"sensitive directory cannot be a workspace root: {p}"
    cred = data_home() / "credentials.json"
    if p == cred.resolve() or p.is_relative_to(cred.resolve()):
        return f"sensitive path cannot be a workspace root: {p}"
    if p.is_relative_to(data_home().resolve()):
        return f"directory under the data home cannot be a workspace root: {p}"
    if require_dir:
        if not p.exists():
            return f"missing path: {p}"
        if not p.is_dir():
            return f"not a directory: {p}"
    return None


@dataclass(frozen=True)
class PathContext:
    """Sandbox roots + safe dirs for one agent/session."""

    primary: Path
    secondary: list[Path] = field(default_factory=list)
    extra_safe_dirs: list[Path] = field(default_factory=list)
    sandbox_mode: str = SANDBOX_WORKSPACE_WRITE

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

    def writable_roots(self) -> list[Path]:
        """Primary, bound secondary roots, temp, data dir, and explicit extra roots."""
        out: list[Path] = [*self.roots]
        for d in [data_home(), Path(tempfile.gettempdir()), *self.extra_safe_dirs]:
            resolved = d.resolve()
            if not any(resolved.is_relative_to(p) or p.is_relative_to(resolved) for p in out):
                out.append(resolved)
        return out

    def safe_dirs(self) -> list[Path]:
        """Backward-compatible alias for writable roots."""
        return self.writable_roots()

    def protected_paths(self) -> list[Path]:
        """Write-level protected boundaries (P0-2).

        Generated per writable *authorization* root — primary, secondary, and
        ``extra_safe_dirs`` — as ``<root>/.git`` and ``<root>/.easycode``, plus
        the credential files (always blocked, even before they exist). The tool
        data dir (``data_home``) and the OS temp dir are deliberately NOT keyed
        here as their own ``.git``/``.easycode`` so tool-owned data (e.g.
        ``~/.easycode/sessions``) stays usable; temporary approval grant roots
        add their own ``.git``/``.easycode`` at the sandbox prompt.
        """
        seen: set[Path] = set()
        out: list[Path] = []
        for root in [*self.roots, *self.extra_safe_dirs]:
            for sub in (root / ".git", root / ".easycode"):
                resolved = sub.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    out.append(resolved)
        cred = data_home() / "credentials.json"
        for secret in (cred, cred.with_suffix(".tmp")):
            resolved = secret.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out

    def is_protected(self, path: Path) -> bool:
        """Tool secrets that must never be read, written, or listed.

        Unlike ``protected_paths`` (the write sandbox boundaries such as
        ``.git``), these paths are blocked across *every* tool operation —
        read, write, and enumeration — and cannot be bypassed by
        ``force_allowed`` (approval never grants credential access).
        """
        p = path.resolve()
        cred = data_home() / "credentials.json"
        for secret in (cred, cred.with_suffix(".tmp")):
            sp = secret.resolve()
            if p == sp or p.is_relative_to(sp):
                return True
        return False

    def is_protected_path(self, path: Path) -> bool:
        """True when ``path`` (or an ancestor) crosses a permanent write boundary.

        Covers credentials plus every authorization root's ``.git``/``.easycode``
        (primary, secondary, extra_safe_dirs). An approval grant can never lift
        this — it is checked by the file tools and by ``grant_granted``.
        """
        p = path.resolve()
        if self.is_protected(p):
            return True
        return any(p.is_relative_to(d) for d in self.protected_paths())

    def grant_granted(self, path: Path, grant: ToolGrant) -> bool:
        """True when ``path`` may be written under a precise approval grant.

        A grant authorizes its exact writable roots only: the path must live
        under one of them and must never itself (or an ancestor) be a protected
        child — ``.git``/``.easycode`` of any workspace/extra root, or a
        credential path. The check walks the path against every protected
        boundary, not just the grant root's own children (P0-2).
        """
        p = path.resolve()
        if self.is_protected_path(p):
            return False
        for root in grant.writable_roots:
            r = root.resolve()
            if p.is_relative_to(r):
                return True
        return False

    def in_allowed(self, path: Path) -> bool:
        """True if ``path`` is writable without sandbox escalation."""
        p = path.resolve()
        if self.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS:
            return True
        if self.sandbox_mode == SANDBOX_READ_ONLY:
            return False
        if self.sandbox_mode != SANDBOX_WORKSPACE_WRITE:
            return False
        if any(p.is_relative_to(d.resolve()) for d in self.protected_paths()):
            return False
        return any(p.is_relative_to(d) for d in self.writable_roots())

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
