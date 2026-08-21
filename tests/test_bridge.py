"""Bridge throttling tests: token-level text is coalesced into readable chunks."""

from __future__ import annotations

import json
from pathlib import Path

from easycode.agent.loop import Agent
from easycode.tools import build_registry
from easycode.web.bridge import stream_chat
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
    return [line async for line in stream_chat(agent, msg, **kw)]


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
    assert texts == WORD