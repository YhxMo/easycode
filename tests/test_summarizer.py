"""Summarizer template + rolling-summary merge tests."""

from __future__ import annotations

from easycode.agent.summarizer import build_prompt, SUMMARY_TEMPLATE


def test_build_prompt_new_summary() -> None:
    p = build_prompt(None, "the conversation")
    assert "<conversation>" in p and "the conversation" in p
    assert "<prior-summary>" not in p
    assert "## Objective" in p and "## Next Move" in p
    assert SUMMARY_TEMPLATE in p


def test_build_prompt_merges_prior_summary() -> None:
    p = build_prompt("OLD SUMMARY", "the new conversation")
    assert "<prior-summary>" in p
    assert "OLD SUMMARY" in p
    assert "Do not mention the summary process" in p