"""macOS Seatbelt command construction for model-originated processes."""

from __future__ import annotations

import sys
from pathlib import Path

from easycode.credentials import data_home
from easycode.permissions.boundary import (
    CONFIG_FILENAME,
    DATA_HOME_STATE_DIRS,
    PathContext,
    ToolGrant,
    secret_paths,
)

SEATBELT_EXECUTABLE = Path("/usr/bin/sandbox-exec")

BASE_POLICY = """(version 1)
(deny default)
(allow file-read*)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix*)
(allow file-write-data (literal "/dev/null"))
{network_policy}
{write_policy}
{protected_policy}
{secret_policy}
"""


def _seatbelt_literal(path: Path) -> str:
    """Quote a path for use inside a Seatbelt ``(literal ...)``/``(subpath ...)`` rule."""
    s = str(path.resolve())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _state_deny_policy(include_write: bool) -> str:
    """Deny reads (and writes) of data-home state dirs + credential files.

    Used when a workspace root lives INSIDE the data dir (a managed git
    worktree): the worktree itself must stay usable, but session records,
    global agent/skill/command definitions, and credentials must not become
    reachable just because the worktree is. ``deny`` always wins over
    ``allow`` in Seatbelt, so listing the state dirs is enough.
    """
    targets = [data_home() / sub for sub in DATA_HOME_STATE_DIRS]
    targets.extend(secret_paths())
    out: list[str] = []
    for p in targets:
        lit = _seatbelt_literal(p)
        out.append(f"(deny file-read* (subpath {lit}))")
        if include_write:
            out.append(f"(deny file-write* (subpath {lit}))")
    return "\n".join(out)


def _secret_policy(include_write: bool, *, protect_all_data_home: bool = True) -> str:
    """Deny reads (and, when ``include_write``, writes) of the application data
    dir, or — when a workspace root lives INSIDE the data dir (a git worktree
    under ``~/.easycode/worktrees``) — only the data-home state dirs and
    credential files, so that worktree can still be used while sessions and
    credentials stay off-limits.

    In Seatbelt ``deny`` always wins over ``allow``, so a narrower ``allow``
    cannot carve a worktree out of a blanket data-home denial; the caller narrows
    the *denial* itself instead.

    ``(allow file-read*)`` in the base policy is the confirmed gap — a child
    could cat ``~/.easycode/credentials.json``. We close it by denying the whole
    data dir for model-originated processes. ``include_write`` is set whenever
    the sandbox hands out any file-write allowance, so the data dir is never a
    shell write target either.
    """
    if not protect_all_data_home:
        return _state_deny_policy(include_write)
    lit = _seatbelt_literal(data_home())
    out = [f"(deny file-read* (subpath {lit}))"]
    if include_write:
        out.append(f"(deny file-write* (subpath {lit}))")
    return "\n".join(out)


def _root_in_data_home(ctx: PathContext) -> bool:
    """True when any of the context's roots lives under the data home."""
    data = data_home().resolve()
    return any(
        r.resolve().is_relative_to(data) for r in [*ctx.roots, *ctx.extra_safe_dirs]
    )


def sandbox_command(
    command: list[str],
    ctx: PathContext,
    *,
    grant: ToolGrant | None = None,
) -> list[str]:
    """Apply workspace isolation, with explicit network and write grants.

    ``danger-full-access`` wraps nothing: the preset means the child runs as an
    ordinary host process, so Seatbelt's default-deny profile — and with it the
    data-home firewall — is not applied at all. ``child_env`` still sanitizes
    the environment, because that protects the parent's secrets rather than
    bounding the child.
    """
    if ctx.full_access:
        return command
    if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
        raise RuntimeError("workspace sandbox is currently supported only on macOS")

    protect_all = not _root_in_data_home(ctx)
    writable = list(ctx.writable_roots())
    if grant:
        for r in grant.writable_roots:
            rp = r.resolve()
            if not any(rp == w or rp.is_relative_to(w) or w.is_relative_to(rp) for w in writable):
                writable.append(rp)
    write_rules = [
        f'(allow file-write* (subpath (param "WRITABLE_ROOT_{i}")))' for i in range(len(writable))
    ]

    protected = list(ctx.protected_paths())
    if grant:
        for r in grant.writable_roots:
            rp = r.resolve()
            for sub in (rp / ".git", rp / ".easycode", rp / CONFIG_FILENAME):
                if not any(sub == q or sub.is_relative_to(q) for q in protected):
                    protected.append(sub)
    protected_rules = [
        f'(deny file-write* (subpath (param "PROTECTED_ROOT_{i}")))' for i in range(len(protected))
    ]
    network_policy = "(allow network*)" if (grant and grant.network_allowed) else ""
    policy = BASE_POLICY.format(
        write_policy="\n".join(write_rules),
        protected_policy="\n".join(protected_rules),
        network_policy=network_policy,
        # The data dir is listed in ``writable_roots`` (so the app's own
        # Python writing of sessions/credentials is unaffected), but the shell
        # sandbox adds a write-deny for it — otherwise a model-originated shell
        # could silently create/delete ``~/.easycode/sessions/*`` without
        # approval, bypassing SessionStore's atomic replacement. When a worktree
        # root lives INSIDE the data dir, the denial is narrowed to the
        # data-home state dirs + credentials so the worktree stays writable by
        # its own setup script without exposing sessions or global extensions.
        secret_policy=_secret_policy(
            include_write=True, protect_all_data_home=protect_all
        ),
    )
    args = [str(SEATBELT_EXECUTABLE), "-p", policy]
    args.extend(f"-DWRITABLE_ROOT_{i}={path.resolve()}" for i, path in enumerate(writable))
    args.extend(f"-DPROTECTED_ROOT_{i}={path.resolve()}" for i, path in enumerate(protected))
    return [*args, "--", *command]
