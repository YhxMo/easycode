"""Workspace file listing for the composer's @-mention menu (POST /api/files)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider


def make_app(tmp_path: Path) -> TestClient:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps({"models": {"fake-a": "fake/a"}}),
        encoding="utf-8",
    )
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **agent_kwargs):
        root = agent_kwargs.get("root") or tmp_path
        return Agent(provider=FakeProvider(script=[]), registry=build_registry(8000), root=Path(root))

    store = SessionStore(cfg, tmp_path, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))


def _workspace(tmp_path: Path) -> None:
    """A small tree with the directories and files the listing must treat differently."""
    (tmp_path / "src" / "web").mkdir(parents=True)
    (tmp_path / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / "src" / "web" / "routes.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text("module.exports = {};\n")
    (tmp_path / ".easycode").mkdir()
    (tmp_path / ".easycode" / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("\0", encoding="utf-8")


def _paths(payload: dict) -> list[str]:
    return [f["path"] for f in payload["files"]]


def test_lists_workspace_files_and_skips_ignored_paths(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)

    r = client.post("/api/files", json={"root": str(tmp_path)})
    assert r.status_code == 200
    paths = _paths(r.json())

    assert "src/app.ts" in paths
    assert "src/web/routes.py" in paths
    assert "README.md" in paths
    # ignored directories, metadata files, and protected boundaries never appear
    assert all(not p.startswith(".git/") for p in paths)
    assert all(not p.startswith("node_modules/") for p in paths)
    assert all(not p.startswith(".easycode/") for p in paths)
    assert ".DS_Store" not in paths
    # the project's own config is a hard-protected path
    assert "easycode.config.json" not in paths


def test_entry_shape_carries_dir_and_name(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)

    entry = next(f for f in client.post("/api/files", json={"root": str(tmp_path)}).json()["files"] if f["path"] == "src/web/routes.py")
    assert entry["name"] == "routes.py"
    assert entry["dir"] == "src/web"


def test_query_ranks_basename_hits_first(tmp_path):
    _workspace(tmp_path)
    # a path match whose own name does not match
    (tmp_path / "readme").mkdir()
    (tmp_path / "readme" / "notes.txt").write_text("notes\n", encoding="utf-8")
    client = make_app(tmp_path)

    paths = _paths(client.post("/api/files", json={"root": str(tmp_path), "q": "readme"}).json())
    assert paths == ["README.md", "readme/notes.txt"]


def test_limit_caps_the_listing(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)
    payload = client.post("/api/files", json={"root": str(tmp_path), "limit": 2}).json()
    assert len(payload["files"]) == 2
    # total reports what matched, so the UI can say the list was cut short
    assert payload["total"] > 2


def test_session_scope_uses_the_sessions_own_roots(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    r = client.post("/api/files", json={"session_id": session.id})
    assert r.status_code == 200
    assert "src/app.ts" in _paths(r.json())


def test_unknown_session_is_404(tmp_path):
    client = make_app(tmp_path)
    assert client.post("/api/files", json={"session_id": "nope"}).status_code == 404


def test_sensitive_draft_root_is_422(tmp_path):
    client = make_app(tmp_path)
    r = client.post("/api/files", json={"root": str(tmp_path / ".git")})
    assert r.status_code == 422


def test_explicit_empty_secondary_roots_do_not_inherit_the_project_binding(tmp_path):
    """`null` inherits the project's secondary roots; `[]` means there are none."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _workspace(workspace)
    # a secondary root outside the primary, so a match can only come from it
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "note.md").write_text("hi\n", encoding="utf-8")

    client = make_app(tmp_path)
    client.app.state.store.cfg.workspace_projects = [
        {"root": str(workspace), "secondary": [str(extra)]}
    ]

    inherited = _paths(client.post("/api/files", json={"root": str(workspace)}).json())
    assert "note.md" in inherited

    explicit = _paths(
        client.post("/api/files", json={"root": str(workspace), "secondary_roots": []}).json()
    )
    assert "note.md" not in explicit


def _content(client: TestClient, session_id: str, **params) -> dict:
    r = client.get("/api/files/content", params={"session_id": session_id, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_content_returns_file_text_with_line_window(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    whole = _content(client, session.id, path="src/app.ts")
    assert whole["text"] == "export const a = 1;"
    assert whole["total_lines"] == 1
    assert whole["truncated"] is False

    many = tmp_path / "many.txt"
    many.write_text("\n".join(f"line {i}" for i in range(1, 11)), encoding="utf-8")
    page = _content(client, session.id, path="many.txt", offset=3, limit=2)
    assert page["text"] == "line 3\nline 4"
    assert page["start_line"] == 3
    assert page["total_lines"] == 10
    assert page["truncated"] is True


def test_content_rejects_paths_outside_the_workspace(tmp_path):
    _workspace(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    r = client.get(
        "/api/files/content", params={"session_id": session.id, "path": str(outside)}
    )
    assert r.status_code == 403
    # a traversal written as a relative path is refused the same way
    r = client.get(
        "/api/files/content", params={"session_id": session.id, "path": "../outside.txt"}
    )
    assert r.status_code == 403


def test_content_rejects_protected_paths(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    for path in ("easycode.config.json", ".easycode/state.json"):
        r = client.get("/api/files/content", params={"session_id": session.id, "path": path})
        assert r.status_code == 403, path


def test_content_unknown_session_and_missing_file(tmp_path):
    _workspace(tmp_path)
    client = make_app(tmp_path)
    assert (
        client.get(
            "/api/files/content", params={"session_id": "nope", "path": "src/app.ts"}
        ).status_code
        == 404
    )
    session = client.app.state.store.create(root=str(tmp_path))
    assert (
        client.get(
            "/api/files/content", params={"session_id": session.id, "path": "src/nope.ts"}
        ).status_code
        == 404
    )
