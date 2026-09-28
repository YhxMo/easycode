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
def test_changes_count_lines_against_head_for_every_state(tmp_path):
    """The counts describe each file against HEAD: staged, unstaged, untracked.

    A file that cannot be described in lines — a binary file — keeps its counts
    and says why it has no diff instead of offering an empty one.
    """
    # The config file the app is built with stays outside the repository, so
    # every file the report names is one this test put there.
    workspace = tmp_path / "ws"
    root = workspace / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\x01\x02")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    # unstaged: one line changed, one added
    (root / "tracked.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    # staged: a new file, entirely new lines
    (root / "staged.txt").write_text("s1\ns2\n", encoding="utf-8")
    git(root, "add", "staged.txt")
    # untracked: the whole file is new
    (root / "loose.txt").write_text("u1\nu2\nu3\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\xff\x03")

    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(root))
        data = client.get(f"/api/sessions/{sess.id}/changes").json()

    by_path = {f["path"]: f for f in data["files"]}
    assert set(by_path) == {"tracked.txt", "staged.txt", "loose.txt", "binary.dat"}
    assert (by_path["tracked.txt"]["added"], by_path["tracked.txt"]["removed"]) == (2, 1)
    assert (by_path["staged.txt"]["added"], by_path["staged.txt"]["removed"]) == (2, 0)
    assert (by_path["loose.txt"]["added"], by_path["loose.txt"]["removed"]) == (3, 0)
    # Totals are the working tree's answer for the whole session's directories.
    assert (data["added"], data["removed"]) == (7, 1)

    # A diff is offered per file, and it is the diff against HEAD.
    tracked_diff = by_path["tracked.txt"]["diff"]
    assert "-two" in tracked_diff and "+TWO" in tracked_diff and "+four" in tracked_diff
    assert by_path["staged.txt"]["diff"].startswith("diff --git")
    # An untracked file has no HEAD to compare against: its whole body is new.
    assert by_path["loose.txt"]["diff"] == "+u1\n+u2\n+u3\n"

    # A binary file keeps its state and is not given a preview it cannot use.
    assert by_path["binary.dat"]["binary"] is True
    assert by_path["binary.dat"]["diff"] is None
    assert by_path["binary.dat"]["diff_note"] == "二进制文件"


@requires_git
def test_changes_sums_every_bound_repository(tmp_path):
    """A session's secondary directory is a repository of its own: its changes
    count into the same totals and keep their own root."""
    workspace = tmp_path / "ws"
    primary = workspace / "primary"
    secondary = workspace / "secondary"
    for repo, name in ((primary, "a.txt"), (secondary, "b.txt")):
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / name).write_text("base\n", encoding="utf-8")
        git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
        git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    (primary / "a.txt").write_text("base\nmore\n", encoding="utf-8")
    (secondary / "b.txt").write_text("base\nmore\nand more\n", encoding="utf-8")

    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(
            root=str(primary), secondary_roots=[str(secondary)]
        )
        data = client.get(f"/api/sessions/{sess.id}/changes").json()

    assert sorted(r["name"] for r in data["repos"]) == ["primary", "secondary"]
    assert {f["repo"] for f in data["files"]} == {str(primary), str(secondary)}
    assert {f["path"]: f["added"] for f in data["files"]} == {"a.txt": 1, "b.txt": 2}
    assert data["added"] == 3
    assert data["removed"] == 0


@requires_git
def test_changes_keeps_a_diff_past_the_size_cap_out_of_the_response(tmp_path):
    """One enormous file must not dominate the response: its counts arrive, its
    preview does not, and the entry says which of the two happened."""
    from easycode.web.git import MAX_DIFF_CHARS

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "huge.txt").write_text("base\n", encoding="utf-8")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    (root / "huge.txt").write_text("x" * (MAX_DIFF_CHARS + 100) + "\n", encoding="utf-8")

    client = make_app(root)
    with client:
        sess = client.app.state.store.create(root=str(root))
        data = client.get(f"/api/sessions/{sess.id}/changes").json()

    entry = data["files"][0]
    assert entry["added"] == 1  # the count is still exact
    assert entry["diff"] is None
    assert entry["diff_note"] == "改动过大，未生成预览"


@requires_git
def test_changes_diff_a_rename_against_its_old_path(tmp_path):
    """A renamed file is diffed with both of its paths: restricted to the new
    one alone, git would report the whole file as added next to a “1 changed
    line” count."""
    workspace = tmp_path / "ws"
    root = workspace / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "old.txt").write_text("a\nb\nc\n", encoding="utf-8")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    git(root, "mv", "old.txt", "new.txt")
    (root / "new.txt").write_text("a\nB\nc\n", encoding="utf-8")

    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(root))
        data = client.get(f"/api/sessions/{sess.id}/changes").json()

    entry = data["files"][0]
    assert entry["path"] == "new.txt"
    assert entry["origin"] == "old.txt"
    assert (entry["added"], entry["removed"]) == (1, 1)
    assert "-b" in entry["diff"] and "+B" in entry["diff"]
    assert "new file mode" not in entry["diff"]


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
        assert data["added"] == 0
        assert data["removed"] == 0
        assert data["error"] is None


def _repo_with_changes(tmp_path) -> Path:
    """A repository holding every kind of file the report distinguishes."""
    workspace = tmp_path / "ws"
    root = workspace / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "old.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\x01\x02")
    (root / "gone.txt").write_text("bye\n", encoding="utf-8")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    (root / "tracked.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    (root / "loose.txt").write_text("u1\nu2\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\xff\x03")
    (root / "gone.txt").unlink()
    git(root, "mv", "old.txt", "new.txt")
    (root / "new.txt").write_text("a\nB\nc\n", encoding="utf-8")
    return workspace


@requires_git
def test_changes_without_diffs_skips_every_git_diff(tmp_path, monkeypatch):
    """`diffs=0` answers with the same report and no per-file git call."""
    import easycode.web.git as gitmod

    workspace = _repo_with_changes(tmp_path)
    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(workspace / "repo"))
        with_diffs = client.get(f"/api/sessions/{sess.id}/changes").json()

        calls: list[tuple] = []
        real = gitmod._git

        def counting(repo, *args, **kwargs):
            calls.append(args)
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(gitmod, "_git", counting)
        stats_only = client.get(f"/api/sessions/{sess.id}/changes?diffs=0").json()

    def flat(data):
        return [{k: v for k, v in f.items() if k != "diff"} for f in data["files"]]

    assert flat(stats_only) == flat(with_diffs)
    assert (stats_only["added"], stats_only["removed"]) == (
        with_diffs["added"],
        with_diffs["removed"],
    )
    assert all(f["diff"] is None for f in stats_only["files"])
    # Only repository-wide reads remain: nothing names a file's path.
    assert not [a for a in calls if "--" in a], calls


@requires_git
def test_changes_wrong_value_for_diffs_is_rejected(tmp_path):
    """The query flag is a number: anything else is a bad request, not a 500."""
    workspace = _repo_with_changes(tmp_path)
    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(workspace / "repo"))
        assert client.get(f"/api/sessions/{sess.id}/changes?diffs=abc").status_code == 422


@requires_git
def test_one_file_diff_is_fetched_by_repo_and_path(tmp_path):
    """The row a reader opens gets its own file's diff, and nothing else."""
    workspace = _repo_with_changes(tmp_path)
    repo = workspace / "repo"
    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(repo))
        tracked = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(repo), "path": "tracked.txt"},
        ).json()
        loose = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(repo), "path": "loose.txt"},
        ).json()
        renamed = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(repo), "path": "new.txt"},
        ).json()

    assert "-two" in tracked["diff"] and "+TWO" in tracked["diff"]
    # An untracked file has no HEAD to compare against: its whole body is new.
    assert loose["diff"] == "+u1\n+u2\n"
    # A rename is diffed against both paths, so it reads as a rename.
    assert "-b" in renamed["diff"] and "+B" in renamed["diff"]
    assert "new file mode" not in renamed["diff"]


@requires_git
def test_one_file_diff_explains_what_it_cannot_show(tmp_path):
    """Binary and deleted files keep the reason instead of an empty preview."""
    workspace = _repo_with_changes(tmp_path)
    repo = workspace / "repo"
    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(repo))
        binary = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(repo), "path": "binary.dat"},
        ).json()
        deleted = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(repo), "path": "gone.txt"},
        ).json()

    assert binary == {"diff": None, "diff_note": "二进制文件"}
    assert deleted == {"diff": None, "diff_note": "文件已删除"}


@requires_git
def test_one_file_diff_refuses_anything_outside_the_session(tmp_path):
    """The repository and the path are both checked against this session."""
    workspace = _repo_with_changes(tmp_path)
    repo = workspace / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(repo))
        other = client.app.state.store.create(root=str(elsewhere))
        base = f"/api/sessions/{sess.id}/changes/diff"

        assert client.get(
            base, params={"repo": str(elsewhere), "path": "tracked.txt"}
        ).status_code == 404
        # A file of this repository that has no uncommitted change is not
        # something the pane was describing.
        assert client.get(
            base, params={"repo": str(repo), "path": "never-changed.txt"}
        ).status_code == 404
        # A path that tries to leave the repository is just another name that
        # is not in the status list.
        assert client.get(
            base, params={"repo": str(repo), "path": "../outside.txt"}
        ).status_code == 404
        # Another session's repository is not this session's either.
        assert client.get(
            f"/api/sessions/{other.id}/changes/diff",
            params={"repo": str(repo), "path": "tracked.txt"},
        ).status_code == 404
        assert client.get(
            f"/api/sessions/{sess.id}/changes/diff", params={"repo": "", "path": "x"}
        ).status_code == 404


@requires_git
def test_one_file_diff_in_a_repository_without_a_commit(tmp_path):
    """No HEAD is not an error: the empty tree is what everything is new to."""
    workspace = tmp_path / "ws"
    root = workspace / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    (root / "first.txt").write_text("hello\n", encoding="utf-8")
    git(root, "add", "first.txt")

    client = make_app(workspace)
    with client:
        sess = client.app.state.store.create(root=str(root))
        body = client.get(
            f"/api/sessions/{sess.id}/changes/diff",
            params={"repo": str(root), "path": "first.txt"},
        ).json()

    assert "hello" in body["diff"]
