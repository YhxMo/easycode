---
name: skill-creator
description: Guide and scaffold new skills into .easycode/skills/<name>/SKILL.md
---

# Skill Creator

This skill helps developers design, scaffold, and verify custom skills for Easy code.

## Workflow

1. Determine the skill's purpose and choose a descriptive kebab-case name.
2. Draft a succinct `description` in frontmatter explaining *when* the agent should invoke this skill.
3. Write clear, structured instructions in the Markdown body.
4. Save the file under `.easycode/skills/<name>/SKILL.md` (project-level) or `~/.easycode/skills/<name>/SKILL.md` (user-level).
5. Verify discovery via `/skills` or `/help`.
