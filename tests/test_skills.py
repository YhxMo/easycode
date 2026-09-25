"""Phase 6 tests: Skill loading, discovery, use_skill tool, /name trigger."""

from __future__ import annotations

import json

import pytest

from easycode.agent.loop import Agent
from easycode.skills import SkillRegistry
from easycode.tools import build_registry
from tests.conftest import FakeProvider
from tests.helpers_history import assert_valid_tool_protocol


def test_skill_discovery_and_override(tmp_path):
    user_skills = tmp_path / "user_skills"
    user_skills.mkdir(parents=True)
    proj_skills = tmp_path / "proj" / ".easycode" / "skills"
    proj_skills.mkdir(parents=True)

    # User skill
    (user_skills / "doc-gen").mkdir()
    (user_skills / "doc-gen" / "SKILL.md").write_text(
        """---
description: Generate docstrings
---
User doc instructions
""",
        encoding="utf-8",
    )

    # Project override skill
    (proj_skills / "doc-gen").mkdir()
    (proj_skills / "doc-gen" / "SKILL.md").write_text(
        """---
description: Project docstrings
---
Project doc instructions
""",
        encoding="utf-8",
    )

    # Project unique skill
    (proj_skills / "test-writer").mkdir()
    (proj_skills / "test-writer" / "SKILL.md").write_text(
        """---
description: Write pytest tests
---
Write tests instructions
""",
        encoding="utf-8",
    )

    reg = SkillRegistry.discover([tmp_path / "proj"], user_dir=user_skills)
    assert sorted(reg.names()) == ["doc-gen", "test-writer"]

    doc_skill = reg.get("doc-gen")
    assert doc_skill is not None
    assert doc_skill.description == "Project docstrings"
    assert doc_skill.source == "project"
    assert doc_skill.body == "Project doc instructions"


@pytest.mark.asyncio
async def test_use_skill_tool_injection(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "my-skill").mkdir()
    (skills_dir / "my-skill" / "SKILL.md").write_text(
        """---
description: Test skill
---
Special instructions to follow
""",
        encoding="utf-8",
    )
    reg = SkillRegistry.discover([], user_dir=skills_dir)

    script = [
        {
            "tool_calls": [
                ("s1", "use_skill", {"name": "my-skill"})
            ],
            "text": "",
        },
        {"text": "Skill applied."},
    ]

    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        skills=reg,
    )

    # Before use_skill, skill body is not in history
    assert not any("Special instructions to follow" in str(m.get("content")) for m in agent.history.messages)

    events = [ev async for ev in agent.respond("use skill")]
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    data = json.loads(results[0])
    assert data["status"] == "ok"
    assert data["skill"] == "my-skill"
    assert data["loaded"] is True
    assert "body" not in data  # P7-2: skill body is injected once via system message

    # After use_skill, system message is injected into history
    assert any("[skill: my-skill]" in str(m.get("content")) for m in agent.history.messages)
    assert any("Special instructions to follow" in str(m.get("content")) for m in agent.history.messages)


@pytest.mark.asyncio
async def test_use_skill_in_batch_keeps_tool_protocol(tmp_path):
    """A skill loaded in a multi-tool batch is injected after all tool results."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "safety").mkdir()
    (skills_dir / "safety" / "SKILL.md").write_text(
        """---
description: Safety skill
---
Safety instructions
""",
        encoding="utf-8",
    )
    reg = SkillRegistry.discover([], user_dir=skills_dir)
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    script = [
        {
            "tool_calls": [
                ("s1", "use_skill", {"name": "safety"}),
                ("g1", "glob", {"pattern": "*.py"}),
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        skills=reg,
    )

    events = [ev async for ev in agent.respond("load skill and list files")]
    assert events[-1].kind == "done"

    payload = agent.history.payload()
    assistant_idx = next(
        i for i, m in enumerate(payload) if m["role"] == "assistant" and m.get("tool_calls")
    )
    assert [m["role"] for m in payload[assistant_idx + 1 : assistant_idx + 3]] == [
        "tool",
        "tool",
    ]
    assert payload[assistant_idx + 3]["role"] == "system"
    assert "[skill: safety]" in payload[assistant_idx + 3]["content"]
    assert_valid_tool_protocol(payload)


@pytest.mark.asyncio
async def test_use_skill_unknown_error(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "known").mkdir()
    (skills_dir / "known" / "SKILL.md").write_text(
        """---
description: Known skill
---
Known body
""",
        encoding="utf-8",
    )
    reg = SkillRegistry.discover([], user_dir=skills_dir)
    script = [
        {
            "tool_calls": [
                ("s1", "use_skill", {"name": "nonexistent"})
            ],
            "text": "",
        },
        {"text": "done"},
    ]
    agent = Agent(
        provider=FakeProvider(script=script),
        registry=build_registry(8000),
        root=tmp_path,
        skills=reg,
    )

    events = [ev async for ev in agent.respond("use nonexistent")]
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    data = json.loads(results[0])
    assert data["status"] == "error"
    assert "unknown skill" in data["message"]
