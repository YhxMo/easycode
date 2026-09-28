"""Per-model credentials stored in ``~/.easycode/credentials.json``.

The file is local-only, chmod 600, and never belongs in the project tree.
Each model owns one credential record; API format is model metadata and is
intentionally not part of credential lookup.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from easycode.paths import DATA_DIR_NAME, data_home

CREDENTIALS_FILENAME = "credentials.json"


@dataclass
class Credential:
    key_id: str
    api_key: str
    base_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            out["base_url"] = self.base_url
        return out

    def masked(self) -> dict[str, Any]:
        """Public view: no api key, only a tail-hint for recognition."""
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else ""
        return {"key_id": self.key_id, "key_tail": tail, "base_url": self.base_url}


def new_credential_id() -> str:
    """Return a stable, alias-independent id for a new model credential."""
    return f"model-{uuid4().hex}"


def credentials_path(home: Path | None = None) -> Path:
    if home is not None:
        return home / DATA_DIR_NAME / CREDENTIALS_FILENAME
    return data_home() / CREDENTIALS_FILENAME


def load_credentials(path: Path | None = None) -> dict[str, Credential]:
    """Load all credentials; missing/unreadable/corrupt file → empty dict."""
    p = path or credentials_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, Credential] = {}
    for key_id, entry in (raw or {}).items():
        if not isinstance(entry, dict) or (
            not entry.get("api_key") and not entry.get("base_url")
        ):
            continue
        out[str(key_id)] = Credential(
            key_id=str(key_id),
            api_key=str(entry.get("api_key") or ""),
            base_url=entry.get("base_url"),
        )
    return out


def _write(creds: dict[str, Credential], path: Path) -> None:
    payload = {k: c.to_dict() for k, c in creds.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_credential(cred: Credential, path: Path | None = None) -> None:
    p = path or credentials_path()
    creds = load_credentials(p)
    creds[cred.key_id] = cred
    _write(creds, p)


def delete_credential(key_id: str, path: Path | None = None) -> bool:
    p = path or credentials_path()
    creds = load_credentials(p)
    if key_id not in creds:
        return False
    del creds[key_id]
    _write(creds, p)
    return True
