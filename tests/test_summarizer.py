"""Summarizer template + rolling-summary merge tests."""

from __future__ import annotations

import pytest

from easycode.agent.summarizer import build_prompt, SUMMARY_TEMPLATE, LLMSummarizer


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


# ---------------------------------------------------------------- item-06 (MS-5)


async def test_summarize_returns_none_on_provider_error(monkeypatch) -> None:
    """MS-5: an LLM failure must surface as ``None`` so the caller can fall back,
    never as a fabricated ``(summary unavailable...)`` note that would replace
    the original conversation text."""
    import easycode.agent.summarizer as sm

    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(sm, "acompletion", boom)
    s = LLMSummarizer("fake/model")
    out = await s.summarize([{"role": "user", "content": "the real conversation"}])
    assert out is None


async def test_summarize_returns_content_on_success(monkeypatch) -> None:
    import easycode.agent.summarizer as sm

    class Resp:
        class Choice:
            class Message:
                content = "a real summary"
            message = Message()
        choices = [Choice()]

    async def ok(*args, **kwargs):
        return Resp()

    monkeypatch.setattr(sm, "acompletion", ok)
    s = LLMSummarizer("fake/model")
    out = await s.summarize([{"role": "user", "content": "x"}])
    assert out == "a real summary"
