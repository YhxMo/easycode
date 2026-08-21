"""YAML frontmatter parsing for .md spec files (agents / skills / commands).

Format (opencode / Claude Code style)::

    ---
    name: my-agent
    description: What it does
    model: deepseek/deepseek-v4-flash
    ---

    Body text (system prompt / instructions)…
"""

from __future__ import annotations

import re
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a declared dependency
    yaml = None  # type: ignore[assignment]

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


class FrontmatterError(ValueError):
    pass


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into (metadata dict, body). Metadata may be empty."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    meta: dict[str, Any] = {}
    if raw.strip():
        if yaml is None:
            raise FrontmatterError("PyYAML is required to parse frontmatter")
        try:
            parsed = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
        if isinstance(parsed, dict):
            meta = parsed
    body = text[m.end():].lstrip("\n")
    return meta, body


def parse_spec(
    text: str,
    *,
    default_name: str,
    required: tuple[str, ...] = ("description",),
) -> tuple[dict[str, Any], str]:
    """Parse a spec file, enforcing required frontmatter fields."""
    meta, body = parse_frontmatter(text)
    meta.setdefault("name", default_name)
    missing = [f for f in required if not str(meta.get(f) or "").strip()]
    if missing:
        raise FrontmatterError(
            f"missing required frontmatter field(s) in '{default_name}': {', '.join(missing)}"
        )
    return meta, body