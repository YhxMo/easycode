"""Undo/redo (session rollback) subsystem.

Owns the undo/redo algebra that bridges ``agent.history`` message-pop with the
file snapshot manager, matching the web endpoint semantics (single-turn undo,
redo of one turn, and undo-to-user). ``Agent`` keeps thin delegating methods so
``cli.py``, the web layer, and every test that calls ``agent.undo_turn()`` /
``agent.redo_turn()`` / ``agent.undo_to_user()`` keep working unchanged.
"""

from __future__ import annotations

from typing import Any


class TurnRollback:
    """Reify the undo/redo algebra for an :class:`~easycode.agent.loop.Agent`.

    Holds a back-reference to the owning agent and reads its ``history`` /
    ``snapshot_manager`` / ``_redo_stack`` live (they are mutated in place and
    reset per turn by the loop).
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def agent(self) -> Any:
        return self._agent

    @property
    def history(self) -> Any:
        return self._agent.history

    @property
    def snapshot_manager(self) -> Any:
        return self._agent.snapshot_manager

    @property
    def redo_stack(self) -> list[list[dict]]:
        return self._agent._redo_stack

    def undo_available(self) -> bool:
        return self.history.last_user_index() >= 0

    def redo_available(self) -> bool:
        return bool(self.redo_stack)

    def undo_turn(self) -> dict:
        """Undo the last user turn: drop its messages and restore files.

        Returns a summary dict; raises RuntimeError when there is nothing to undo.
        """
        if not self.undo_available():
            raise RuntimeError("nothing to undo")
        removed = self.history.pop_user_turn()
        self.redo_stack.append(removed)
        if self.snapshot_manager and self.snapshot_manager.can_undo():
            return self.snapshot_manager.undo_turn()
        return {"restored": [], "message_only": True}

    def redo_turn(self) -> dict:
        """Redo the last undone turn: re-append its messages and reapply files."""
        if not self.redo_stack:
            raise RuntimeError("nothing to redo")
        self.history.messages.extend(self.redo_stack.pop())
        if self.snapshot_manager and self.snapshot_manager.can_redo():
            return self.snapshot_manager.redo_turn()
        return {"restored": [], "message_only": True}

    def undo_to_user(self, nth: int) -> dict:
        """Undo everything back to just before the ``nth`` user message (1-based).

        The ``nth`` prompt and everything after it are removed and their file
        changes rolled back. Pending redo state is discarded (a batch undo
        cannot be re-applied in one step). Raises RuntimeError when ``nth``
        is out of range.
        """
        count = sum(1 for m in self.history.messages if m.get("role") == "user")
        if nth < 1 or nth > count:
            raise RuntimeError(f"invalid user message index: {nth} (有 {count} 条用户消息)")
        summary: dict = {"restored": []}
        while count >= nth and self.undo_available():
            self.history.pop_user_turn()
            self.redo_stack.clear()
            if self.snapshot_manager and self.snapshot_manager.can_undo():
                part = self.snapshot_manager.undo_turn()
                summary["restored"].extend(part.get("restored", []))
            count -= 1
        if not summary["restored"]:
            summary["message_only"] = True
        return summary


__all__ = ["TurnRollback"]
