"""macOS Seatbelt command construction for model-originated processes."""

from __future__ import annotations

import sys
from pathlib import Path

from easycode.credentials import data_home
from easycode.policy import SANDBOX_DANGER_FULL_ACCESS, SANDBOX_READ_ONLY
from easycode.workspace import PathContext, ToolGrant

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

#: allow-all (``danger-full-access``) still keeps the secret firewall: the
#: sandbox relaxes file writes, reads and network, but the application data dir
#: (``~/.easycode`` — credentials, sessions) remains off-limits to child procs.
FULL_ACCESS_POLICY = """(version 1)
(deny default)
(allow file-read*)
(allow file-write*)
(allow network*)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix*)
{secret_policy}
"""


def _seatbelt_literal(path: Path) -> str:
    """Quote a path for use inside a Seatbelt ``(literal ...)``/``(subpath ...)`` rule."""
    s = str(path.resolve())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _secret_policy(include_write: bool, *, protect_all_data_home: bool = True) -> str:
    """Deny reads (and, when ``include_write``, writes) of the application data
    dir, or — when a workspace root lives INSIDE the data dir (a git worktree
    under ``~/.easycode/worktrees``) — just the credential files, so that
    worktree can still be read by its own setup script while ``credentials.json``
    stays off-limits.

    In Seatbelt ``deny`` always wins over ``allow``, so a narrower ``allow``
    cannot carve a worktree out of a blanket data-home denial; the caller narrows
    the *denial* itself instead.

    ``(allow file-read*)`` in the base policy is the confirmed gap — a child
    could cat ``~/.easycode/credentials.json``. We close it by denying the whole
    data dir for model-originated processes. ``include_write`` is set whenever
    the sandbox hands out any file-write allowance (``danger-full-access`` and
    the normal workspace-write path alike), so the data dir is never a shell
    write target either.
    """
    if not protect_all_data_home:
        creds = data_home() / "credentials.json"
        out: list[str] = []
        for p in (creds, creds.with_suffix(".tmp")):
            lit = _seatbelt_literal(p)
            out.append(f"(deny file-read* (literal {lit}))")
            if include_write:
                out.append(f"(deny file-write* (literal {lit}))")
        return "\n".join(out)
    lit = _seatbelt_literal(data_home())
    out = [f"(deny file-read* (subpath {lit}))"]
    if include_write:
        out.append(f"(deny file-write* (subpath {lit}))")
    return "\n".join(out)


def sandbox_command(
    command: list[str],
    ctx: PathContext,
    *,
    grant: ToolGrant | None = None,
) -> list[str]:
    """Apply workspace isolation, with explicit network and write grants."""
    if ctx.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS:
        # allow-all: full access, but retain the secret firewall.
        if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
            return command  # no Seatbelt available; env sanitization still applies
        policy = FULL_ACCESS_POLICY.format(secret_policy=_secret_policy(include_write=True))
        args = [str(SEATBELT_EXECUTABLE), "-p", policy]
        return [*args, "--", *command]
    if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
        raise RuntimeError("workspace sandbox is currently supported only on macOS")

    writable = [] if ctx.sandbox_mode == SANDBOX_READ_ONLY else list(ctx.writable_roots())
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
            for sub in (rp / ".git", rp / ".easycode"):
                if not any(sub == q or sub.is_relative_to(q) for q in protected):
                    protected.append(sub)
    protected_rules = [
        f'(deny file-write* (subpath (param "PROTECTED_ROOT_{i}")))' for i in range(len(protected))
    ]
    network_policy = "(allow network*)" if (grant and grant.network_allowed) else ""
    # A workspace root that lives inside the data dir (a git worktree created
    # under ~/.easycode/worktrees) must stay readable by the sandboxed process,
    # so the blanket data-home read denial is narrowed to just the credentials.
    data_home_resolved = data_home().resolve()
    any_root_in_data_home = any(
        r.resolve().is_relative_to(data_home_resolved) for r in [*ctx.roots, *ctx.extra_safe_dirs]
    )
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
        # credential files so the worktree stays writable by its own setup script.
        secret_policy=_secret_policy(
            include_write=True, protect_all_data_home=not any_root_in_data_home
        ),
    )
    args = [str(SEATBELT_EXECUTABLE), "-p", policy]
    args.extend(f"-DWRITABLE_ROOT_{i}={path.resolve()}" for i, path in enumerate(writable))
    args.extend(f"-DPROTECTED_ROOT_{i}={path.resolve()}" for i, path in enumerate(protected))
    return [*args, "--", *command]
