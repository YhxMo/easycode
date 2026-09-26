"""Session file-tool records and the live Git state beside them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from easycode.web.artifacts import (
    MAX_DIFF,
    MAX_EXCERPT,
    build_record,
    records_from_messages,
)
from easycode.web.main import create_app
from easycode.web.session import SessionStore
from tests.conftest import FakeProvider

READ_OK = json.dumps(
    {
        "status": "ok",
        "path": "src/app.ts",
        "absolute_path": "/ws/src/app.ts",
        "in_allowed": True,
        "total_lines": 12,
        "lines": 12,
        "content": "1: 内容",
    }
)


def make_app(root: Path, session_dir: Path | None = None) -> TestClient:
    cfg_file = root / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=root)
    cfg.root = root

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[]),
            registry=build_registry(8000),
            root=Path(kw.get("root") or root),
            secondary_roots=[Path(p) for p in (kw.get("secondary_roots") or [])],
        )

    store = SessionStore(cfg, root, factory)
    return TestClient(create_app(cfg=cfg, session_store=store, static_dir=root / "no-dist"))


def git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_build_record_keeps_only_what_a_card_shows():
    """A record is the card's data, not a copy of the tool's whole output."""
    record = build_record("t1", "read_file", {"path": "src/app.ts"}, READ_OK)
    assert record is not None
    data = json.loads(record["result"])
    assert data["absolute_path"] == "/ws/src/app.ts"
    assert data["total_lines"] == 12
    assert data["content"] == "1: 内容"
    assert record["args"] == {"path": "src/app.ts"}

    long_read = json.dumps({"status": "ok", "path": "a", "content": "x" * (MAX_EXCERPT + 50)})
    trimmed = json.loads(build_record("t2", "read_file", {}, long_read)["result"])
    assert len(trimmed["content"]) < MAX_EXCERPT + 100
    assert "只保留" in trimmed["content"]

    long_diff = json.dumps({"status": "ok", "path": "a", "diff": "+" + "x" * (MAX_DIFF + 50)})
    cut = json.loads(build_record("t3", "write_file", {}, long_diff)["result"])
    assert len(cut["diff"]) < MAX_DIFF + 100


def test_build_record_skips_what_has_no_card():
    assert build_record("t1", "shell", {"command": "ls"}, READ_OK) is None
    assert build_record("t2", "read_file", {}, "not json") is None
    assert build_record("t3", "read_file", {}, json.dumps([1, 2])) is None
    # A call that never produced a change has no diff to show.
    assert build_record("t4", "edit_file", {}, json.dumps({"status": "ok", "path": "a"})) is None


def test_build_record_names_a_written_file_absolutely():
    """write_file/edit_file results carry no target; the session resolves it."""
    result = json.dumps({"status": "ok", "path": "src/app.ts", "diff": "+x"})
    record = build_record("t1", "write_file", {}, result, lambda p: Path("/ws") / p)
    assert json.loads(record["result"])["absolute_path"] == "/ws/src/app.ts"


def test_records_from_messages_rebuilds_what_history_still_has():
    messages = [
        {"role": "user", "content": "看看 app.ts"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {"name": "read_file", "arguments": '{"path": "src/app.ts"}'},
                },
                {"id": "t2", "function": {"name": "shell", "arguments": '{"command": "ls"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": READ_OK},
        {"role": "tool", "tool_call_id": "t2", "content": "ok"},
    ]
    records = records_from_messages(messages)
    assert [r["name"] for r in records] == ["read_file"]
    assert records[0]["args"] == {"path": "src/app.ts"}


def test_history_missing_its_tool_results_rebuilds_only_what_is_left():
    """A compaction that already dropped a result leaves nothing to rebuild."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
    ]
    assert records_from_messages(messages) == []


def test_chat_turn_records_file_tools_and_persists_them(tmp_path):
    """A turn's file tools are recorded on the session and written with it."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "app.ts").write_text("hello\n", encoding="utf-8")
    client = make_app(root)
    with client:
        store = client.app.state.store
        sess = store.create()
        sess.agent.provider.script = [
            {
                "tool_calls": [("t1", "read_file", {"path": "app.ts"})],
            },
            {"text": "done"},
        ]
        r = client.post("/api/chat", json={"message": "看看 app.ts", "session_id": sess.id})
        assert r.status_code == 200

        detail = client.get(f"/api/sessions/{sess.id}").json()
        assert [a["name"] for a in detail["artifacts"]] == ["read_file"]
        assert json.loads(detail["artifacts"][0]["result"])["content"].startswith("1: hello")

        saved = json.loads((tmp_path / ".easycode" / "sessions" / f"{sess.id}.json").read_text())
        assert [a["id"] for a in saved["artifacts"]] == ["t1"]

    # A fresh store over the same directory restores them with the session.
    client2 = make_app(root, session_dir=tmp_path / ".easycode" / "sessions")
    with client2:
        detail = client2.get(f"/api/sessions/{sess.id}").json()
        assert [a["id"] for a in detail["artifacts"]] == ["t1"]


def test_old_session_file_is_backfilled_from_its_history(tmp_path):
    """A conversation written before records existed gets them on load."""
    root = tmp_path / "ws"
    root.mkdir()
    sessions = tmp_path / ".easycode" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "legacy.json").write_text(
        json.dumps(
            {
                "id": "legacy",
                "title": "旧会话",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "ask",
                "root": str(root),
                "messages": [
                    {"role": "user", "content": "看看 app.ts"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "t1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "src/app.ts"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "t1", "content": READ_OK},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = make_app(root)
    with client:
        detail = client.get("/api/sessions/legacy").json()
        assert [a["id"] for a in detail["artifacts"]] == ["t1"]
        assert json.loads(detail["artifacts"][0]["result"])["total_lines"] == 12


@requires_git
def test_changes_reports_the_working_tree_as_it_is(tmp_path):
    """Staged, unstaged and untracked files all show up, with their own state."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    (root / "gone.txt").write_text("two\n", encoding="utf-8")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    (root / "tracked.txt").write_text("one\nmore\n", encoding="utf-8")
    (root / "new.txt").write_text("fresh\n", encoding="utf-8")
    git(root, "add", "new.txt")
    (root / "loose.txt").write_text("untracked\n", encoding="utf-8")
    (root / "gone.txt").unlink()

    client = make_app(root)
    with client:
        sess = client.app.state.store.create(root=str(root))
        data = client.get(f"/api/sessions/{sess.id}/changes").json()
        assert [r["name"] for r in data["repos"]] == ["repo"]
        by_path = {f["path"]: f for f in data["files"]}
        assert by_path["new.txt"]["staged"] == "A"
        assert by_path["new.txt"]["untracked"] is False
        assert by_path["loose.txt"]["untracked"] is True
        assert by_path["loose.txt"]["staged"] == ""
        assert by_path["tracked.txt"]["unstaged"] == "M"
        assert by_path["tracked.txt"]["staged"] == ""
        assert by_path["gone.txt"]["unstaged"] == "D"
        # Nothing to open for a deletion, and the entry says so.
        assert by_path["gone.txt"]["exists"] is False
        assert by_path["tracked.txt"]["exists"] is True
        assert data["truncated"] is False


@requires_git
def test_changes_lists_nothing_for_a_directory_outside_git(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    client = make_app(root)
    with client:
        sess = client.app.state.store.create(root=str(root))
        data = client.get(f"/api/sessions/{sess.id}/changes").json()
        assert data["repos"] == []
        assert data["files"] == []
        assert data["error"] is None
