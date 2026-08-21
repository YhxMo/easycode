"""Conversation history management."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from easycode.models.base import Message

SummarizerFn = Callable[[list[Message]], str]

SUMMARY_PREFIX = "Previous conversation summary (older messages were condensed):"


@dataclass
class History:
    """Bounded message history (OpenAI-format dicts).

    Budgets: ``max_tokens`` (usable model budget, primary) and ``max_chars``
    (backstop for non-token-countable models). ``summary`` tracks the latest
    rolling compaction summary so repeated compactions merge instead of
    overwriting (aligned with opencode).
    """

    system: Message | None = None
    messages: list[Message] = field(default_factory=list)
    max_messages: int = 100
    max_chars: int = 400_000
    max_tokens: int = 32_000
    summary: str | None = None

    def set_system(self, content: str) -> None:
        self.system = {"role": "system", "content": content}

    def add(self, message: Message) -> None:
        self.messages.append(message)
        while len(self.messages) > self.max_messages:
            self.messages.pop(0)

    def add_user(self, content: str) -> None:
        self.add({"role": "user", "content": content})

    def add_assistant(self, content: str, tool_calls: list | None = None) -> None:
        msg: Message = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.add(msg)

    def add_tool(self, tool_call_id: str, name: str, content: str) -> None:
        self.add({"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content})

    def payload(self) -> list[Message]:
        """Messages for a completion call: system (if set) + trimmed history."""
        out: list[Message] = []
        if self.system:
            out.append(self.system)
        out.extend(self.messages)
        return out

    def estimate_chars(self) -> int:
        total = sum(len(str(m.get("content") or "")) for m in self.messages)
        total += sum(
            len(str(tc.get("function", {}).get("arguments") or ""))
            for m in self.messages
            for tc in (m.get("tool_calls") or [])
        )
        return total

    def estimate_tokens(self) -> int:
        """Token estimate via litellm if possible, else the cheap heuristic."""
        try:
            import litellm

            return litellm.token_counter(messages=self.payload() or [{"role": "user", "content": ""}])
        except Exception:  # noqa: BLE001 - heuristic fallback
            return max(1, self._estimate_tokens_cheap())

    def _estimate_tokens_cheap(self) -> int:
        return self.estimate_messages_tokens(self.messages)

    @staticmethod
    def estimate_messages_tokens(messages: list[Message]) -> int:
        """Tokenizer-aware char heuristic for an explicit message list.

        CJK and other non-ASCII characters (kana, hangul, emoji…) tokenize
        ≈ 1 token/char while ASCII is closer to 4 chars/token. A flat
        chars/4 assumption badly under-counts non-ASCII-heavy transcripts,
        so count non-ASCII separately.
        """
        text = "".join(str(m.get("content") or "") for m in messages)
        text += "".join(
            str(tc.get("function", {}).get("arguments") or "")
            for m in messages
            for tc in (m.get("tool_calls") or [])
        )
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        ascii_chars = len(text) - non_ascii
        return non_ascii + (ascii_chars + 3) // 4

    def over_budget(self) -> bool:
        """Cheap-gated budget check; avoid a full token count on every loop iteration.

        estimate_chars()/_estimate_tokens_cheap() are O(message text) while
        litellm.token_counter is comparatively expensive, so only run the exact
        count when the cheap estimate approaches ~70% of the token budget.
        """
        chars = self.estimate_chars()
        if chars > self.max_chars:
            return True
        if self._estimate_tokens_cheap() <= int(self.max_tokens * 0.7):
            return False
        return self.estimate_tokens() > self.max_tokens

    @staticmethod
    def is_summary(message: Message) -> bool:
        return message.get("role") == "system" and str(message.get("content", "")).startswith(
            SUMMARY_PREFIX
        )

    def _drop_oldest(self) -> Message | None:
        """Pop the oldest non-summary message (summary messages are protected)."""
        for i, m in enumerate(self.messages):
            if self.is_summary(m):
                continue
            return self.messages.pop(i)
        return None

    def trim(self) -> None:
        """Drop oldest messages beyond the limits (no summarization)."""
        while len(self.messages) > self.max_messages:
            if self._drop_oldest() is None:
                break
        total = self.estimate_chars()
        while total > self.max_chars and len(self.messages) > 2:
            dropped = self._drop_oldest()
            if dropped is None:
                break
            total -= len(str(dropped.get("content") or ""))
        while self.estimate_tokens() > self.max_tokens and len(self.messages) > 2:
            if self._drop_oldest() is None:
                break

    def condense(self, summary: str, keep_recent: int = 20) -> bool:
        """Replace everything older than ``keep_recent`` messages with a summary.

        Kept for compatibility; the loop uses :meth:`condense_from` with a
        token-selected tail index instead.
        """
        if len(self.messages) <= keep_recent + 1:
            return False
        return self.condense_from(summary, len(self.messages) - keep_recent)

    def condense_from(self, summary: str, tail_start: int) -> bool:
        """Replace messages before ``tail_start`` with a summary; keep the tail.

        Records ``summary`` so the next compaction can merge into it.
        """
        if tail_start <= 0 or tail_start >= len(self.messages):
            return False
        old = self.messages[:tail_start]
        if not old:
            return False
        recent = self.messages[tail_start:]
        summary_msg: Message = {
            "role": "system",
            "content": f"{SUMMARY_PREFIX}\n{summary}",
        }
        self.messages = [summary_msg, *recent]
        self.summary = summary
        return True

    def turns(self) -> list[tuple[int, int]]:
        """``(start, end)`` index pairs, one per user turn (end exclusive)."""
        result: list[tuple[int, int]] = []
        start: int | None = None
        for i, m in enumerate(self.messages):
            if m.get("role") == "user":
                if start is not None:
                    result.append((start, i))
                start = i
        if start is not None:
            result.append((start, len(self.messages)))
        return result

    def _split_turn_start(self, start: int, end: int, remaining: int) -> int | None:
        """First index in ``[start, end)`` whose suffix fits ``remaining`` tokens."""
        if end - start <= 1:
            return None
        for s in range(start + 1, end):
            if self.estimate_messages_tokens(self.messages[s:end]) <= remaining:
                return s
        return None

    def select_tail_start(self, budget: int, tail_turns: int | None = None) -> int | None:
        """Index of the first message kept verbatim, or None if nothing older to compact.

        Walks the most recent turns backward, keeping them within ``budget``
        tokens (see opencode's ``select``); the turn that does not fit is split
        for a partial tail.
        """
        if budget <= 0:
            return None
        all_turns = self.turns()
        if not all_turns:
            return None
        recent = all_turns if tail_turns is None else all_turns[-tail_turns:]
        keep: int | None = None
        total = 0
        for start, end in reversed(recent):
            size = self.estimate_messages_tokens(self.messages[start:end])
            if total + size <= budget:
                total += size
                keep = start
                continue
            split = self._split_turn_start(start, end, budget - total)
            if split is not None:
                keep = split
            break
        if keep is None or keep == 0:
            return None
        return keep

    def last_user_index(self) -> int:
        """Index of the last ``role=user`` message, or -1."""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                return i
        return -1

    def pop_user_turn(self) -> list[Message]:
        """Remove the last user turn (its user message + everything after).

        Returns the removed messages so a redo stack can re-append them.
        """
        idx = self.last_user_index()
        if idx < 0:
            return []
        removed = self.messages[idx:]
        self.messages = self.messages[:idx]
        return removed