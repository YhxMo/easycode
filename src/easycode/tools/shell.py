"""Shell execution tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field


class ExecuteShellArgs(BaseModel):
    command: str = Field(description="shell command to run. Uses shell=True so pipes/globs work.")
    timeout: int = Field(120, description="timeout in seconds", ge=1, le=600)


MAX_OUTPUT_CHARS = 20_000


def execute_shell(args: ExecuteShellArgs, *, root: Path) -> str:
    try:
        proc = subprocess.run(
            args.command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return json_out("timeout", {"command": args.command[:200]})
    except OSError as exc:
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