"""Conversation history management."""

from __future__ import annotations

from dataclasses import dataclass, field

from easycode.models.base import Message

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

    @property
    def summary(self) -> str | None:
        """Latest rolling compaction summary, derived from ``messages[0]``."""
        if self.messages and self.is_summary(self.messages[0]):
            return str(self.messages[0]["content"])[len(SUMMARY_PREFIX) :].lstrip()
        return None

    def set_system(self, content: str) -> None:
        self.system = {"role": "system", "content": content}

    def add(self, message: Message) -> None:
        self.messages.append(message)
        # Trim in whole turns so a single pop never lands mid-turn on an
        # assistant ``tool_calls`` message or a ``tool`` result: a
        # boundary can only break a tool_call/result pairing, which would
        # leave an orphan tool in the payload.
        while len(self.messages) > self.max_messages:
            if not self._drop_oldest_turn():
                break

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

    @staticmethod
    def _text(messages: list[Message]) -> str:
        """Concatenated content + tool-call arguments of a message list."""
        text = "".join(str(m.get("content") or "") for m in messages)
        text += "".join(
            str(tc.get("function", {}).get("arguments") or "")
            for m in messages
            for tc in (m.get("tool_calls") or [])
        )
        return text

    def estimate_chars(self) -> int:
        return len(self._text(self.messages))

    def estimate_tokens(self) -> int:
        """Token estimate via litellm if possible, else the cheap heuristic.

        Both paths count the system prompt (the payload includes it), so a
        large system consumes budget rather than being hidden behind the
        message-only count.
        """
        try:
            import litellm

            return litellm.token_counter(
                messages=self.payload() or [{"role": "user", "content": ""}]
            )
        except Exception:  # noqa: BLE001 - heuristic fallback
            return max(1, self._estimate_tokens_cheap() + self._system_tokens())

    def _estimate_tokens_cheap(self) -> int:
        return self.estimate_messages_tokens(self.messages)

    def _system_tokens(self) -> int:
        if not self.system:
            return 0
        return self.estimate_messages_tokens([self.system])

    @staticmethod
    def estimate_text_tokens(text: str) -> int:
        """Tokenizer-aware char heuristic for a raw string."""
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        ascii_chars = len(text) - non_ascii
        return non_ascii + (ascii_chars + 3) // 4

    @staticmethod
    def estimate_messages_tokens(messages: list[Message]) -> int:
        """Tokenizer-aware char heuristic for an explicit message list.

        CJK and other non-ASCII characters (kana, hangul, emoji…) tokenize
        ≈ 1 token/char while ASCII is closer to 4 chars/token. A flat
        chars/4 assumption badly under-counts non-ASCII-heavy transcripts,
        so count non-ASCII separately.
        """
        return History.estimate_text_tokens(History._text(messages))

    def over_budget(self, extra: int = 0) -> bool:
        """Cheap-gated budget check for the completion payload.

        ``extra`` is the estimated token cost of the non-message content sent
        with every call — the tool schemas. Both the cheap pre-gate and the
        exact count now account for the system prompt and ``extra``, so a large
        system or a large tool set trips over-budget instead of being hidden by
        an empty conversation. Without this, only ``estimate_chars``
        (messages only) and the message-only cheap gate were consulted and a
        big system/schema never triggered compaction.
        """
        chars = self.estimate_chars()
        if chars > self.max_chars:
            return True
        system_tokens = self._system_tokens()
        if self._estimate_tokens_cheap() + system_tokens + extra <= int(self.max_tokens * 0.7):
            return False
        return self.estimate_tokens() + extra > self.max_tokens

    @staticmethod
    def is_summary(message: Message) -> bool:
        return message.get("role") == "system" and str(message.get("content", "")).startswith(
            SUMMARY_PREFIX
        )

    def trim(self) -> None:
        """Drop oldest messages beyond the limits (no summarization).

        Trims whole user turns so an assistant ``tool_calls`` message and its
        ``tool`` results are never split across the trim boundary.
        """
        while len(self.messages) > self.max_messages:
            if not self._drop_oldest_turn():
                break
        total = self.estimate_chars()
        while total > self.max_chars and len(self.messages) > 2:
            if not self._drop_oldest_turn():
                break
            total = self.estimate_chars()
        while self.estimate_tokens() > self.max_tokens and len(self.messages) > 2:
            if not self._drop_oldest_turn():
                break

    def condense_from(self, summary: str, tail_start: int) -> bool:
        """Replace messages before ``tail_start`` with a summary; keep the tail.

        ``tail_start`` is first snapped to a non-tool boundary so the kept
        tail never begins with an orphaned ``tool`` result. The summary lives
        as the head system message, so the next compaction can merge into it.
        """
        if tail_start <= 0 or tail_start >= len(self.messages):
            return False
        tail_start = self._coalesce_tail_start(tail_start)
        if tail_start <= 0:
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
        return True

    def _coalesce_tail_start(self, idx: int) -> int:
        """Snap a tail index back so the suffix never begins with a tool result.

        When ``idx`` points at a ``role=tool`` message, walk back to the
        assistant message that declared the call so the whole tool group is
        retained together — a suffix starting with a bare ``tool`` would be an
        orphan.
        """
        i = idx
        while i >= 0 and self.messages[i].get("role") == "tool":
            i -= 1
        return i if i >= 0 else idx

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

    def _drop_oldest_turn(self) -> bool:
        """Drop the oldest complete user turn (never a partial tool pair).

        A turn runs from one ``role=user`` message up to (not including) the
        next ``role=user`` message. Dropping the oldest turn therefore never
        separates an assistant ``tool_calls`` message from its ``tool``
        results. When fewer than two turns exist we only drop messages that
        precede the first user message (defensive) — a single in-flight turn
        is never removed, so tool results are not orphaned.

        Returns True when at least one message was dropped.
        """
        turns = self.turns()
        if len(turns) >= 2:
            start = turns[0][0]
            end = turns[1][0]
            del self.messages[start:end]
            return True
        first_user = next((i for i, m in enumerate(self.messages) if m.get("role") == "user"), None)
        if first_user is None:
            # No user boundary: drop a single leading orphaned message.
            if self.messages:
                self.messages.pop(0)
                return True
            return False
        # Exactly one turn remains; dropping it could orphan tool results.
        return False

    def _split_turn_start(self, start: int, end: int, remaining: int) -> int | None:
        """First index in ``[start, end)`` whose suffix fits ``remaining`` tokens.

        Only cuts at a message that can begin a suffix without orphaning a
        ``tool`` result: a cut that lands on a ``role=tool`` message is
        skipped because its preceding assistant (which declared the call)
        would be dropped, leaving an unpaired tool.
        """
        if end - start <= 1:
            return None
        for s in range(start + 1, end):
            if self.messages[s].get("role") == "tool":
                continue  # a cut here would orphan the tool result
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
