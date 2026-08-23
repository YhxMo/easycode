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


def _secret_policy(include_write: bool) -> str:
    """Deny reads of the application data dir (credentials live there).

    ``(allow file-read*)`` in the base policy is the confirmed gap — a child
    could cat ``~/.easycode/credentials.json``. We close it by denying the whole
    data dir for model-originated processes. Write is included when the caller
    provides no separate write protection (the ``danger-full-access`` policy).
    """
    lit = _seatbelt_literal(data_home())
    out = [f"(deny file-read* (subpath {lit}))"]
    if include_write:
        out.append(f"(deny file-write* (subpath {lit}))")
    return "\n".join(out)


def sandbox_command(
    command: list[str],
    ctx: PathContext,
    *,
    force_allowed: bool = False,
    grant: ToolGrant | None = None,
) -> list[str]:
    """Wrap a command in Seatbelt; non-macOS callers fail closed upstream.

    ``force_allowed`` is a legacy alias: it is folded into ``grant`` as a
    network-only relaxation so existing callers keep working. The precise grant
    (P0-1) separates the two dimensions — ``grant.network_allowed`` enables
    network, while ``grant.writable_roots`` adds *specific* external writable
    directories (each its own ``-D`` + subpath rule). File-write and process-exec
    limits are always retained; only ``danger-full-access`` disables the sandbox
    almost entirely — and even then the application data dir (``~/.easycode``)
    stays off-limits so credentials cannot be read or written by a child.
    """
    if ctx.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS:
        # allow-all: full access, but retain the secret firewall.
        if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
            return command  # no Seatbelt available; env sanitization still applies
        policy = FULL_ACCESS_POLICY.format(secret_policy=_secret_policy(include_write=True))
        args = [str(SEATBELT_EXECUTABLE), "-p", policy]
        return [*args, "--", *command]
    if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
        raise RuntimeError("workspace sandbox is currently supported only on macOS")

    if grant is None and force_allowed:
        grant = ToolGrant(network_allowed=True)

    writable = [] if ctx.sandbox_mode == SANDBOX_READ_ONLY else list(ctx.writable_roots())
    if grant:
        for r in grant.writable_roots:
            rp = r.resolve()
            if not any(rp == w or rp.is_relative_to(w) or w.is_relative_to(rp) for w in writable):
                writable.append(rp)
    write_rules = ["(allow file-write* (subpath (param \"WRITABLE_ROOT_%d\")))" % i for i in range(len(writable))]

    protected = list(ctx.protected_paths())
    if grant:
        for r in grant.writable_roots:
            rp = r.resolve()
            for sub in (rp / ".git", rp / ".easycode"):
                if not any(sub == q or sub.is_relative_to(q) for q in protected):
                    protected.append(sub)
    protected_rules = [
        "(deny file-write* (subpath (param \"PROTECTED_ROOT_%d\")))" % i
        for i in range(len(protected))
    ]
    network_policy = "(allow network*)" if (grant and grant.network_allowed) else ""
    policy = BASE_POLICY.format(
        write_policy="\n".join(write_rules),
        protected_policy="\n".join(protected_rules),
        network_policy=network_policy,
        secret_policy=_secret_policy(include_write=False),
    )
    args = [str(SEATBELT_EXECUTABLE), "-p", policy]
    args.extend(f"-DWRITABLE_ROOT_{i}={path.resolve()}" for i, path in enumerate(writable))
    args.extend(f"-DPROTECTED_ROOT_{i}={path.resolve()}" for i, path in enumerate(protected))
    return [*args, "--", *command]
