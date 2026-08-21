"""Tool unit tests: 5 tools + workspace-root safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycode.tools import build_registry


@pytest.fixture
def reg():
    return build_registry(8000)


def _run(reg, name, args, root: Path):
    return json.loads(reg.execute(name, args, root))


def test_read_file(reg, tmp_path):
    (tmp_path / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    out = _run(reg, "read_file", {"path": "a.txt"}, tmp_path)
    assert out["status"] == "ok"
    assert "line1" in out["content"]


def test_read_missing(reg, tmp_path):
    out = _run(reg, "read_file", {"path": "nope.txt"}, tmp_path)
    assert out["status"] == "error"


def test_write_file_nested(reg, tmp_path):
    out = _run(reg, "write_file", {"path": "sub/x.txt", "content": "hi"}, tmp_path)
    assert out["status"] == "ok"
    assert (tmp_path / "sub" / "x.txt").read_text() == "hi"


def test_glob_recursive(reg, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    out = _run(reg, "glob", {"pattern": "**/*.py"}, tmp_path)
    assert set(out["matches"]) == {"pkg/a.py", "b.py"}


def test_glob_skips_meta_and_git(reg, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "f.py").write_text("x")
    (tmp_path / "._junk.py").write_text("x")
    (tmp_path / "keep.py").write_text("x")
    out = _run(reg, "glob", {"pattern": "**/*.py"}, tmp_path)
    assert out["matches"] == ["keep.py"]


def test_grep(reg, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ndef foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing here\n", encoding="utf-8")
    out = _run(reg, "grep", {"pattern": r"def \w+"}, tmp_path)
    assert out["status"] == "ok"
    assert out["matches"][0]["file"] == "a.py"
    assert out["matches"][0]["line"] == 2


def test_grep_invalid_regex(reg, tmp_path):
    out = _run(reg, "grep", {"pattern": "("}, tmp_path)
    assert out["status"] == "error"


def test_execute_shell(reg, tmp_path):
    out = _run(reg, "execute_shell", {"command": "echo hi", "timeout": 10}, tmp_path)
    assert out["status"] == "ok"
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]


def test_execute_shell_failure(reg, tmp_path):
    out = _run(reg, "execute_shell", {"command": "exit 3"}, tmp_path)
    assert out["exit_code"] == 3


def test_path_escape_blocked(reg, tmp_path):
    """Reads outside the sandbox are allowed; writes/edits are reported for approval."""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("s3cr3t")
    out = _run(reg, "read_file", {"path": "../secret.txt"}, tmp_path)
    assert out["status"] == "ok"
    assert "s3cr3t" in out["content"]
    assert out["in_allowed"] is False

    out2 = _run(reg, "write_file", {"path": "../evil.txt", "content": "x"}, tmp_path)
    assert out2["status"] == "error"
    assert out2["in_allowed"] is False
    assert "approval" in out2["message"]
    assert not (tmp_path.parent / "evil.txt").exists()


def test_result_truncation(reg, tmp_path):
    (tmp_path / "big.txt").write_text("a" * 100_000, encoding="utf-8")
    result = reg.execute("read_file", {"path": "big.txt"}, tmp_path)
    assert len(result) <= 8000 + 200
    assert "[truncated" in result


def test_unknown_tool(reg, tmp_path):
    with pytest.raises(KeyError):
        reg.execute("nope", {}, tmp_path)


def test_edit_file_success(reg, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def one():\n    return 1\n\ndef two():\n    return 2\n", encoding="utf-8")
    out = _run(reg, "edit_file", {"path": "a.py", "old_string": "return 1", "new_string": "return 42"}, tmp_path)
    assert out["status"] == "ok"
    assert out["applied"] is True
    assert "return 42" in f.read_text()
    assert "+" in out["diff"] and "-" in out["diff"]
    assert "def one():" in out["diff"]


def test_edit_file_not_found(reg, tmp_path):
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    out = _run(reg, "edit_file", {"path": "a.py", "old_string": "y = 2", "new_string": "z"}, tmp_path)
    assert out["status"] == "error"
    assert "not found" in out["message"]


def test_edit_file_ambiguous_match(reg, tmp_path):
    (tmp_path / "a.py").write_text("v = 1\nv = 1\n", encoding="utf-8")
    out = _run(reg, "edit_file", {"path": "a.py", "old_string": "v = 1", "new_string": "v = 9"}, tmp_path)
    assert out["status"] == "error"
    assert out["occurrences"] == 2
    assert "unique" in out["message"]


def test_edit_file_dry_run_no_change(reg, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a = 1\n", encoding="utf-8")
    out = _run(reg, "edit_file", {"path": "a.py", "old_string": "a = 1", "new_string": "a = 2", "dry_run": True}, tmp_path)
    assert out["status"] == "ok"
    assert out["dry_run"] is True
    assert "a = 2" in out["diff"]
    assert f.read_text() == "a = 1\n"  # unchanged


def test_edit_file_escape_blocked(reg, tmp_path):
    out = _run(reg, "edit_file", {"path": "../x.py", "old_string": "a", "new_string": "b"}, tmp_path)
    assert out["status"] == "error"