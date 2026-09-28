"""Platform sandbox launch helpers."""

from __future__ import annotations

import os
import re

from easycode.permissions.sandbox.macos import sandbox_command

__all__ = ["child_env", "sandbox_command"]

#: Environment variable names treated as secret-bearing and stripped from
#: model-originated child processes by default (a conservative child env policy).
#: ``${var}`` markers such as ``AWS_SECRET_ACCESS_KEY``, ``OPENAI_API_KEY``,
#: ``GITHUB_TOKEN``, ``DB_PASSWORD`` all match.
SECRET_ENV_RE = re.compile(
    r"api[_-]?key|access[_-]?key|secret|password|passwd|token|credential|"
    r"auth|private|bearer|signing|_key$|_secret$|_token$|_passw?d$",
    re.IGNORECASE,
)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Conservative child environment for model-originated subprocesses.

    Shell and stdio MCP child processes must not inherit secret-bearing
    environment variables by default (``OPENAI_API_KEY``, ``AWS_SECRET_ACCESS_KEY``,
    ``GITHUB_TOKEN``, ``DB_PASSWORD``, …). Everything else (``PATH``, ``HOME``,
    locale, ``TMPDIR``) is preserved so the process still works; explicit MCP
    ``env`` configuration is layered on top and wins on collisions.
    """
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if SECRET_ENV_RE.search(k):
            continue
        out[k] = v
    if extra:
        out.update(extra)
    return out
