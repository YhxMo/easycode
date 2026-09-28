"""The easycode data directory, ``~/.easycode``.

Session records, extensions, credentials and worktrees live here rather than in
the project tree. Everything that needs the location asks for it here, so a
temporary or relocated ``HOME`` moves all of it at once.
"""

from __future__ import annotations

from pathlib import Path

#: Name of the data directory under the user's home.
DATA_DIR_NAME = ".easycode"


def data_home() -> Path:
    return Path.home() / DATA_DIR_NAME
