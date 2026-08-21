"""P5.5-1: FileSnapshotManager (content snapshot at tool time, git-independent)."""

from __future__ import annotations

from pathlib import Path

import pytest

from easycode.snapshot import FileSnapshotManager


@pytest.fixture
def mgr(tmp_path: Path) -> FileSnapshotManager:
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    return m


def test_undo_restores_modified_and_deletes_newly_created(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    (tmp_path / "a.txt").write_text("original\n", encoding="utf-8")
    m.note_file(tmp_path / "a.txt")
    m.note_file(tmp_path / "c.txt")  # absent at note time → created by turn

    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("new\n", encoding="utf-8")

    summary = m.undo_turn()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "c.txt").exists()
    assert any(str(p).endswith("a.txt") for p in summary["restored"])

    m.redo_turn()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "changed\n"
    assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "new\n"


def test_works_without_git_at_all(tmp_path: Path) -> None:
    """Content snapshots do not need a git repository."""
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    m.note_file(tmp_path / "x.txt")  # absent → created by turn
    (tmp_path / "x.txt").write_text("hi\n", encoding="utf-8")
    m.undo_turn()
    assert not (tmp_path / "x.txt").exists()


def test_double_note_keeps_first_pre_state(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    m.note_file(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("v2\n", encoding="utf-8")
    m.note_file(tmp_path / "a.txt")  # second note must not overwrite pre

    (tmp_path / "a.txt").write_text("v3\n", encoding="utf-8")
    m.undo_turn()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1\n"


def test_new_turn_clears_pending_redo(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    # build a real undo/redo pair then invalidate it
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    m.begin_turn()
    m.note_file(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("y\n", encoding="utf-8")
    m.undo_turn()
    assert m.can_redo()
    m.begin_turn()  # new turn invalidates redo
    assert not m.can_redo()


def test_undo_raises_when_empty(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    with pytest.raises(RuntimeError):
        m.undo_turn()


def test_redo_raises_when_nothing(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    m.note_file(tmp_path / "a.txt")
    m.undo_turn()
    m.redo_turn()
    with pytest.raises(RuntimeError):
        m.redo_turn()


def test_note_tool_dispatches_by_tool_name(tmp_path: Path) -> None:
    m = FileSnapshotManager("sess-1", [tmp_path])
    m.begin_turn()
    (tmp_path / "f.txt").write_text("keep\n", encoding="utf-8")
    from easycode.workspace import PathContext

    ctx = PathContext(primary=tmp_path)
    m.note_tool("write_file", {"path": "f.txt"}, ctx)
    (tmp_path / "f.txt").write_text("gone\n", encoding="utf-8")
    m.undo_turn()
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "keep\n"