---
name: skill-creator
description: 指导和脚手架生成新的 Easy code 技能（创建到 .easycode/skills/<name>/SKILL.md）
---

# Skill Creator 技能生成器

当用户需要创建或改进一个专用的 Easy code Skill 时使用此技能。

## 规范与工作流程

1. **命名规范**：使用小写连字符（如 `my-custom-skill`），Skill 目录名与 frontmatter 中的 `name` 保持一致。
2. **位置选择**：
   - 项目级：`<workspace>/.easycode/skills/<name>/SKILL.md`（推荐当前项目使用）
   - 用户全局级：`~/.easycode/skills/<name>/SKILL.md`（跨所有项目通用）
3. **YAML Frontmatter 说明**：
   - `description`（必填）：清晰说明何时触发此技能（例如触发词、适用场景），系统提示词中将直接展示该描述供模型渐进披露路由。
   - `name`（可选）：技能名称。
4. **正文编写**：
   - 详细的执行步骤、规范要求、参考代码或输出格式。
5. **验证**：
   - 在 CLI/Web 中输入 `/skills` 查看是否正确加载，或直接输入 `/<name>` 测试触发。
