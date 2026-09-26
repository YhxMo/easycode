"""Workspace file listing for the composer's @-mention menu (POST /api/files)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from easycode.tools.files import MAX_READ_BYTES
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
        # secondary roots must reach the agent: a factory that drops them would
        # make every multi-root assertion below pass for the wrong reason.
        root = agent_kwargs.get("root") or tmp_path
        secondary = agent_kwargs.get("secondary_roots") or []
        return Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=Path(root),
            secondary_roots=[Path(p) for p in secondary],
        )

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


def test_invalid_secondary_root_is_422_like_chat(tmp_path):
    """A bad draft root is a request error on both endpoints, never a 500."""
    client = make_app(tmp_path)
    body = {"root": str(tmp_path), "secondary_roots": [str(tmp_path / "missing-dir")]}

    files = client.post("/api/files", json=body)
    chat = client.post("/api/chat", json={**body, "message": "hi"})
    assert files.status_code == 422, files.text
    assert chat.status_code == 422, chat.text
    assert "missing" in files.json()["detail"]


def test_unreadable_file_reports_a_real_error(tmp_path):
    """A read failure is not an empty file: the pane must be able to tell."""
    _workspace(tmp_path)
    locked = tmp_path / "locked.txt"
    locked.write_text("secret\n", encoding="utf-8")
    locked.chmod(0)
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    try:
        r = client.get("/api/files/content", params={"session_id": session.id, "path": "locked.txt"})
        assert r.status_code == 500, r.text
        assert "读取文件失败" in r.json()["detail"]
    finally:
        locked.chmod(0o644)


def test_content_offset_and_limit_keep_their_tolerant_behaviour(tmp_path):
    _workspace(tmp_path)
    many = tmp_path / "many.txt"
    many.write_text("\n".join(f"line {i}" for i in range(1, 6)), encoding="utf-8")
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    # any integer is accepted; a non-positive limit means "to the end"
    assert _content(client, session.id, path="many.txt", limit=0)["text"].endswith("line 5")
    assert _content(client, session.id, path="many.txt", offset=-3)["start_line"] == 1
    # past the end is an empty window, not an error; `truncated` only ever
    # reports a window that was cut short, which an empty one is not
    past = _content(client, session.id, path="many.txt", offset=99)
    assert past["text"] == ""
    assert past["total_lines"] == 5
    assert past["truncated"] is False


def test_same_name_in_two_roots_has_distinct_targets(tmp_path):
    """A relative path alone cannot say which file it means."""
    primary = tmp_path / "ws"
    (primary / "src").mkdir(parents=True)
    (primary / "src" / "same.txt").write_text("primary\n", encoding="utf-8")
    extra = tmp_path / "extra"
    (extra / "src").mkdir(parents=True)
    (extra / "src" / "same.txt").write_text("secondary\n", encoding="utf-8")

    client = make_app(tmp_path)
    session = client.app.state.store.create(
        root=str(primary), secondary_roots=[str(extra)]
    )

    listing = client.post("/api/files", json={"session_id": session.id, "q": "same"}).json()
    entries = listing["files"]
    assert [e["path"] for e in entries] == ["src/same.txt", "src/same.txt"]
    assert len({e["absolute_path"] for e in entries}) == 2
    assert len({e["root"] for e in entries}) == 2

    # the secondary file opens as itself, not as the primary's namesake
    secondary = next(e for e in entries if e["root"] == str(extra.resolve()))
    body = _content(client, session.id, path=secondary["absolute_path"])
    assert body["text"] == "secondary"


def test_symlinks_that_the_preview_would_refuse_are_not_listed(tmp_path):
    primary = tmp_path / "ws"
    primary.mkdir()
    (primary / "inside.txt").write_text("in\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("out\n", encoding="utf-8")
    (primary / "escape.txt").symlink_to(outside)
    (primary / ".easycode").mkdir()
    (primary / ".easycode" / "state.json").write_text("{}", encoding="utf-8")
    (primary / "state.txt").symlink_to(primary / ".easycode" / "state.json")

    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(primary))

    names = [e["name"] for e in client.post("/api/files", json={"session_id": session.id}).json()["files"]]
    assert "inside.txt" in names
    assert "escape.txt" not in names
    assert "state.txt" not in names

    # and the preview refuses them for the same reason
    for path in ("escape.txt", "state.txt"):
        r = client.get("/api/files/content", params={"session_id": session.id, "path": path})
        assert r.status_code in (403, 404), (path, r.status_code)


def test_overlapping_roots_list_a_file_once(tmp_path):
    primary = tmp_path / "ws"
    inner = primary / "pkg"
    inner.mkdir(parents=True)
    (inner / "mod.py").write_text("x = 1\n", encoding="utf-8")

    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(primary), secondary_roots=[str(inner)])

    entries = client.post("/api/files", json={"session_id": session.id}).json()["files"]
    hits = [e for e in entries if e["name"] == "mod.py"]
    assert len(hits) == 1
    # the primary reaches it first, so the primary's relative path names it
    assert hits[0]["path"] == "pkg/mod.py"
    assert hits[0]["absolute_path"] == str((inner / "mod.py").resolve())


def _naive_window(text: str, offset: int, limit: int) -> dict:
    """What the preview returned before it read the file in bounded chunks."""
    lines = text.splitlines()
    total = len(lines)
    start = max(0, offset - 1)
    window = lines[start:] if limit <= 0 else lines[start : start + limit]
    body = "\n".join(window)
    cut = len(body.encode("utf-8")) > MAX_READ_BYTES
    if cut:
        body = body.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
    return {
        "text": body,
        "start_line": start + 1,
        "total_lines": total,
        "truncated": cut or start + len(window) < total,
    }


TRICKY_FILES = {
    "empty.txt": "",
    "no-newline.txt": "a",
    "trailing.txt": "a\n",
    "only-break.txt": "\n",
    "two-breaks.txt": "\n\n",
    "crlf.txt": "a\r\nb\r\nc",
    "lone-cr.txt": "a\rb",
    "cr-crlf.txt": "a\r\r\nb",
    "unicode-break.txt": "a b c",
    "big-line.txt": "x" * (MAX_READ_BYTES + 5000) + "\ntail\n",
    "multi-chunk.txt": "中" * 40000 + "\n尾",
    "many-lines.txt": "\n".join(f"line {i}" for i in range(1, 4000)),
}


def test_preview_matches_the_unbounded_read_for_tricky_files(tmp_path):
    """Chunked reading must not change what the preview returns: this compares
    every case against the whole-file computation it replaced."""
    _workspace(tmp_path)
    for name, text in TRICKY_FILES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    client = make_app(tmp_path)
    session = client.app.state.store.create(root=str(tmp_path))

    for name, text in TRICKY_FILES.items():
        for offset, limit in ((1, 0), (1, 2), (3, 0), (2, 1), (99, 0), (-3, 0), (1, 1)):
            expected = _naive_window(text, offset, limit)
            r = client.get(
                "/api/files/content",
                params={"session_id": session.id, "path": name, "offset": offset, "limit": limit},
            )
            assert r.status_code == 200, (name, offset, limit, r.text)
            got = {k: r.json()[k] for k in expected}
            assert got == expected, (name, offset, limit, got, expected)


def test_preview_reads_a_large_file_in_bounded_chunks(tmp_path, monkeypatch):
    """The point of the chunked read: a preview never loads the whole file."""
    import easycode.web.routes_files as routes

    huge = tmp_path / "huge.txt"
    huge.write_text("x" * (routes.CHUNK_BYTES * 3 + 10) + "\nend\n", encoding="utf-8")

    sizes: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            sizes.append(size)
            return self._handle.read(size)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    monkeypatch.setattr(Path, "open", lambda self, *a, **kw: Spy(real_open(self, *a, **kw)))
    window = routes._read_window(huge, 0, 0)

    # every line is counted, but once the byte budget is spent the text stops
    # being kept — the trailing "end" line is deliberately not retained
    assert window.total == 2
    assert window.lines == ["x" * (routes.CHUNK_BYTES * 3 + 10)]
    assert sizes and all(0 < size <= routes.CHUNK_BYTES for size in sizes)
    assert len(sizes) > 1
