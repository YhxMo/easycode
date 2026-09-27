"""Path ownership: classify filesystem paths into sandbox categories.

Categories:

- ``workspace``: under the primary or secondary working directories
- ``temp``: under the system temporary directory (``tempfile.gettempdir()``)
- ``system``: under the data dir (``~/.easycode/``, sessions/credentials...)
- ``external``: anything else

Everything but ``external`` is exempt from approval prompts (plus any
``extra_safe_dirs``). The approval layer uses the same context.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from easycode.credentials import data_home
from easycode.policy import SANDBOX_DANGER_FULL_ACCESS, SANDBOX_WORKSPACE_WRITE

#: Project configuration filename (protected inside every workspace root).
CONFIG_FILENAME = "easycode.config.json"

#: ``data_home()`` subdirectories holding cross-session state (session records,
#: agent/skill/command definitions). Writes there need approval — the user can
#: still authorize editing their own extensions, but nothing changes silently.
DATA_HOME_STATE_DIRS = ("sessions", "agents", "skills", "commands")


@dataclass(frozen=True)
class ToolGrant:
    """Precise, single-call authorization conveyed by an approval.

    An approval states *what* it grants — network and/or a set of explicit external
    writable roots — instead of one anonymous flag. The roots are already
    canonical (absolute, resolved) and validated; each is added to the sandbox
    as its own ``-D`` parameter + subpath rule, and only for this one call.
    Nothing here is ever written back to Agent/Session/Config.
    """

    network_allowed: bool = False
    writable_roots: tuple[Path, ...] = ()


def secret_paths() -> tuple[Path, Path]:
    """The credential file and its atomic-write temp sibling (always blocked)."""
    cred = data_home() / "credentials.json"
    return cred, cred.with_suffix(".tmp")


def resolve_workspace_path(raw: str | Path, base: Path | None = None) -> Path:
    """Resolve a configured workspace path to an absolute canonical path.

    Relative paths are anchored to ``base`` (the config-file directory) when
    given, otherwise to CWD — callers that must not drift (Web/Session) pass an
    explicit absolute ``base`` so a relative path is never silently resolved
    against the process CWD.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        anchor = base or Path.cwd()
        p = anchor / p
    return p.resolve()


def _normalise_roots(
    raw: list[str] | None, base: Path | None, *, allow_relative: bool
) -> tuple[list[Path], str | None]:
    """Resolve + validate + de-duplicate a root list.

    Returns ``(paths, error)``; when ``error`` is non-None the caller must fail
    closed. Every entry must be an existing, canonical, non-sensitive directory.
    Relative entries resolve against ``base`` when ``allow_relative``; otherwise
    they are rejected outright — a shell's explicit writable root must never be
    resolved against a drifting CWD.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for raw_item in raw or []:
        s = str(raw_item).strip()
        if not s:
            continue
        if allow_relative:
            p = resolve_workspace_path(s, base)
        else:
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


def validate_writable_roots(raw: list[str], base: Path | None) -> tuple[list[Path], str | None]:
    """Strict validation for a shell tool's explicit ``writable_roots``."""
    return _normalise_roots(raw, base, allow_relative=False)


def normalise_secondary(
    secondary: list[str] | None, base: Path | None
) -> tuple[list[Path], str | None]:
    """Resolve + validate + de-duplicate a secondary/extra_safe list."""
    return _normalise_roots(secondary, base, allow_relative=True)


def sensitive_ancestor(path: Path) -> Path | None:
    """The nearest ``.git``/``.easycode`` ancestor of a candidate root, if any.

    The data home is itself named ``.easycode`` but may host permanent git
    worktrees, so it is not treated as a sensitive ancestor; everything under
    it that is not an allowed worktree is rejected by ``root_error`` itself.
    """
    data = data_home().resolve()
    for anc in [path, *path.parents]:
        if anc.name == ".git":
            return anc
        if anc.name == ".easycode" and anc != data:
            return anc
    return None


def root_error(path: Path, *, require_dir: bool = True) -> str | None:
    """Validation for a workspace/temporary root candidate.

    Returns a human-readable error string when ``path`` is illegal as a root,
    else ``None``. A legal root must be absolute+canonical, must exist (unless
    ``require_dir`` is False), must be a directory (not a plain file), and must
    never be a sensitive dir (``.git``/``.easycode``/credentials) itself or a
    descendant of one, nor live under the data home (permanent worktrees under
    ``data_home()/worktrees/<name>`` stay allowed).
    """
    p = path.resolve()
    sensitive = sensitive_ancestor(p)
    if sensitive is not None:
        return f"sensitive directory cannot be a workspace root: {p} (inside {sensitive})"
    cred = secret_paths()[0].resolve()
    if p == cred or p.is_relative_to(cred):
        return f"sensitive path cannot be a workspace root: {p}"
    if p.is_relative_to(data_home().resolve()):
        # Permanent git worktrees live under the data dir by design and must
        # remain valid workspace roots; the container itself and every other
        # data-home path are rejected.
        worktrees = (data_home() / "worktrees").resolve()
        if p == worktrees or not p.is_relative_to(worktrees):
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
        """Primary, bound secondary roots, temp, data dir, and explicit extra roots.

        A candidate that already sits inside one of these roots is skipped: that
        root's ``subpath`` rule already covers it. The reverse direction is not
        coverage — allowing a root never allows its parent — so a candidate that
        merely *contains* a root is still listed, with one exception: the data
        dir is dropped when a root lives inside it, so that a managed worktree
        cannot hand out its siblings. The sandbox narrows its data-dir write deny
        for that case instead (see ``_secret_policy``).
        """
        out: list[Path] = [*self.roots]
        data = data_home().resolve()
        for d in [data, Path(tempfile.gettempdir()), *self.extra_safe_dirs]:
            resolved = d.resolve()
            if any(resolved.is_relative_to(p) for p in out):
                continue
            if resolved == data and any(p.is_relative_to(data) for p in out):
                continue
            out.append(resolved)
        return out

    def protected_paths(self) -> list[Path]:
        """Write-level protected boundaries.

        Generated per writable *authorization* root — primary, secondary, and
        ``extra_safe_dirs`` — as ``<root>/.git``, ``<root>/.easycode``, and the
        project ``easycode.config.json``, plus the credential files (always
        blocked, even before they exist). The tool data dir (``data_home``) and
        the OS temp dir are deliberately NOT keyed here as their own
        ``.git``/``.easycode`` so tool-owned data stays usable; writes to the
        data-home *state* dirs require approval instead (see ``in_allowed``).
        Temporary approval grant roots add their own boundaries at the sandbox
        prompt.
        """
        seen: set[Path] = set()
        out: list[Path] = []
        for root in [*self.roots, *self.extra_safe_dirs]:
            for sub in (root / ".git", root / ".easycode", root / CONFIG_FILENAME):
                resolved = sub.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    out.append(resolved)
        for secret in secret_paths():
            resolved = secret.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out

    @property
    def full_access(self) -> bool:
        """True under ``danger-full-access``: no boundary but the OS user's own."""
        return self.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS

    def is_protected(self, path: Path) -> bool:
        """Tool secrets that must never be read, written, or listed.

        Unlike ``protected_paths`` (the write sandbox boundaries such as
        ``.git``), these paths are blocked across *every* tool operation —
        read, write, and enumeration — and cannot be bypassed by
        an approval grant (approval never grants credential access).

        ``danger-full-access`` removes this firewall: the user asked for the
        whole host, credentials included, and an in-process rule they did not
        ask for would only be a hidden landmine. Explicit ``permission_rules``
        remain the way to keep a tool away from them.
        """
        if self.full_access:
            return False
        p = path.resolve()
        for secret in secret_paths():
            sp = secret.resolve()
            if p == sp or p.is_relative_to(sp):
                return True
        return False

    def is_protected_path(self, path: Path) -> bool:
        """True when ``path`` (or an ancestor) crosses a permanent write boundary.

        Covers credentials plus every authorization root's
        ``.git``/``.easycode``/``easycode.config.json`` (primary, secondary,
        extra_safe_dirs). An approval grant can never lift this — it is checked
        by the file tools and by ``grant_granted`` — but the full-access preset
        does, since it is the user's own decision to drop the boundary rather
        than an approval for one call.
        """
        if self.full_access:
            return False
        p = path.resolve()
        if self.is_protected(p):
            return True
        return any(p.is_relative_to(d) for d in self.protected_paths())

    def is_model_state(self, path: Path) -> bool:
        """True for data-home state dirs whose writes require approval."""
        if self.full_access:
            return False
        dh = data_home().resolve()
        return any(path.is_relative_to(dh / sub) for sub in DATA_HOME_STATE_DIRS)

    def grant_granted(self, path: Path, grant: ToolGrant) -> bool:
        """True when ``path`` may be written under a precise approval grant.

        A grant authorizes its exact writable roots only: the path must live
        under one of them and must never itself (or an ancestor) be a protected
        child — ``.git``/``.easycode``/``easycode.config.json`` of any
        workspace/extra root *or of the grant root itself* — or a credential
        path. The check walks the path against every protected boundary, not
        just the grant root's own children.
        """
        p = path.resolve()
        if self.is_protected_path(p):
            return False
        for root in grant.writable_roots:
            r = root.resolve()
            if any(
                p == sub or p.is_relative_to(sub)
                for sub in (r / ".git", r / ".easycode", r / CONFIG_FILENAME)
            ):
                return False
            if p.is_relative_to(r):
                return True
        return False

    def in_allowed(self, path: Path) -> bool:
        """True if ``path`` is writable without sandbox escalation."""
        p = path.resolve()
        if self.full_access:
            return True
        if self.sandbox_mode != SANDBOX_WORKSPACE_WRITE:
            return False
        if self.is_protected_path(p) or self.is_model_state(p):
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
