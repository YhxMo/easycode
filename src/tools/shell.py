"""Shell execution tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from easycode.permissions.approval import destructive_command_reason
from easycode.permissions.boundary import PathContext, ToolGrant, validate_writable_roots
from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS
from easycode.permissions.sandbox import child_env, sandbox_command
from easycode.tools.registry import json_out, tool_scope


class ExecuteShellArgs(BaseModel):
    command: str = Field(
        description="shell command to run; pipes, redirects, and globs are supported"
    )
    timeout: int = Field(120, description="timeout in seconds", ge=1, le=600)
    sandbox_permissions: str = Field(
        "use_default",
        description="use_default or require_escalated; escalation requires approval",
        pattern="^(use_default|require_escalated)$",
    )
    justification: str | None = Field(
        None,
        description="why this command needs sandbox escalation / external-write access",
    )
    writable_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit absolute directories this command may write to outside the "
            "workspace. Each must be an existing absolute directory (never a file, "
            "missing path, or .git/.easycode). Only list directories the task truly "
            "needs; secondary/extra workspace directories do NOT need to be listed. "
            "The command string is never parsed to infer these."
        ),
    )


MAX_OUTPUT_CHARS = 20_000


def execute_shell(
    args: ExecuteShellArgs,
    *,
    root: Path,
    ctx: PathContext | None = None,
    grant: ToolGrant | None = None,
) -> str:
    scope = tool_scope(root, ctx)
    # Under the sandboxed presets (ask / auto-review) a clearly destructive
    # command (rm -rf /, git reset --hard, git clean, git push --force) is
    # denied outright, before any validation or sandbox, and cannot be
    # re-enabled by a grant or an approval. Under danger-full-access
    # (allow-all) the denylist and the writable_roots gate are both off —
    # the sandbox and approvals are already gone there (Codex-aligned);
    # permission_rules remain the way to selectively forbid commands.
    full_access = scope.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS
    if not full_access:
        deny = destructive_command_reason(args.command)
        if deny:
            return json_out(
                "error",
                {
                    "message": f"tool call rejected: execute_shell: {deny}",
                    "rejected": True,
                    "category": "destructive",
                    "reason": deny,
                    "in_allowed": False,
                },
            )
        # Validate the model's explicit writable_roots declaration: invalid
        # entries (relative / missing / plain file / .git / .easycode / data home)
        # fail closed with a structured error instead of silently dropping them.
        declared, err = validate_writable_roots(args.writable_roots, None)
        if err:
            return json_out(
                "error",
                {"message": f"invalid writable_roots: {err}", "in_allowed": False},
            )
        # Never trust the model's own declaration: it is only honored when an
        # approval grant covers it. Any declared root the grant does NOT cover is
        # rejected, so a shell can never write outside the workspace unprompted.
        if declared:
            granted = {p.resolve() for p in (grant.writable_roots if grant else ())}
            missing = [str(p) for p in declared if p not in granted]
            if missing:
                return json_out(
                    "error",
                    {
                        "message": f"writable_roots not granted by approval: {', '.join(missing)}",
                        "in_allowed": False,
                    },
                )
    try:
        command = sandbox_command(
            ["/bin/sh", "-c", args.command],
            scope,
            grant=grant,
        )
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=args.timeout,
            env=child_env(),
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
