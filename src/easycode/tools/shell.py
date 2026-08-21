"""Shell execution tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from easycode.sandbox import sandbox_command
from easycode.workspace import PathContext


class ExecuteShellArgs(BaseModel):
    command: str = Field(description="shell command to run; pipes, redirects, and globs are supported")
    timeout: int = Field(120, description="timeout in seconds", ge=1, le=600)
    sandbox_permissions: str = Field(
        "use_default",
        description="use_default or require_escalated; escalation requires approval",
        pattern="^(use_default|require_escalated)$",
    )
    justification: str | None = Field(None, description="why sandbox escalation is required")


MAX_OUTPUT_CHARS = 20_000


def execute_shell(
    args: ExecuteShellArgs,
    *,
    root: Path,
    ctx: PathContext | None = None,
    force_allowed: bool = False,
) -> str:
    scope = ctx or PathContext(primary=root)
    try:
        command = sandbox_command(
            ["/bin/sh", "-c", args.command], scope, force_allowed=force_allowed
        )
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return json_out("timeout", {"command": args.command[:200]})
    except (OSError, RuntimeError) as exc:
        return json_out("error", {"message": str(exc)})

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    payload = {
        "exit_code": proc.returncode,
        "stdout": stdout[-MAX_OUTPUT_CHARS:],
        "stderr": stderr[-MAX_OUTPUT_CHARS:],
    }
    if len(stdout) > MAX_OUTPUT_CHARS or len(stderr) > MAX_OUTPUT_CHARS:
        payload["truncated"] = True
    return json_out("ok", payload)


def json_out(status: str, payload: dict) -> str:
    import json

    return json.dumps({"status": status, **payload}, ensure_ascii=False, default=str)
