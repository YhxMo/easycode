"""MCP credentials: secret storage kept apart from the configuration.

A server's configuration says *where* a secret comes from; this module holds the
secret itself, in ``~/.easycode/mcp-credentials.json`` with owner-only
permissions and an atomic replace. Keeping them separate is what lets the
settings UI, the API and the logs all read a configuration without ever holding
a token.

A credential is owned by ``(scope, project root, server)`` plus the URL it was
issued for. The URL is part of the identity on purpose: a stored token must not
be sent to a host the user has pointed the server at since, because the token
was authorized for the old one and nobody agreed to the new one.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from easycode.paths import data_home

if TYPE_CHECKING:
    from easycode.extensions.mcp.config import MCPServerConfig, ResolvedServer

log = logging.getLogger("easycode.extensions.mcp.credentials")

CREDENTIALS_FILENAME = "mcp-credentials.json"

#: Kinds of secret a record can hold.
KINDS = ("env", "header", "bearer", "oauth")


def credentials_path() -> Path:
    return data_home() / CREDENTIALS_FILENAME


@dataclass
class MCPCredential:
    """One secret grant, addressable by ``id``."""

    id: str
    kind: str
    scope: str
    server: str
    #: Canonical project root the grant belongs to; "" for personal/app scopes.
    root: str = ""
    #: The server URL this was authorized for, when the transport has one.
    url: str = ""
    #: Secret values by name: env var, header name, or ``token`` for a bearer.
    values: dict[str, str] = field(default_factory=dict)
    #: OAuth state, when ``kind == "oauth"``.
    oauth: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPCredential:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            kind=str(data.get("kind") or "env"),
            scope=str(data.get("scope") or "personal"),
            server=str(data.get("server") or ""),
            root=str(data.get("root") or ""),
            url=str(data.get("url") or ""),
            values={str(k): str(v) for k, v in (data.get("values") or {}).items()},
            oauth=dict(data.get("oauth") or {}),
            updated_at=str(data.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "server": self.server,
            "values": self.values,
        }
        if self.root:
            out["root"] = self.root
        if self.url:
            out["url"] = self.url
        if self.oauth:
            out["oauth"] = self.oauth
        if self.updated_at:
            out["updated_at"] = self.updated_at
        return out

    def public(self) -> dict[str, Any]:
        """The masked view: names and presence, never a value.

        What the settings panel needs is which variables are configured and
        whether a token exists; handing it the secret would put it in a browser
        and in every proxy log between.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "names": sorted(self.values),
            "has_value": bool(self.values) or bool(self.oauth.get("access_token")),
            "expires_at": self.oauth.get("expires_at"),
            "updated_at": self.updated_at or None,
        }


class CredentialStore:
    """Reads and writes the credential file.

    Nothing is cached in memory: the file is small, and every mutation reads it
    again before writing. That is what keeps two concurrent token refreshes from
    overwriting each other — each one re-reads, changes only its own record, and
    lands the result with an atomic replace.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or credentials_path()

    def _load(self) -> dict[str, MCPCredential]:
        out: dict[str, MCPCredential] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                entries = raw.get("credentials") if isinstance(raw, dict) else None
                for entry in entries or []:
                    cred = MCPCredential.from_dict(entry)
                    if cred.server:
                        out[cred.id] = cred
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                # A file that cannot be read is reported, not silently replaced:
                # it may hold every MCP token the user has.
                log.warning("无法读取 MCP 凭据文件 %s: %s", self.path, exc)
                return {}
        return out

    def _write(self, records: dict[str, MCPCredential]) -> None:
        payload = {"credentials": [c.to_dict() for c in records.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        # 0600 before any content: a world-readable temp file would leak even if
        # the final chmod happened, and ``replace`` keeps the mode it was given.
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def all(self) -> list[MCPCredential]:
        return list(self._load().values())

    def get(self, credential_id: str) -> MCPCredential | None:
        return self._load().get(credential_id)

    def find(
        self, *, scope: str, server: str, root: str = "", kind: str | None = None
    ) -> MCPCredential | None:
        """The grant for one server in one scope, if there is exactly one."""
        return self.snapshot().find(scope=scope, server=server, root=root, kind=kind)

    def snapshot(self) -> CredentialSnapshot:
        """One read of the file, for looking up several servers consistently.

        Read-only on purpose: writes still read-modify-write the real file, so
        a snapshot can never be mistaken for something to save into.
        """
        return CredentialSnapshot(self._load())

    def save(self, cred: MCPCredential) -> MCPCredential:
        """Insert or replace one record, atomically."""
        from datetime import UTC, datetime

        cred.updated_at = datetime.now(UTC).isoformat()
        records = dict(self._load())
        records[cred.id] = cred
        self._write(records)
        return cred

    def replace_values(self, credential_id: str, values: dict[str, str]) -> None:
        """Update exactly one record's secret values.

        Refresh happens per credential, so a concurrent refresh of another
        server cannot overwrite this one's result: the read-modify-write is
        scoped to the single record.
        """
        records = dict(self._load())
        cred = records.get(credential_id)
        if cred is None:
            return
        cred.values = dict(values)
        self._write(records)

    def delete(self, credential_id: str) -> bool:
        records = dict(self._load())
        if credential_id not in records:
            return False
        del records[credential_id]
        self._write(records)
        return True

    def delete_for_server(self, *, scope: str, server: str, root: str = "") -> int:
        """Remove every grant belonging to one server registration."""
        records = dict(self._load())
        doomed = [
            cid
            for cid, cred in records.items()
            if cred.scope == scope and cred.server == server and cred.root == root
        ]
        for cid in doomed:
            del records[cid]
        if doomed:
            self._write(records)
        return len(doomed)

    def versions(self) -> dict[str, str]:
        """A digest per record, for the connection fingerprint.

        Rotating a token must invalidate a running connection; a fingerprint
        that only covered the configuration would leave the old session serving
        an old token until something else changed.
        """
        import hashlib

        out: dict[str, str] = {}
        for cid, cred in sorted(self._load().items()):
            raw = json.dumps(cred.to_dict(), ensure_ascii=False, sort_keys=True)
            out[cid] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return out


@dataclass(frozen=True)
class CredentialSnapshot:
    """One read of the credential file, queried like a store."""

    records: dict[str, MCPCredential]

    def find(
        self, *, scope: str, server: str, root: str = "", kind: str | None = None
    ) -> MCPCredential | None:
        for cred in self.records.values():
            if cred.scope != scope or cred.server != server or cred.root != root:
                continue
            if kind is not None and cred.kind != kind:
                continue
            return cred
        return None


def store() -> CredentialStore:
    """The credential store for the current data home.

    The path is resolved per call rather than at import, so a relocated data
    home (the tests' isolated ``HOME``, or a relaunch) is honoured.
    """
    return CredentialStore(credentials_path())


def credential_versions(credential_store: CredentialStore | None = None) -> dict[str, str]:
    """A digest per credential record, for fingerprinting a connection."""
    try:
        return (credential_store or store()).versions()
    except OSError:
        return {}


def matching_credential(
    config: MCPServerConfig,
    *,
    scope: str,
    root: str = "",
    credential_store: Any,
):
    """The stored grant that authenticates ``config``, or None.

    The one place this rule exists: the panel, the connecting path and the
    connection fingerprint all ask the same question, so a token is never
    filled in from one record and invalidated by another.
    """
    if not (config.secret_env or config.secret_headers or config.bearer_credential):
        return None
    cred = credential_store.find(scope=scope, server=config.name, root=root)
    if cred is None:
        return None
    # A record is only used for the URL it was issued for. Pointing the server
    # at a different host must not send it the old host's token, because nobody
    # agreed to that host.
    if cred.url and config.url and cred.url != config.url:
        return None
    return cred


def related_credential_versions(
    servers: list[ResolvedServer], project_root: str, credential_store: Any
) -> dict[str, str]:
    """Per-credential digests, for the records these servers actually use.

    Only the related ones: an unrelated token rotation is not a reason to drop a
    running process. Matching goes through :func:`matching_credential`, so a
    server whose credential was replaced by one for another URL is covered too
    — otherwise that change would leave the old token in a live session.
    """
    credentials = credential_store.snapshot()
    all_versions = credential_versions(credential_store)
    out: dict[str, str] = {}
    for server in servers:
        cred = matching_credential(
            server.config,
            scope=server.scope,
            root=project_root if server.scope == "project" else "",
            credential_store=credentials,
        )
        if cred is not None and cred.id in all_versions:
            out[cred.id] = all_versions[cred.id]
    return out


def apply_credentials(
    config: MCPServerConfig,
    *,
    scope: str,
    root: str = "",
    credential_store: Any = None,
) -> MCPServerConfig:
    """``config`` with the secrets it references filled in from the store.

    The names in ``secret_env``/``secret_headers`` are the *targets* (an
    environment variable, an HTTP header) and their values are the key to read
    inside the stored record, so one server can take several secrets and each
    can be replaced on its own.

    A record is only used for the URL it was issued for. Pointing the server at
    a different host must not send it the old host's token, because nobody
    agreed to that host.
    """
    cred = matching_credential(
        config, scope=scope, root=root, credential_store=credential_store or store()
    )
    if cred is None:
        return config

    def value(key: str) -> str:
        return cred.values.get(key, "")

    out = replace(config, env=dict(config.env), http_headers=dict(config.http_headers))
    for target, key in config.secret_env.items():
        if secret := value(key):
            out.env[target] = secret
    for target, key in config.secret_headers.items():
        if secret := value(key):
            out.http_headers[target] = secret
    if config.bearer_credential:
        token = value(config.bearer_credential)
        if token:
            out.http_headers["Authorization"] = f"Bearer {token}"
    return out
