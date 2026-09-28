"""The tool-result shapes the Web pane reads.

`frontend/src/__tests__/Pane.test.ts` builds its cards from the samples in
`fixtures/toolResults.json`; this module regenerates those samples with the real
tools and fails when a field is added, renamed or dropped. Keeping the two in
step is what stops the frontend from quietly reading `undefined` out of a
result the backend has since changed.
"""

from __future__ import annotations

import json
from pathlib import Path

from easycode.permissions.boundary import PathContext
from easycode.tools import build_registry

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "toolResults.json"
)


def _workspace(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "export const a = 1;\nexport const b = 2;\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    return tmp_path


def _run(root: Path, name: str, args: dict) -> dict:
    return json.loads(build_registry(8000).execute(name, args, root, ctx=PathContext(primary=root)))


def test_result_shapes_match_the_frontend_fixture(tmp_path):
    root = _workspace(tmp_path / "ws")
    # A file the session may read but does not own: its display path stays
    # absolute, which is how the pane knows no preview link can be opened.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("external note\n", encoding="utf-8")
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))

    produced = {
        "read_file": {
            "ok": _run(root, "read_file", {"path": "src/app.ts"}),
            "error": _run(root, "read_file", {"path": "src/nope.ts"}),
            "outside": _run(root, "read_file", {"path": str(outside / "note.txt")}),
        },
        "grep": {
            "ok": _run(root, "grep", {"pattern": "export", "include": "*.ts"}),
            "empty": _run(root, "grep", {"pattern": "nothing-here"}),
        },
        "glob": {
            "ok": _run(root, "glob", {"pattern": "*.md"}),
            "empty": _run(root, "glob", {"pattern": "*.rs"}),
        },
        "edit_file": {
            "applied": _run(
                root, "edit_file", {"path": "src/app.ts", "old_string": "a = 1", "new_string": "a = 2"}
            ),
            "dry_run": _run(
                root,
                "edit_file",
                {
                    "path": "src/app.ts",
                    "old_string": "a = 2",
                    "new_string": "a = 3",
                    "dry_run": True,
                },
            ),
            "error": _run(
                root, "edit_file", {"path": "src/app.ts", "old_string": "nope", "new_string": "x"}
            ),
        },
    }

    for tool, cases in produced.items():
        for case, result in cases.items():
            assert set(result) == set(fixture[tool][case]), f"{tool}.{case}: {sorted(result)}"

    # Match rows have their own shape: `file` for display, `root` only for a
    # non-primary root, and never a `path` the frontend would have to guess at.
    grep_ok = next(iter(produced["grep"]["ok"]["matches"]))
    assert set(grep_ok) == {"file", "line", "text"}
    assert set(fixture["grep"]["secondary"]["matches"][0]) == {"file", "line", "text", "root"}


def test_glob_matches_use_both_documented_forms(tmp_path):
    """A string is a primary-root file; an object names the root it came from."""
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "note.md").write_text("hi\n", encoding="utf-8")
    primary = tmp_path / "ws"
    primary.mkdir()
    (primary / "README.md").write_text("# readme\n", encoding="utf-8")

    ctx = PathContext(primary=primary, secondary=[extra])
    result = json.loads(
        build_registry(8000).execute("glob", {"pattern": "*.md"}, primary, ctx=ctx)
    )
    assert result["matches"] == ["README.md", {"path": "note.md", "root": str(extra.resolve())}]


def test_read_file_target_is_unambiguous_across_roots(tmp_path):
    """The display path is relative to its own root; `absolute_path` is not."""
    primary = tmp_path / "ws"
    (primary / "src").mkdir(parents=True)
    (primary / "src" / "same.txt").write_text("primary\n", encoding="utf-8")
    extra = tmp_path / "extra"
    (extra / "src").mkdir(parents=True)
    (extra / "src" / "same.txt").write_text("secondary\n", encoding="utf-8")

    ctx = PathContext(primary=primary, secondary=[extra])
    result = json.loads(
        build_registry(8000).execute(
            "read_file", {"path": str(extra / "src" / "same.txt")}, primary, ctx=ctx
        )
    )
    assert result["path"] == "src/same.txt"
    assert result["absolute_path"] == str((extra / "src" / "same.txt").resolve())
