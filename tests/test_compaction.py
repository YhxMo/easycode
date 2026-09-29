"""Compaction tests."""

from __future__ import annotations

import json

import pytest

from easycode.config import Config
from easycode.models.credentials import Credential, save_credential
from tests.conftest import fake_agent


def test_history_token_budget():
    from easycode.agent.context import History

    h = History(max_tokens=1000)
    assert not h.over_budget()
    h.add_user("x" * 20_000)  # ~5000 tokens heuristic > 1000
    assert h.over_budget()


def test_history_trim_fallback_without_summarizer():
    from easycode.agent.context import History

    h = History(max_tokens=1000)
    for i in range(30):
        h.add_user(f"msg {i} " + "y" * 500)
    h.trim()
    assert h.estimate_tokens() <= 1000 or len(h.messages) <= 3
    assert len(h.messages) < 30


@pytest.mark.asyncio
async def test_agent_summarizer_wired_in_loop(tmp_path):
    """Over-budget turns invoke the summarizer and keep a token-selected tail."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    script = [
        {"tool_calls": [("c1", "read_file", {"path": "nope"})], "text": ""},
        {"text": "final answer"},
    ]
    from easycode.agent.context import History

    agent = Agent(
        provider=FakeProvider(model="fake", script=script),
        registry=build_registry(8000),
        root=tmp_path,
        summarizer=None,
        max_context_tokens=100_000,
    )
    agent.history = History(max_tokens=100_000)
    agent.history.set_system("sys")
    agent.history.add_user("old " + "z" * 20_000)  # old turn to summarize
    agent.history.add_assistant("old answer")
    agent.history.add_user("recent question")  # recent turn fits the tail budget
    agent.history.add_assistant("recent answer")
    agent.history.max_chars = 1_000  # force over-budget before the turn
    agent.compaction["preserve_recent_tokens"] = 2_000

    async def fake_summarize(messages, previous_summary=None):
        return "[synthetic summary]"

    agent.summarizer = fake_summarize
    async for _ in agent.respond("do it"):
        pass
    contents = " ".join(str(m.get("content", "")) for m in agent.history.messages)
    assert "synthetic summary" in contents
    assert "recent answer" in contents


@pytest.mark.asyncio
async def test_agent_no_summarizer_hard_trim(tmp_path):
    from easycode.agent.context import History
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from tests.conftest import FakeProvider

    yuge = "z" * 30_000
    agent = Agent(
        provider=FakeProvider(model="fake", script=[{"text": "ok"}]),
        registry=build_registry(8000),
        root=tmp_path,
        max_context_tokens=1000,
    )
    agent.history = History(max_tokens=1000)
    agent.history.set_system("sys")
    agent.history.add_user(yuge)
    agent.history.trim()
    assert len(agent.history.messages) <= 2


def test_usable_tokens_reserves_output_buffer(tmp_path):

    a = fake_agent(
        tmp_path,
        max_context_tokens=32_000,
        model_limits={"context": 100_000, "output": 8_000},
    )
    # default buffer 20_000 → reserved = min(20_000, 8_000) = 8_000
    assert a.compactor.usable_tokens(a.max_context_tokens, a.model_limits) == 100_000 - 8_000

    b = fake_agent(tmp_path, max_context_tokens=32_000)
    assert b.model_limits is None
    assert (
        b.compactor.usable_tokens(b.max_context_tokens, b.model_limits) == 32_000
    )  # unknown model → no reserve


def test_prune_clears_old_tool_outputs(tmp_path, monkeypatch):
    import easycode.agent.compaction as compaction

    monkeypatch.setattr(compaction, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 5)
    from easycode.agent.compaction import PRUNED_OUTPUT

    agent = fake_agent(tmp_path)
    h = agent.history
    h.add_user("q1")
    h.add_tool("1", "read_file", "x" * 400)
    h.add_user("q2")
    h.add_tool("2", "read_file", "y" * 400)
    h.add_user("q3")
    h.add_tool("3", "read_file", "z" * 400)

    agent.compactor.prune(agent.history)

    assert h.messages[1]["content"] == PRUNED_OUTPUT  # oldest cleared
    assert h.messages[3]["content"] == "y" * 400  # last 2 turns protected
    assert h.messages[5]["content"] == "z" * 400


def test_prune_protects_skill_output(tmp_path, monkeypatch):
    import easycode.agent.compaction as compaction

    monkeypatch.setattr(compaction, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(compaction, "PRUNE_MINIMUM", 5)

    agent = fake_agent(tmp_path)
    h = agent.history
    h.add_user("q1")
    h.add_tool("1", "use_skill", "x" * 400)
    h.add_user("q2")
    h.add_tool("2", "read_file", "y" * 400)
    h.add_user("q3")
    h.add_tool("3", "read_file", "z" * 400)

    agent.compactor.prune(agent.history)

    assert h.messages[1]["content"] == "x" * 400  # skill output never cleared
    assert h.messages[3]["content"] == "y" * 400
    assert h.messages[5]["content"] == "z" * 400


@pytest.mark.asyncio
async def test_summarizer_merges_previous_summary(tmp_path, monkeypatch):
    """The loop passes the prior summary to LLMSummarizer for rolling merge."""
    from easycode.agent.summarizer import LLMSummarizer

    captured = {}

    async def fake_summarize(self, messages, previous_summary=None):
        captured["previous"] = previous_summary
        return "[merged]"

    monkeypatch.setattr(LLMSummarizer, "__call__", fake_summarize)

    agent = fake_agent(
        tmp_path,
        [{"text": "ok"}],
        summarizer=LLMSummarizer("fake/a"),
        max_context_tokens=100_000,
    )
    from easycode.agent.context import SUMMARY_PREFIX, History

    agent.history = History(max_tokens=100_000)
    agent.history.set_system("sys")
    agent.history.add({"role": "system", "content": f"{SUMMARY_PREFIX}\n[first summary]"})
    agent.history.add_user("old " + "z" * 20_000)
    agent.history.add_assistant("old answer")
    agent.history.add_user("recent")
    agent.history.add_assistant("recent answer")
    agent.history.max_chars = 1_000
    agent.compaction["preserve_recent_tokens"] = 2_000

    async for _ in agent.respond("do it"):
        pass
    assert captured.get("previous") == "[first summary]"
    assert "[merged]" in " ".join(str(m.get("content", "")) for m in agent.history.messages)


@pytest.mark.asyncio
async def test_summarizer_transcript_skips_prior_summary(tmp_path):
    """10A: the prior summary reaches the summarizer once (previous_summary),
    not a second time as the head of the transcript."""
    from easycode.agent.context import SUMMARY_PREFIX

    captured: dict = {}

    async def fake_summarize(messages, previous_summary=None):
        captured["messages"] = list(messages)
        captured["previous"] = previous_summary
        return "[merged]"

    agent = fake_agent(
        tmp_path,
        [{"text": "ok"}],
        summarizer=fake_summarize,
        max_context_tokens=100_000,
    )
    h = agent.history
    h.max_chars = 1_000
    h.add({"role": "system", "content": f"{SUMMARY_PREFIX}\n[first summary]"})
    h.add_user("old " + "z" * 20_000)
    h.add_assistant("old answer")
    h.add_user("recent")
    h.add_assistant("recent answer")
    agent.compaction["preserve_recent_tokens"] = 2_000

    await agent.compactor.condense(agent.history, agent.summarizer)

    assert captured["previous"] == "[first summary]"
    assert all("[first summary]" not in str(m.get("content", "")) for m in captured["messages"])


def test_make_agent_wires_summarizer(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(
        json.dumps({"models": {"fake-a": {"model": "fake/a", "key_id": "fake-key"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    save_credential(
        Credential(key_id="fake-key", api_key="sk-fake"),
        path=tmp_path / ".easycode" / "credentials.json",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    cfg.root = tmp_path

    from easycode.agent.factory import make_agent

    agent = make_agent(cfg, "fake-a", tmp_path)
    assert agent.summarizer is not None
    assert agent.summarizer.model == "fake/a"
    assert agent.max_context_tokens == 32_000


def test_config_max_context_tokens(tmp_path, monkeypatch):
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"max_context_tokens": 9999}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.max_context_tokens == 9999
    cfg.save()
    assert "max_context_tokens" in cfg_file.read_text(encoding="utf-8")
