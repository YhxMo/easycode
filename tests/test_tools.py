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


def test_truncation_produces_valid_json(reg, tmp_path):
    """Central truncation must keep valid JSON (P7-4): never slice mid-string."""
    (tmp_path / "big.txt").write_text(
        "\n".join(f"line {i} " + "x" * 60 for i in range(5000)), encoding="utf-8"
    )
    result = reg.execute("read_file", {"path": "big.txt"}, tmp_path)
    assert len(result) <= 8000 + 200
    data = json.loads(result)  # must parse
    assert data["status"] == "ok"
    assert data["truncated"] is True


def test_read_file_paging(reg, tmp_path):
    (tmp_path / "page.txt").write_text(
        "".join(f"line {i:03d}\n" for i in range(1, 11)), encoding="utf-8"
    )
    out = _run(reg, "read_file", {"path": "page.txt", "offset": 4, "limit": 3}, tmp_path)
    assert out["status"] == "ok"
    assert out["start_line"] == 4
    assert out["end_line"] == 6
    assert out["total_lines"] == 10
    assert out["truncated"] is True
    assert "4: line 004" in out["content"]
    assert "6: line 006" in out["content"]
    assert "line 007" not in out["content"]
    assert "Use offset=7 to continue" in out["content"]


def test_read_file_full_read_end_marker(reg, tmp_path):
    (tmp_path / "e.txt").write_text("a\nb\n", encoding="utf-8")
    out = _run(reg, "read_file", {"path": "e.txt"}, tmp_path)
    assert out["status"] == "ok"
    assert out["total_lines"] == 2
    assert out["truncated"] is False
    assert "End of file - total 2 lines" in out["content"]


def test_read_file_offset_out_of_range(reg, tmp_path):
    (tmp_path / "r.txt").write_text("a\nb\n", encoding="utf-8")
    out = _run(reg, "read_file", {"path": "r.txt", "offset": 5}, tmp_path)
    assert out["status"] == "error"


def test_read_file_empty(tmp_path, reg):
    (tmp_path / "e.txt").write_text("", encoding="utf-8")
    out = _run(reg, "read_file", {"path": "e.txt"}, tmp_path)
    assert out["status"] == "ok"
    assert out["start_line"] == 1
    assert out["end_line"] == 0
    assert out["total_lines"] == 0


def test_read_file_empty_offset_out_of_range(tmp_path, reg):
    (tmp_path / "e.txt").write_text("", encoding="utf-8")
    out = _run(reg, "read_file", {"path": "e.txt", "offset": 5}, tmp_path)
    assert out["status"] == "error"


def test_write_file_big_diff_stays_valid_json(tmp_path, reg):
    """P7-4 regression: a large write_file diff must not slice the JSON."""
    (tmp_path / "huge.py").write_text("x = 1\n" * 5000, encoding="utf-8")
    result = reg.execute(
        "write_file", {"path": "huge.py", "content": "y = 2\n" * 5000}, tmp_path
    )
    assert len(result) <= 8000 + 200
    data = json.loads(result)  # must parse
    assert data["status"] == "ok"
    assert data["truncated"] is True
    assert data["path"] == "huge.py"


def test_grep_truncation_keeps_head_matches(tmp_path, reg):
    for i in range(300):
        (tmp_path / f"f{i}.py").write_text(f"needle {i} " + "y" * 300, encoding="utf-8")
    result = reg.execute("grep", {"pattern": "needle", "max_matches": 300}, tmp_path)
    data = json.loads(result)  # valid JSON, not sliced
    assert data["truncated"] is True
    assert len(data["matches"]) > 0
    assert all(m["text"].startswith("needle") for m in data["matches"])


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