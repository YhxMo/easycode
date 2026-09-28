"""Tool unit tests: 5 tools + workspace-root safety."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from easycode.tools import build_registry
from easycode.tools.files import MAX_LINE_LEN, MAX_READ_BYTES, ReadFileArgs, read_file


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


@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
def test_execute_shell(reg, tmp_path):
    out = _run(reg, "execute_shell", {"command": "echo hi", "timeout": 10}, tmp_path)
    assert out["status"] == "ok"
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]


@pytest.mark.skipif(sys.platform != "darwin", reason="workspace shell sandbox is macOS-only")
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


def _whole_file_read_result(path: Path, root: Path, offset: int, limit: int) -> dict:
    """The original read_text/splitlines result, including its output envelope."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    selected = []
    bytes_used = 0
    truncated = False
    for line in lines[offset - 1 : offset - 1 + limit]:
        out_line = line
        if len(line) > MAX_LINE_LEN:
            out_line = line[:MAX_LINE_LEN] + "…[truncated line]"
            truncated = True
        size = len(out_line.encode("utf-8", errors="replace")) + 1
        if selected and bytes_used + size > MAX_READ_BYTES:
            truncated = True
            break
        selected.append(out_line)
        bytes_used += size
    if offset - 1 + len(selected) < total:
        truncated = True
    content = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=offset))
    if truncated:
        content += f"\n\n(Showing lines {offset}-{offset + len(selected) - 1} of {total}. Use offset={offset + len(selected)} to continue.)"
    else:
        content += f"\n\n(End of file - total {total} lines)"
    return {
        "status": "ok",
        "path": path.relative_to(root).as_posix(),
        "absolute_path": str(path),
        "in_allowed": True,
        "start_line": offset,
        "end_line": offset + len(selected) - 1,
        "total_lines": total,
        "truncated": truncated,
        "lines": len(selected),
        "chars": len(content),
        "content": content,
    }


@pytest.mark.parametrize(
    ("data", "offset", "limit"),
    [
        (b"a\r\nb\rc\n", 1, 10),
        (b"a" * (64 * 1024 - 1) + b"\r\nb", 2, 1),
        ("a\u2028b\x85c".encode(), 1, 10),
        (b"a" * 1999, 1, 1),
        (b"a" * 2000, 1, 1),
        (b"a" * 2001, 1, 1),
        ((b"x" * 1000 + b"\n") * 60, 1, 100),
        (b"a" * (64 * 1024 - 1) + b"\xe4\xb8\xad\xff", 1, 1),
        (b"\n", 1, 1),
        (b"a\r", 1, 1),
    ],
)
def test_read_file_streaming_matches_whole_file_result(tmp_path, data, offset, limit):
    path = tmp_path / "sample.txt"
    path.write_bytes(data)
    expected = _whole_file_read_result(path, tmp_path, offset, limit)
    actual = json.loads(
        read_file(ReadFileArgs(path=path.name, offset=offset, limit=limit), root=tmp_path)
    )
    assert actual == expected


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
    result = json.loads(reg.execute("nope", {}, tmp_path))
    assert result["status"] == "error"
    assert "unknown tool" in result["message"]


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

def test_empty_tool_selection_exposes_no_tools(reg):
    assert reg.schemas(set()) == []
    assert reg.schemas(None)


def test_listing_does_not_enter_ignored_directories(tmp_path, monkeypatch):
    """Skipped directories are pruned before the walk enters them: the whole
    point is not paying for their contents at all."""
    import os

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    for ignored in ("node_modules/dep", ".git", "__pycache__"):
        d = tmp_path / ignored
        d.mkdir(parents=True)
        (d / "hidden.py").write_text("y = 1\n", encoding="utf-8")

    scanned: list[str] = []
    real_scandir = os.scandir

    def spy(path="."):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    from easycode.tools.files import _iter_files

    assert [p.name for p in _iter_files(tmp_path)] == ["app.py"]
    for ignored in ("node_modules", ".git", "__pycache__"):
        assert not [p for p in scanned if ignored in p], scanned


def test_listing_keeps_its_path_order(tmp_path):
    """Callers stop at the first N results, so the order is part of the contract."""
    from easycode.tools.files import _iter_files

    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "z.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "a" / "y.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "m.py").write_text("x\n", encoding="utf-8")

    rel = [p.relative_to(tmp_path).as_posix() for p in _iter_files(tmp_path)]
    assert rel == sorted(rel) == ["a/y.py", "b/z.py", "m.py"]
