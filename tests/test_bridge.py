"""Bridge throttling tests: token-level text is coalesced into readable chunks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from easycode.agent.loop import Agent
from easycode.models.base import StreamEvent
from easycode.tools import build_registry
from easycode.web.bridge import ApprovalBroker, event_to_sse, stream_chat_with_approval
from tests.conftest import FakeProvider

WORD = "found"
BIG = "x" * 400


def make_agent(tmp_path: Path, text: str) -> Agent:
    # script yields a text turn; the provider emits one char per StreamEvent
    # by returning text per token here (each char becomes a text event)
    provider = FakeProvider(script=[{"text": text}])
    return Agent(provider=provider, registry=build_registry(8000), root=tmp_path)


def parse_events(lines: list[str]) -> list[dict]:
    return [json.loads(line[6:]) for line in lines if line.startswith("data:")]


async def run_chat(agent: Agent, msg: str, **kw) -> list[str]:
    broker = ApprovalBroker()
    lines: list[str] = []
    async for kind, payload in stream_chat_with_approval(agent, msg, broker, **kw):
        assert kind in {"event", "approval"}
        if kind == "event":
            lines.append(event_to_sse(payload))
    return lines


async def test_short_text_emitted_as_single_event(tmp_path):
    agent = make_agent(tmp_path, WORD)
    lines = await run_chat(agent, "hi", flush_chars=64)
    events = parse_events(lines)
    texts = [e["content"] for e in events if e["type"] == "text"]
    assert "".join(texts) == WORD
    assert len(texts) == 1


async def test_long_text_coalesced_into_few_chunks(tmp_path):
    agent = make_agent(tmp_path, BIG)
    lines = await run_chat(agent, "hi", flush_chars=64)
    events = parse_events(lines)
    texts = [e["content"] for e in events if e["type"] == "text"]
    joined = "".join(texts)
    assert joined == BIG
    assert len(texts) <= 8  # 400 chars / 64 = ~7 chunks, not 400 events


async def test_tool_events_are_not_coalesced(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    agent = Agent(
        provider=FakeProvider(
            script=[
                {"tool_calls": [("c1", "glob", {"pattern": "*.py"})], "text": ""},
                {"text": WORD},
            ]
        ),
        registry=build_registry(8000),
        root=tmp_path,
    )
    lines = await run_chat(agent, "list")
    events = parse_events(lines)
    kinds = [e["type"] for e in events]
    assert kinds.count("tool_start") == 1
    assert kinds.count("tool_result") == 1
    assert kinds[-1] == "done"
    texts = "".join(e["content"] for e in events if e["type"] == "text")
    assert texts == WORD    # text flushes before the tool call, and the final answer lands before done
    assert kinds.index("tool_start") < kinds.index("tool_result") < kinds.index("text")


async def test_approval_required_event_carries_manager_reason(tmp_path):
    """The approval prompt uses the reason the loop already computed."""
    from easycode.mcp import MCPConnection, MCPSession, MCPSessionManager, mcp_tool_name

    fname = mcp_tool_name("demo", "danger")
    mgr = MCPSessionManager({})
    sess = MCPSession("demo", MCPConnection("demo", {}))
    sess.tools = {
        fname: {
            "name": "danger",
            "schema": {
                "type": "function",
                "function": {"name": fname, "description": "danger", "parameters": {}},
            },
            "annotations": {},
        }
    }
    mgr._sessions = {"demo": sess}

    agent = Agent(
        provider=FakeProvider(
            script=[
                {"tool_calls": [("c1", fname, {})], "text": ""},
                {"text": WORD},
            ]
        ),
        registry=build_registry(8000),
        root=tmp_path,
        mcp_manager=mgr,
    )
    broker = ApprovalBroker()
    reasons: list[str] = []
    async for kind, payload in stream_chat_with_approval(agent, "go", broker):
        if kind == "approval":
            approval_id, _tc, reason, _scope = payload
            reasons.append(reason)
            broker.resolve(approval_id, False)
    assert reasons == [mgr.approval_reason(fname)]


class PausingProvider(FakeProvider):
    """Streams some text, then blocks until cancelled (never finishes the turn)."""

    def __init__(self, text: str, started: asyncio.Event) -> None:
        super().__init__()
        self.text = text
        self.started = started

    async def stream(self, messages, tools=None):
        for token in self.text:
            yield StreamEvent(kind="text", content=token)
        self.started.set()
        await asyncio.Event().wait()


async def test_cancel_flushes_buffered_text_then_cancelled(tmp_path):
    started = asyncio.Event()
    cancel = asyncio.Event()
    agent = Agent(
        provider=PausingProvider(WORD, started),
        registry=build_registry(8000),
        root=tmp_path,
    )

    async def trigger():
        await started.wait()
        # let the bridge drain the queue into its buffer before cancelling
        for _ in range(20):
            await asyncio.sleep(0)
        cancel.set()

    trig = asyncio.create_task(trigger())
    # large thresholds keep the partial text buffered until cancellation
    lines = await run_chat(agent, "hi", flush_chars=10_000, flush_seconds=60, cancel_event=cancel)
    await trig
    events = parse_events(lines)
    assert events == [{"type": "text", "content": WORD}, {"type": "cancelled", "content": "cancelled"}]
