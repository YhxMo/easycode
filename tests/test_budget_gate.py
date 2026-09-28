"""Budget gate must fail *before* the provider is called.

The loop compacts when over budget, but when there is no tool boundary to cut
(a single huge user turn) compaction cannot reduce the payload. In that case
invariant #5 requires the loop to fail explicitly before calling the provider
instead of sending the oversized payload and relying on the vendor to reject it.
"""

from __future__ import annotations

import pytest

from easycode.agent.compaction import BudgetExceededError, Compactor
from easycode.agent.context import History
from easycode.agent.loop import Agent
from easycode.tools import build_registry
from tests.conftest import FakeProvider


def make_agent(tmp_path, script: list[dict] | None = None):
    provider = FakeProvider(script=list(script or []))
    agent = Agent(provider=provider, registry=build_registry(8000), root=tmp_path)
    return agent, provider


async def test_single_oversized_turn_fails_before_provider(tmp_path):
    """One huge user message with no tool boundary to cut → the gate fires
    before provider.stream, so provider.calls stays empty."""
    agent, provider = make_agent(tmp_path)
    agent.history.max_tokens = 50
    agent.history.max_chars = 10_000_000

    events: list = []
    with pytest.raises(BudgetExceededError, match="预算"):
        async for ev in agent.respond("x" * 2000):
            events.append(ev)

    # the provider was never reached (fail before the call)
    assert provider.calls == []
    # the failure is surfaced as a user-visible error event, not swallowed
    errs = [e for e in events if e.kind == "error"]
    assert errs and "预算" in errs[0].error
    # The failed prompt stays visible; no tool calls were added.
    assert agent.history.messages == [{"role": "user", "content": "x" * 2000}]


async def test_gate_fires_even_when_compaction_auto_false(tmp_path):
    """`compaction.auto=False` opts out of automatic compaction, but the
    final budget gate must STILL reject an over-budget payload before the
    provider call."""
    agent, provider = make_agent(tmp_path)
    agent.compaction["auto"] = False
    agent.history.max_tokens = 50
    agent.history.max_chars = 10_000_000

    with pytest.raises(BudgetExceededError):
        async for ev in agent.respond("x" * 2000):
            pass

    assert provider.calls == []


async def test_small_message_path_unaffected(tmp_path):
    """A message well under budget still reaches the provider exactly once —
    the gate must not reject healthy turns."""
    agent, provider = make_agent(tmp_path, [{"text": "hello"}])
    agent.history.max_tokens = 1_000_000

    events = [ev async for ev in agent.respond("hi")]

    assert len(provider.calls) == 1
    assert "".join(e.content or "" for e in events if e.kind == "text") == "hello"


async def test_budget_error_reaches_web_sse_channel(tmp_path):
    """A4: the pre-call budget failure must reach the Web SSE channel as an
    error event (not a hang or a silent empty stream) — the bridge forwards the
    generic agent error event to the client."""
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval

    agent, provider = make_agent(tmp_path)
    agent.history.max_tokens = 50
    agent.history.max_chars = 10_000_000

    broker = ApprovalBroker(timeout=1.0)
    events: list = []
    async for kind, payload in stream_chat_with_approval(agent, "x" * 2000, broker):
        if kind == "event":
            events.append(payload)

    assert provider.calls == []
    errs = [e for e in events if e.kind == "error"]
    assert errs and "预算" in errs[0].error


@pytest.mark.asyncio
async def test_prepare_checks_an_under_budget_history_once(monkeypatch):
    """The common small-history path must estimate the budget a single time.

    The gate is called on every turn, so a redundant second estimate is paid
    on every message, not just when compaction fires.
    """
    history = History(max_tokens=32_000)
    history.add_user("small request")
    calls = 0
    original = History.over_budget

    def counted(self, extra=0):
        nonlocal calls
        calls += 1
        return original(self, extra)

    monkeypatch.setattr(History, "over_budget", counted)
    await Compactor({}).prepare(history, None, extra=100)
    assert calls == 1
