"""File snapshot & undo/redo for session rollback.

Instead of relying on git to discover candidates (which is empty before a
turn starts), the manager snapshots file *content* at the moment a
file-modifying tool is about to run: the loop calls :meth:`note_tool` right
before dispatching ``write_file``/``edit_file``, and the manager records the
pre-state bytes (or a marker when the file does not exist yet).

Undo restores those bytes exactly — pre-existing uncommitted changes are
preserved because the snapshot captures whatever was on disk at that moment,
git or not. Redo rewrites the ``post`` state captured at undo time.

Known limitation: ``execute_shell`` commands that modify files other than
through the file tools are not tracked, so they are not rolled back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TurnRecord:
    """One user turn: pre state (captured lazily before each edit)."""

    pre: dict[str, bytes | None] = field(default_factory=dict)  # None = file absent
    post: dict[str, bytes | None] | None = None  # captured at undo time


class FileSnapshotManager:
    """Per-session turn snapshots for file-level undo/redo."""

    def __init__(self, session_id: str, roots: list[Path]) -> None:
        self.session_id = session_id
        self.roots = [r.resolve() for r in roots]
        self.stack: list[TurnRecord] = []
        self._redo_records: list[TurnRecord] = []

    # --- state --------------------------------------------------------------

    def can_undo(self) -> bool:
        return bool(self.stack)

    def can_redo(self) -> bool:
        return bool(self._redo_records)

    def begin_turn(self) -> None:
        """Open a new turn record; a new turn invalidates any pending redo."""
        self._redo_records.clear()
        self.stack.append(TurnRecord())

    def note_file(self, path: Path) -> None:
        """Record pre-state for ``path`` (first note per turn wins)."""
        if not self.stack:
            return
        rec = self.stack[-1]
        key = str(path)
        if key in rec.pre:
            return
        try:
            rec.pre[key] = path.read_bytes()
        except OSError:
            rec.pre[key] = None  # file created later in this turn

    def note_tool(self, name: str, arguments: dict, ctx) -> None:
        """Called by the loop before dispatching a tool to record pre-state."""
        if name in ("write_file", "edit_file"):
            raw = arguments.get("path")
            if raw:
                self.note_file(ctx.resolve(str(raw)))

    def pop_turn(self) -> None:
        """Drop the newest record (used when a turn is cancelled)."""
        if self.stack:
            self.stack.pop()

    def rollback_turn(self) -> list[str]:
        """Restore the newest turn's file pre-state, then drop it (MS-6).

        Used when a turn is cancelled or aborts so write tools that already
        executed are rolled back to their pre-state. Unlike :meth:`undo_turn`
        this does NOT record a ``post`` state for redo: the turn is being
        abandoned, not undone. ``execute_shell`` side effects (files written by
        shell commands, which are never snapshotted) are NOT rolled back — the
        caller reports that residual risk and must not pretend the turn was
        cleanly un-done.

        Returns the list of paths that were restored (best-effort).
        """
        if not self.stack:
            return []
        rec = self.stack.pop()
        restored: list[str] = []
        for rel, data in rec.pre.items():
            p = Path(rel)
            try:
                if data is None:
                    # file did not exist before this turn -> it was created -> delete it
                    if p.is_file():
                        p.unlink()
                        restored.append(rel)
                elif not p.is_file() or p.read_bytes() != data:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    restored.append(rel)
            except OSError:
                continue  # best-effort rollback; do not let one failure abort the rest
        return restored

    # --- undo / redo --------------------------------------------------------

    def undo_turn(self) -> dict:
        """Roll the last turn back: restore pre bytes, delete created files.

        Returns a summary of restored paths. Raises RuntimeError when there
        is nothing to undo.
        """
        if not self.stack:
            raise RuntimeError("nothing to undo")
        rec = self.stack.pop()

        post: dict[str, bytes | None] = {}
        changed: list[str] = []
        for rel, data in rec.pre.items():
            p = Path(rel)
            try:
                post[rel] = p.read_bytes()
            except OSError:
                post[rel] = None
            if data is None:
                # created during the turn → delete it
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    changed.append(rel)
            else:
                try:
                    if p.read_bytes() != data:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                        changed.append(rel)
                except OSError:
                    try:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                        changed.append(rel)
                    except OSError:
                        continue

        rec.post = post
        self._redo_records.append(rec)
        return {"restored": changed}

    def redo_turn(self) -> dict:
        """Reapply the most recently undone turn's file state."""
        if not self._redo_records:
            raise RuntimeError("nothing to redo")
        rec = self._redo_records.pop()
        if rec.post is None:
            raise RuntimeError("nothing to redo")
        changed: list[str] = []
        for rel, data in rec.post.items():
            p = Path(rel)
            try:
                if data is None:
                    if p.is_file():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                changed.append(rel)
            except OSError:
                continue
        rec.post = None
        self.stack.append(rec)
        return {"restored": changed}