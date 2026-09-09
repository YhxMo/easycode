"""Compaction-fidelity contract tests.

Invariant #6: the compaction path (prune + condense) must not silently lose
task state.  Given a faithful summarizer that reports the five contract
categories — unfinished objective, constraints, changed files, tool side
effects, and the next step — the resulting ``summary + retained tail`` must let
a recoverer reconstruct all five.

The failure matrix (None / exception / oversized garbage / field-missing
summary) pins the system's behavior to "conservative trim + never fabricate
summary text", aligned with the A4 gate: an over-budget payload after
compaction must raise :class:`BudgetExceededError` *before* the provider call
(that is a legal, documented outcome rather than a silent truncation).

The scenario only exercises ``Agent`` and :class:`~easycode.agent.context.History`
over the real ``_condense_if_over_budget`` path — no product code is touched
here.
"""

from __future__ import annotations

import pytest

import easycode.agent.loop as loop
from easycode.agent.context import History
from easycode.agent.loop import PRUNED_OUTPUT, Agent, BudgetExceededError
from easycode.tools import build_registry
from tests.conftest import FakeProvider

# Contract markers (must survive a summary + tail reconstruction).
GOAL = "迁移 parser.py 支持 AST 新节点"
CONSTRAINT = "保持 Python 3.10 兼容"
CHANGED_FILE = "parser.py"
SIDE_EFFECT_SHELL = "写入 config.json"
SIDE_EFFECT_SKILL = "safety"
NEXT = "运行回归测试"

FIVE_FIELD_SUMMARY = (
    "## Objective\n"
    f"- {GOAL}（未完成）\n"
    "## Important Details\n"
    f"- 约束：{CONSTRAINT}\n"
    "## Work State\n"
    "### Completed\n"
    f"- 已改文件：{CHANGED_FILE}\n"
    "### Active\n"
    f"- 工具副作用：已注入技能 {SIDE_EFFECT_SKILL}\n"
    "## Next Move\n"
    f"- {NEXT}\n"
)


def build_agent(tmp_path) -> Agent:
    """A multi-turn agent whose history holds all five contract categories and
    whose budget forces the real prune + condense path."""
    agent = Agent(
        provider=FakeProvider(model="fake", script=[{"text": "ok"}]),
        registry=build_registry(8000),
        root=tmp_path,
        max_context_tokens=100_000,
    )
    agent.history = History(max_tokens=100_000)
    agent.history.set_system("sys")
    # turn 0 (old -> summarized): unfinished objective + constraint + edit result
    agent.history.add_user(f"目标：{GOAL}；约束：{CONSTRAINT}")
    agent.history.add(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "edit1",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": '{"path": "parser.py", "old_string": "old", "new_string": "new"}',
                    },
                }
            ],
        }
    )
    agent.history.add_tool("edit1", "edit_file", "x" * 400)  # big old output -> pruned
    agent.history.add_assistant(f"已将 {CHANGED_FILE} 改为暂存方案（未完成）")
    # turn 1 (old -> summarized): protected tool side effect (use_skill injection)
    agent.history.add_user("加载安全检查技能")
    agent.history.add(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "sk1",
                    "type": "function",
                    "function": {"name": "use_skill", "arguments": '{"name": "safety"}'},
                }
            ],
        }
    )
    agent.history.add_tool("sk1", "use_skill", '{"status": "ok", "skill": "safety", "loaded": true}')
    agent.history.add({"role": "system", "content": "[skill: safety]\ncheck body"})
    # turn 2 (recent -> retained tail): approved external shell write + next step
    agent.history.add_user(f"批准执行 shell {SIDE_EFFECT_SHELL}")
    agent.history.add(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "sh1",
                    "type": "function",
                    "function": {
                        "name": "execute_shell",
                        "arguments": '{"command": "echo hi > config.json"}',
                    },
                }
            ],
        }
    )
    agent.history.add_tool("sh1", "execute_shell", f"已{SIDE_EFFECT_SHELL}")
    agent.history.add_assistant(f"下一步：{NEXT}")
    # deterministic over-budget + a small recent tail
    agent.history.max_chars = 200
    agent.compaction["preserve_recent_tokens"] = 500
    agent.compaction["tail_turns"] = 1
    return agent


def _summary_and_tail(history: History) -> tuple[str, list[dict]]:
    """Split a post-compaction history into (summary text, tail messages)."""
    assert history.messages and History.is_summary(history.messages[0])
    return (
        str(history.messages[0].get("content") or ""),
        history.messages[1:],
    )


def _recovered(history: History) -> dict[str, bool]:
    """The recoverer: does ``summary + tail`` carry each of the five categories?"""
    summary, tail = _summary_and_tail(history)
    blob = summary + "\n" + "\n".join(str(m.get("content") or "") for m in tail)
    return {
        "objective": GOAL in blob,
        "constraint": CONSTRAINT in blob,
        "changed_file": CHANGED_FILE in blob,
        "side_effect": (SIDE_EFFECT_SHELL in blob) or (SIDE_EFFECT_SKILL in blob),
        "next_step": NEXT in blob,
    }


# ---------------------------------------------------------------- positive path


async def test_compaction_recovers_all_five_categories(tmp_path, monkeypatch):
    """A faithful summarizer + retained tail must recover all five contract
    categories after the real prune + condense path."""
    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    agent = build_agent(tmp_path)

    recorded: list[list[dict]] = []

    async def faithful_summarize(messages, previous_summary=None):
        recorded.append(list(messages))
        return FIVE_FIELD_SUMMARY

    agent.summarizer = faithful_summarize

    await agent._condense_if_over_budget()

    # prune ran BEFORE summarize: the summarizer saw the big old edit result
    # already cleared to the protected marker.
    assert recorded, "the summarizer must be invoked"
    assert any(m.get("content") == PRUNED_OUTPUT for m in recorded[0])
    # ...but the goal / constraint / changed-file context was still available to it.
    transcript = " ".join(str(m.get("content") or "") for m in recorded[0])
    assert GOAL in transcript and CONSTRAINT in transcript and CHANGED_FILE in transcript

    # summary injected + recorded for rolling merge
    assert agent.history.summary == FIVE_FIELD_SUMMARY
    summary, tail = _summary_and_tail(agent.history)
    assert FIVE_FIELD_SUMMARY in summary

    # tail assertions: the recent shell-write + next-step turn was retained; the
    # old changed-file context was summarized away (so the summary is the carrier).
    tail_text = " ".join(str(m.get("content") or "") for m in tail)
    assert SIDE_EFFECT_SHELL in tail_text and NEXT in tail_text
    assert CHANGED_FILE not in tail_text

    # recovery: all five categories are present in summary + tail
    recovered = _recovered(agent.history)
    assert recovered == {
        "objective": True,
        "constraint": True,
        "changed_file": True,
        "side_effect": True,
        "next_step": True,
    }, recovered


# ---------------------------------------------------------------- failure matrix


async def test_summarizer_none_falls_back_to_trim_without_fabrication(tmp_path, monkeypatch):
    """None summary: conservative trim path — no injected summary, no fabricated
    degrade text."""
    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    agent = build_agent(tmp_path)

    async def returning_none(messages, previous_summary=None):
        return None

    agent.summarizer = returning_none
    before = len(agent.history.messages)

    await agent._condense_if_over_budget()

    assert agent.history.summary is None  # never fabricated a summary
    for m in agent.history.messages:
        assert "summary unavailable" not in str(m.get("content") or "")
    # conservative: history no longer holds the full (over-budget) message list
    assert len(agent.history.messages) < before


async def test_summarizer_exception_propagates_without_fabrication(tmp_path, monkeypatch):
    """A raising summarizer surfaces the failure without injecting fabricated
    summary text."""
    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    agent = build_agent(tmp_path)

    async def exploding(messages, previous_summary=None):
        raise RuntimeError("summarizer blew up")

    agent.summarizer = exploding

    with pytest.raises(RuntimeError, match="summarizer blew up"):
        await agent._condense_if_over_budget()

    assert agent.history.summary is None
    for m in agent.history.messages:
        assert "summary unavailable" not in str(m.get("content") or "")


async def test_oversized_summary_trips_budget_gate(tmp_path, monkeypatch):
    """Oversized garbage summary: the system injects it verbatim (never
    fabricating) and the A4 final gate raises BudgetExceededError *before* the
    provider call — a legal, explicit outcome rather than silent truncation."""
    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    agent = build_agent(tmp_path)

    garbage = "x" * 20_000

    async def bloat(messages, previous_summary=None):
        return garbage

    agent.summarizer = bloat

    await agent._condense_if_over_budget()
    assert agent.history.summary == garbage  # injected as-is, not fabricated

    with pytest.raises(BudgetExceededError):
        agent._raise_if_over_budget()


async def test_field_missing_summary_injected_verbatim_without_fabrication(tmp_path, monkeypatch):
    """A summary missing fields is passed through verbatim; the system never
    invents filler to replace absent fields."""
    monkeypatch.setattr(loop, "PRUNE_PROTECT", 20)
    monkeypatch.setattr(loop, "PRUNE_MINIMUM", 5)
    agent = build_agent(tmp_path)

    # Deliberately omits side-effect and next-step fields.
    partial = "## Objective\n- 迁移 parser.py 支持 AST 新节点\n"

    async def partial_summarize(messages, previous_summary=None):
        return partial

    agent.summarizer = partial_summarize

    await agent._condense_if_over_budget()

    assert agent.history.summary == partial
    summary, _tail = _summary_and_tail(agent.history)
    assert partial in summary
    # no fabricated degrade/invented text was added to pad the gap
    for m in agent.history.messages:
        assert "summary unavailable" not in str(m.get("content") or "")
        assert "[summary]" not in str(m.get("content") or "")
