"""Glob compatibility and directory-pruning regressions."""

import json
import os
from pathlib import Path

import pytest

from easycode.permissions.boundary import PathContext
from easycode.permissions.policy import SANDBOX_DANGER_FULL_ACCESS
from easycode.tools import build_registry
from easycode.tools.search import GlobArgs, _is_skipped, _search_skip_dirs, glob


def legacy_glob(args, ctx):
    results = set()
    for root in ctx.roots:
        for pattern in (args.pattern, f"**/{args.pattern}"):
            for path in root.glob(pattern):
                if (
                    path.is_file()
                    and not _is_skipped(path, root, _search_skip_dirs(ctx))
                    and not ctx.is_protected(path)
                ):
                    tag = None if path.is_relative_to(ctx.primary.resolve()) else str(root)
                    results.add((ctx.display(path), tag))
    ordered = sorted(results, key=lambda row: row[0])[: args.max_results]
    return {
        "status": "ok",
        "matches": [path if tag is None else {"path": path, "root": tag} for path, tag in ordered],
        "count": len(ordered),
        "truncated": len(results) > args.max_results,
    }


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "ws"
    extra = tmp_path / "extra"
    root.mkdir()
    extra.mkdir()
    for name in (
        "a.py",
        "b.py",
        ".hidden.py",
        "name[1].py",
        "pkg/nested/n.py",
        "pkg/c.py",
        ".git/inside.py",
        "node_modules/dep/ignored.py",
        "._meta.py",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n")
    (extra / "external.py").write_text("pass\n")
    (root / "linked").symlink_to(extra, target_is_directory=True)
    (root / "cycle").symlink_to(root, target_is_directory=True)
    (root / "broken.py").symlink_to(root / "missing.py")
    (root / "large.py").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    return root, extra


@pytest.mark.parametrize("full_access", [False, True])
@pytest.mark.parametrize("max_results", [3, 500])
@pytest.mark.parametrize(
    "pattern",
    [
        "*.py",
        "**/*.py",
        "pkg/*.py",
        "pkg/*/*.py",
        "**/nested/*.py",
        "**/**/*.py",
        "pkg/**/n*.py",
        "[ab].py",
        "[!a].py",
        "name[[]1].py",
        "?.py",
        "A.py",
        ".hidden.py",
        "./a.py",
        "linked/*.py",
        "**/linked/*.py",
        "cycle/*.py",
        "**/*",
        "**",
        "pkg/**",
        "pkg/",
        "../extra/*.py",
        "a**.py",
        "",
        "/absolute",
    ],
)
def test_glob_matches_original_pathlib_behavior(tree, pattern, full_access, max_results):
    root, extra = tree
    ctx = PathContext(
        primary=root,
        secondary=[extra],
        **({"sandbox_mode": SANDBOX_DANGER_FULL_ACCESS} if full_access else {}),
    )
    args = GlobArgs(pattern=pattern, max_results=max_results)
    try:
        expected = legacy_glob(args, ctx)
    except (ValueError, NotImplementedError) as exc:
        expected = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    actual = json.loads(build_registry(100000).execute("glob", args.model_dump(), root, ctx))
    assert actual == expected


@pytest.mark.parametrize("full_access", [False, True])
def test_glob_never_enters_ignored_directories(tree, monkeypatch, full_access):
    root, _ = tree
    ctx = PathContext(
        primary=root, **({"sandbox_mode": SANDBOX_DANGER_FULL_ACCESS} if full_access else {})
    )
    scanned = []
    original = os.scandir

    def spy(path):
        scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", spy)
    result = json.loads(glob(GlobArgs(pattern="*.py"), root=root, ctx=ctx))
    assert result["status"] == "ok"
    assert not any("node_modules" in path.parts for path in scanned)
    assert any(".git" in path.parts for path in scanned) is full_access
    assert not any(path.name == "cycle" for path in scanned)
    assert "large.py" in result["matches"]
