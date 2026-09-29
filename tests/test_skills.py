"""Phase 6 tests: Skill loading, discovery, use_skill tool, /name trigger."""

from __future__ import annotations

import json

import pytest

from easycode.extensions.skills import SkillRegistry
from tests.conftest import fake_agent
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

    agent = fake_agent(tmp_path, script, skills=reg)

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

    # After use_skill, the skill context is injected into history: the body, and
    # the directory its relative paths resolve against.
    injected = "\n".join(
        str(m.get("content")) for m in agent.history.messages if m.get("role") == "system"
    )
    assert "# Skill: my-skill" in injected
    assert "Special instructions to follow" in injected
    assert str(skills_dir / "my-skill" / "SKILL.md") in injected
    assert str(skills_dir / "my-skill") in injected


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
    agent = fake_agent(tmp_path, script, skills=reg)

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
    assert "# Skill: safety" in payload[assistant_idx + 3]["content"]
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
    agent = fake_agent(tmp_path, script, skills=reg)

    events = [ev async for ev in agent.respond("use nonexistent")]
    results = [e.tool_result for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    data = json.loads(results[0])
    assert data["status"] == "error"
    assert "unknown skill" in data["message"]


def test_skill_context_names_the_resource_directory(tmp_path):
    """Both loading paths must describe the same package the same way."""
    from easycode.extensions.commands import build_registry
    from easycode.extensions.skills import skill_context

    skills_dir = tmp_path / "skills"
    (skills_dir / "pack").mkdir(parents=True)
    (skills_dir / "pack" / "SKILL.md").write_text(
        """---
description: Packaged skill
---
Read references/guide.md first
""",
        encoding="utf-8",
    )
    reg = SkillRegistry.discover([], user_dir=skills_dir)
    skill = reg.get("pack")
    assert skill is not None and skill.path is not None
    assert skill.directory == skills_dir / "pack"

    context = skill_context(skill)
    assert str(skills_dir / "pack" / "SKILL.md") in context
    assert str(skills_dir / "pack") in context
    assert "Read references/guide.md first" in context

    # The same text is what ``/pack`` expands to, so a hand-run skill and a
    # model-loaded one cannot disagree about where their resources are.
    cmds = build_registry([], reg)
    cmd = cmds.get("pack")
    assert cmd is not None
    assert cmd.body == context


def test_skill_without_placeholders_keeps_the_task(tmp_path):
    """A skill body is instructions: the task typed after /name must survive."""
    from easycode.extensions.commands import Command

    skill = Command(name="review", description="Review", kind="skill", body="Follow the checklist")
    expanded = skill.expand("检查这个 PR")
    assert expanded.startswith("Follow the checklist")
    assert "检查这个 PR" in expanded

    # A skill that does name a placeholder keeps the plain substitution, and
    # the task is not appended a second time.
    templated = Command(
        name="review", description="Review", kind="skill", body="Checklist for $ARGUMENTS"
    )
    assert templated.expand("检查这个 PR") == "Checklist for 检查这个 PR"

    # A template command is a prompt, not instructions: unchanged behaviour.
    template = Command(name="t", description="T", kind="template", body="Do it")
    assert template.expand("ignored") == "Do it"


def test_broken_skill_is_skipped_by_discovery(tmp_path, caplog):
    """One unusable package must not hide the rest, and must say why."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "good").mkdir(parents=True)
    (skills_dir / "good" / "SKILL.md").write_text(
        "---\ndescription: Fine\n---\nbody\n", encoding="utf-8"
    )
    (skills_dir / "bad-yaml").mkdir()
    (skills_dir / "bad-yaml" / "SKILL.md").write_text(
        "---\ndescription: [unclosed\n---\nbody\n", encoding="utf-8"
    )
    (skills_dir / "no-frontmatter").mkdir()
    (skills_dir / "no-frontmatter" / "SKILL.md").write_text("body only\n", encoding="utf-8")
    (skills_dir / "not-a-dir.txt").write_text("x", encoding="utf-8")

    reg = SkillRegistry.discover([], user_dir=skills_dir)
    assert reg.names() == ["good"]
    # Skipping is reported, not silent: a user whose skill vanished needs the
    # reason in the log rather than an empty list.
    assert "bad-yaml" in caplog.text
