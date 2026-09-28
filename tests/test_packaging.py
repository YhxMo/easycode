"""How the project is packaged: the one place both halves of an install must agree.

Neither of these can fail at runtime in a source checkout — the source tree is
right there — so a mistake only shows up in a built wheel or a broken static
mount. They are cheap to check, hence checked here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_setuptools_packages_match_source_tree():
    """`packages` is hand-written, so it can silently fall behind `src/`."""
    with (REPO / "pyproject.toml").open("rb") as fh:
        listed = set(tomllib.load(fh)["tool"]["setuptools"]["packages"])
    src = REPO / "src"
    found = {
        ".".join(("easycode", *path.parent.relative_to(src).parts))
        for path in src.rglob("__init__.py")
    }
    assert found == listed


def test_frontend_dist_points_into_repo():
    """`parents[N]` counts directories; adding or removing a level breaks it."""
    from easycode.web.main import FRONTEND_DIST

    assert FRONTEND_DIST == REPO / "frontend" / "dist"
