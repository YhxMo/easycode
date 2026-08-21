"""macOS Seatbelt command construction for model-originated processes."""

from __future__ import annotations

import sys
from pathlib import Path

from easycode.policy import SANDBOX_DANGER_FULL_ACCESS, SANDBOX_READ_ONLY
from easycode.workspace import PathContext

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
(allow file-write-data (literal \"/dev/null\"))
{write_policy}
{protected_policy}
"""


def sandbox_command(command: list[str], ctx: PathContext, *, force_allowed: bool = False) -> list[str]:
    """Wrap a command in Seatbelt; non-macOS callers fail closed upstream."""
    if force_allowed or ctx.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS:
        return command
    if sys.platform != "darwin" or not SEATBELT_EXECUTABLE.is_file():
        raise RuntimeError("workspace sandbox is currently supported only on macOS")

    writable = [] if ctx.sandbox_mode == SANDBOX_READ_ONLY else ctx.writable_roots()
    write_rules = ["(allow file-write* (subpath (param \"WRITABLE_ROOT_%d\")))" % i for i in range(len(writable))]
    protected_rules = [
        "(deny file-write* (subpath (param \"PROTECTED_ROOT_%d\")))" % i
        for i in range(len(ctx.protected_paths()))
    ]
    policy = BASE_POLICY.format(
        write_policy="\n".join(write_rules),
        protected_policy="\n".join(protected_rules),
    )
    args = [str(SEATBELT_EXECUTABLE), "-p", policy]
    args.extend(f"-DWRITABLE_ROOT_{i}={path.resolve()}" for i, path in enumerate(writable))
    args.extend(f"-DPROTECTED_ROOT_{i}={path.resolve()}" for i, path in enumerate(ctx.protected_paths()))
    return [*args, "--", *command]
