"""Shared history-protocol assertion helpers.

This module is intentionally *not* a pytest collection target — it exposes
importable helpers only, so its functions are never counted as tests.

``assert_valid_tool_protocol`` upgrades the weak ``assert_no_orphan_tools``
membership check (it only proved every ``tool`` result's id appeared in *some*
earlier assistant) into a strict one-to-one pairing contract:

* every assistant ``tool_call`` id must be consumed by exactly one ``tool``
  result, in declaration (stream) order;
* a duplicated result, a missing result, a re-used id from an older turn, or a
  result that appears *before* its declaring assistant are all rejected;
* ``system`` messages are exempt only when no result is pending: one injected
  between a declaring assistant and its tool results breaks the protocol
  (OpenAI-compatible providers require the results to follow immediately).

The companion ``validated_payload`` wrapper lets test cases enforce the
invariant at a payload boundary, and ``ValidatingFakeProvider`` wires the same
predicate into a ``FakeProvider`` subclass so the check runs at the provider's
payload exit.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from easycode.models.base import Message
from tests.conftest import FakeProvider


class ProtocolViolation(AssertionError):
    """Raised when a message sequence violates the one-to-one tool protocol.

    Subclasses :class:`AssertionError` so ``pytest.raises(AssertionError)``
    catches it while still being descriptively typeable.
    """


def assert_valid_tool_protocol(messages: list[Message]) -> None:
    """Assert one-to-one ``tool_call`` <-> ``tool``-result pairing.

    Walks the message sequence once, consuming declared ids on the fly:

    * an assistant ``tool_calls`` batch enqueues its ids in order;
    * a ``tool`` result must be the *next* enqueued id → enforces both the
      declaration-order consumption rule and the "result must follow its
      declaring assistant" ordering;
    * an id that was already fulfilled is rejected (duplicate result, or a
      re-use of an old-turn id);
    * an id that is not yet declared is rejected (result precedes its
      assistant — interleaving);
    * at the end the pending queue must be empty, so no declared call is left
      unpaired (missing result).

    ``system`` messages are allowed only while no result is pending.  Raises
    :class:`ProtocolViolation` on the first violation; returns ``None`` when the
    sequence is fully paired.
    """
    pending: deque[str] = deque()
    fulfilled: set[str] = set()
    for m in messages:
        role = m.get("role")
        if role == "system":
            if pending:
                raise ProtocolViolation(
                    "system message interleaved between tool_calls and their results"
                )
            continue
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                cid = tc.get("id")
                if cid is None:
                    raise ProtocolViolation("assistant tool_call is missing an 'id'")
                if cid in fulfilled:
                    raise ProtocolViolation(
                        f"duplicate tool_call id {cid!r}: already fulfilled"
                    )
                pending.append(cid)
            continue
        if role == "tool":
            cid = m.get("tool_call_id")
            if cid is None:
                raise ProtocolViolation("tool result is missing a 'tool_call_id'")
            if cid in fulfilled:
                raise ProtocolViolation(
                    f"duplicate tool result for id {cid!r}: already fulfilled"
                )
            if not pending:
                raise ProtocolViolation(
                    f"orphan tool result {cid!r}: no pending declaration"
                )
            expected = pending.popleft()
            if cid != expected:
                raise ProtocolViolation(
                    f"tool result {cid!r} out of order: expected {expected!r}"
                )
            fulfilled.add(cid)
    if pending:
        raise ProtocolViolation(
            f"missing tool result(s) for {len(pending)} declared call(s): "
            f"{list(pending)}"
        )


def validated_payload(messages: list[Message]) -> list[Message]:
    """Return ``messages`` after asserting the tool protocol.

    Wraps :func:`assert_valid_tool_protocol` so a test case can enforce the
    invariant at an arbitrary payload boundary::

        payload = validated_payload(history.payload())
    """
    assert_valid_tool_protocol(messages)
    return messages


class ValidatingFakeProvider(FakeProvider):
    """A :class:`FakeProvider` that validates each payload at the provider exit.

    ``stream`` asserts the one-to-one tool protocol on ``messages`` *before*
    delegating to the base implementation, so a malformed history (missing /
    duplicate / old-turn / interleaved result) is refused at the farthest
    downstream boundary the loop can reach.
    """

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        assert_valid_tool_protocol(messages)
        async for ev in super().stream(messages, tools):
            yield ev
