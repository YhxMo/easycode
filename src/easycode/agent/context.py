"""Conversation history management."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from easycode.models.base import Message

SummarizerFn = Callable[[list[Message]], str]


@dataclass
class History:
    """Bounded message history (OpenAI-format dicts).

    Budgets: ``max_tokens`` (model token estimate, primary) and
    ``max_chars`` (backstop for non-token-countable models).
    """

    system: Message | None = None
    messages: list[Message] = field(default_factory=list)
    max_messages: int = 100
    max_chars: int = 400_000
    max_tokens: int = 32_000

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
        """Token estimate via litellm if possible, else ≈ chars/4 heuristic."""
        try:
            import litellm

            return litellm.token_counter(messages=self.payload() or [{"role": "user", "content": ""}])
        except Exception:  # noqa: BLE001 - heuristic fallback
            return max(1, self._estimate_tokens_cheap())

    def _estimate_tokens_cheap(self) -> int:
        """Tokenizer-aware char heuristic without a litellm call.

        CJK and other non-ASCII characters (kana, hangul, emoji…) tokenize
        ≈ 1 token/char while ASCII is closer to 4 chars/token. A flat
        chars/4 assumption badly under-counts non-ASCII-heavy transcripts,
        so count non-ASCII separately.
        """
        text = "".join(str(m.get("content") or "") for m in self.messages)
        text += "".join(
            str(tc.get("function", {}).get("arguments") or "")
            for m in self.messages
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

    def trim(self) -> None:
        """Drop oldest messages beyond the limits (no summarization)."""
        while len(self.messages) > self.max_messages:
            self.messages.pop(0)
        total = self.estimate_chars()
        while total > self.max_chars and len(self.messages) > 2:
            dropped = self.messages.pop(0)
            total -= len(str(dropped.get("content") or ""))
        while self.estimate_tokens() > self.max_tokens and len(self.messages) > 2:
            self.messages.pop(0)

    def condense(self, summary: str, keep_recent: int = 20) -> bool:
        """Replace everything older than ``keep_recent`` messages with a summary.

        The summary is stored as a leading system-style message so the model
        keeps memory of the conversation's early history.
        Returns True if condensation happened.
        """
        if len(self.messages) <= keep_recent + 1:
            return False
        old = self.messages[: -keep_recent]
        recent = self.messages[-keep_recent:]
        if not old:
            return False
        summary_msg: Message = {
            "role": "system",
            "content": f"Previous conversation summary (older messages were condensed):\n{summary}",
        }
        self.messages = [summary_msg, *recent]
        return True

    def summarizable_chunk(self, keep_recent: int = 20, max_chars: int = 60_000) -> tuple[list[Message], bool]:
        """Return the oldest messages eligible for condensation (and whether any exist)."""
        if len(self.messages) <= keep_recent + 1:
            return [], False
        old = self.messages[: -keep_recent]
        total = 0
        chunk: list[Message] = []
        for m in old:
            size = len(str(m.get("content") or ""))
            if chunk and total + size > max_chars:
                break
            chunk.append(m)
            total += size
        return chunk, bool(chunk)

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