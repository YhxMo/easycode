# easycode

Python CLI 编程助手（类 Claude Code / opencode），带 Web UI。

- **多供应商 LLM 抽象**：基于 [LiteLLM](https://github.com/BerriAI/litellm)，统一 OpenAI / Anthropic / DeepSeek / 本地模型等的 OpenAI 格式接口
- **手写 agentic tool-use 循环**（全异步）：流式输出 + 工具调用 → 执行 → 回填结果 → 再循环，直到给出最终回复
- **内置 7 个工具**：`execute_shell` / `read_file` / `write_file` / `edit_file` / `grep` / `glob` / `parallel_tasks`
- **子 agent 并行**：`parallel_tasks` 把独立子任务分发到子 agent（限流并发、独立上下文、结果回填）
- **项目规则**：自动加载工作区 AGENTS.md（向上查找）注入系统提示
- **上下文压缩**：历史超预算时由 LLM 摘要旧消息，保留最近 N 条原文
- **结构化编辑 + diff**：`edit_file` 精确替换（唯一匹配校验、dry_run 预览 unified diff）；CLI diff 语法高亮，`/run` 一键改代码并汇总变更
- **双端交互**：多行 REPL CLI + Web UI（FastAPI + SSE 流式 + React）
- **多会话**：Web 端会话隔离 + 落盘持久化（刷新/重启可恢复）
- **会话中断**：Web 忙碌时「停止」按钮 / CLI `Ctrl+C`，中断回合自动回滚半成品消息，历史保持可用
- **撤销 / 重做（undo / redo）**：Web 端每条 user prompt 下方有「↶ 回滚到此处」（删除该条 prompt 及其后的全部消息与文件改动，`POST /api/sessions/{id}/undo` 带 `until_user`）；输入区保留「↷ 重做」；CLI 用 `/undo` `/redo`。文件回滚基于「工具调用时刻的内容快照」，不依赖 git，且不误伤回合前已有的未提交改动

## 安装

需要 [uv](https://docs.astral.sh/uv/)（Python ≥ 3.11）：

```bash
uv sync
```

## 配置

### API Key

二选一：

- `.env` 环境变量：复制 `.env.example` 为 `.env`，填入供应商 key（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` …）
- 凭据文件 `~/.easycode/credentials.json`（自动 `chmod 600`，不入库）：

```json
{
  "my-key": { "api_key": "sk-...", "provider": "openai", "base_url": "https://..." }
}
```

配置文件里用 `{"model": "gpt-4o", "key_id": "my-key"}` 引用 key；Web UI「添加模型」弹窗自动写入凭据，`GET /api/models` 脱敏不返回 key。

### 模型别名（easycode.config.json）

```json
{
  "default_model": "deepseek-v4flash",
  "models": {
    "deepseek-v4flash": "deepseek/deepseek-v4-flash",
    "gpt5.6-terra": "openai/gpt-5.6-terra",
    "gpt5.6-sol": "openai/gpt-5.6-sol",
    "claude-sonnet5": "anthropic/claude-sonnet-5",
    "claude-opus5": "anthropic/claude-opus-5"
  },
  "tools": { "execute_shell": true, "read_file": true, "write_file": true, "grep": true, "glob": true },
  "max_tool_result_chars": 8000,
  "max_context_tokens": 32000,
  "permission": "ask",
  "workspace": { "secondary": ["~/shared-lib"], "extra_safe_dirs": ["~/notes"] },
  "mcp_servers": { "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]} }
}
```

- `workspace.secondary`：次要工作目录（与主目录同等免批准，可多个）；`workspace.extra_safe_dirs`：额外免批准目录
- 路径归属判定：主/次工作目录 + 系统临时目录 + `~/.easycode/` 免批准，其余为 `external`（读取允许，编辑需批准）
- `permission`：`ask`（默认，外部文件编辑/疑似联网命令事前询问）/ `auto-review`（全自动+事后变更汇总）/ `allow-all`（全放行）；CLI 用 `--permission`
- `mcp_servers`：外部 MCP server（stdio `{command,args}` 或 `{url}`），工具以 `mcp__<server>__<tool>` 注入 agent
- `max_context_tokens`：上下文预算（默认 32k），超限时 LLM 摘要最旧消息，无摘要能力则硬裁剪

- `models`：别名 → litellm 模型串（带提供商前缀，如 `deepseek/`、`openai/`、`anthropic/`），或对象 `{"model": "gpt-4o", "key_id": "my-key"}`（key 从凭据文件取）
- 想临时换模型，无需改文件，在 REPL 里用 `/model`（见下）
- 配置文件从当前目录向上查找最近的 `easycode.config.json`

## 使用

### CLI（REPL）

```bash
uv run easycode                # 启动（默认模型）
uv run easycode -m claude-sonnet5   # 指定别名启动
uv run easycode -r /path/to/project # 指定工作目录
uv run easycode -r main --secondary-root /shared/lib # 追加次要工作目录（可多次）
```

### Web UI

```bash
uv run easycode web            # http://127.0.0.1:8000
uv run easycode web --port 9000
```

- 生产模式自动托管 `frontend/dist`（需先构建：`cd frontend && npm install && npm run build`）
- 开发模式：另起一个终端 `cd frontend && npm run dev`（Vite 代理 `/api` → 8000），改动前端热更新
- 浏览器打开后：左侧会话列表（多会话隔离，落盘 `~/.easycode/sessions/*.json`，刷新/重启可恢复续聊），按**项目（主工作目录）分组**：新会话时选择项目目录（手动输入或 📁 访达选择），**每个主目录绑定一组次目录**（＋/📁 多选/✕ 维护，绑定持久化到 config 并由会话历史推断合并，主/次目录同样免批准），发出第一条消息后锁定；未选择的项目归入 `default project`（也能挂次目录）；底部可切换 / 添加 / 删除模型

REPL 命令：

| 命令 | 说明 |
|---|---|
| `/model` | 列出模型与当前默认 |
| `/model <别名>` | 切换默认模型 |
| `/model <别名>=<litellm串>` | 运行时新增/覆盖别名并保存 |
| `/run <任务>` | 一键改代码：agent 自动执行修改，结束后渲染本次全部 diff 汇总 |
| `/undo` | 撤销上一回合：删除该回合消息并回滚其文件改动 |
| `/redo` | 重做被 `/undo` 撤销的回合（消息 + 文件） |
| `/skills` | 列出可用 skills（`~/.easycode/skills/` 与 `<workspace>/.easycode/skills/`） |
| `/agents` | 列出可委派 agents（`~/.easycode/agents/` 与 `<workspace>/.easycode/agents/`） |
| `/help` | 帮助（动态列出内置/模板/skill 全量命令） |
| `/exit` | 退出 |

键位：`Enter` 发送，`Esc+Enter` 换行，`Ctrl+C` 中断当前回合（自动回滚半成品消息），`Ctrl+D` 退出。

## 自定义 Agent / Skill / Command（Phase 6）

Easy code 提供了与主流生态对齐的扩展能力：

### 1. 自定义 Agent（.easycode/agents/*.md）
- **路径**：`~/.easycode/agents/{name}.md`（全局）或 `<workspace>/.easycode/agents/{name}.md`（项目级，同名覆盖）。
- **定义格式**：YAML frontmatter + Markdown 正文（作为 System Prompt）。
  ```yaml
  ---
  name: plan
  description: 负责架构探索与计划制定的只读 Agent
  tools: [read_file, grep, glob]
  mode: subagent
  ---
  你是一个架构规划专家...
  ```
- **委派与调用**：通过内置 `task` 工具（`{agent, prompt}`）按需委派独立上下文的具名子 Agent，执行完毕后将结论回填给主 Agent。

### 2. Skill 系统（SKILL.md + use_skill / 命令式触发）
- **路径**：`~/.easycode/skills/{name}/SKILL.md` 或 `<workspace>/.easycode/skills/{name}/SKILL.md`。
- **渐进披露**：系统提示中仅展示 Skill 的 `name` 与 `description`，Agent 判断命中时调用 `use_skill(name)` 动态将全文注入历史；用户亦可直接在 CLI/Web 输入 `/{name}` 执行。
- **配置开关**：支持在 `easycode.config.json` 中配置 `"skills": {"enabled": false}` 全局禁用。

### 3. Slash Command 模板（.easycode/commands/*.md）
- **路径**：`~/.easycode/commands/{name}.md` 或 `<workspace>/.easycode/commands/{name}.md`。
- **参数替换**：支持 `$ARGUMENTS`（或 `$ARGS`）以及 `$1`..`$9` 位置参数展开。
- **双端支持**：CLI 与 Web 共享命令发现层，Web 端提供输入前导 `/` 的快捷自动补全（`GET /api/commands`）。

## 会话中断与回滚

- **中断**：Web 端回合进行中「发送」按钮变「停止」（`POST /api/sessions/{id}/cancel`），SSE 收到 `cancelled` 事件后流正常结束；CLI 用 `Ctrl+C`。被中断回合的未完成消息会从历史中移除，不污染后续轮次。
- **撤销 / 重做**：在 `write_file`/`edit_file` 等工具**执行前**记录目标文件内容快照（`src/easycode/snapshot.py`），`/undo` 时精确还原、并删除该回合新建的文件；`/redo` 重新应用。机制**不依赖 git**（任意工作目录可用），也不会误伤回合前已有的未提交改动。局限：`execute_shell` 直接改写的文件不在追踪范围。
- Web 端每条用户 prompt 下方有「↶ 回滚到此处」按钮（回滚到该条 prompt 之前，删除其后所有消息与文件改动）；输入区右侧保留「↷ 重做」按钮。undo/redo 栈为会话内存态，重启后重置。

## 架构

```
src/easycode/
├── cli.py                # typer 入口：main(REPL) / web(FastAPI) 子命令
├── config.py             # 配置加载 + 模型别名解析（向上查找 config）
├── frontmatter.py        # YAML frontmatter 解析
├── agents.py             # AgentSpec + AgentRegistry（自定义 Agent 发现与解析）
├── skills.py             # Skill + SkillRegistry（Skill 渐进式披露与按需加载）
├── commands.py           # CommandRegistry（内置/模板/Skill 统一命令路由与展开）
├── models/
│   ├── base.py           # Provider 异步协议 + StreamEvent/ToolCall 类型
│   └── litellm_provider.py  # litellm.acompletion 流式 + tool_calls 分片累积（SSA）
├── agent/
│   ├── loop.py           # 异步 agentic loop（响应用户 → 工具循环 → final；内置 parallel_tasks, task, use_skill）
│   ├── context.py        # 会话历史（条数/字符预算 + 摘要压缩 condense）
│   ├── summarizer.py     # LLMSummarizer：非流式摘要旧消息
│   └── system.py         # 系统提示词 + AGENTS.md + Skills/Agents 列表向上查找注入
├── tools/
│   ├── registry.py       # @tool 装饰器 + pydantic → JSON Schema + 结果截断
│   ├── shell.py          # execute_shell
│   └── files.py          # read/write/edit(精确替换+dry_run)/grep/glob（pathlib 限制工作目录）
├── snapshot.py           # 回合文件快照 + undo/redo（工具执行前记录内容字节）
├── ui/render.py          # rich 渲染（CLI 用）
└── web/                  # Web UI 后端
    ├── main.py           # FastAPI app + /api/chat(SSE) + /api/models + /api/sessions + /api/commands
    ├── session.py        # SessionStore：多会话隔离 + 落盘持久化
    └── bridge.py         # AgentEvent → SSE 序列化

frontend/                 # Vite + React + TS 前端
├── vite.config.ts        # dev 代理 /api → 8000
└── src/
    ├── App.tsx           # 会话列表 / 消息流 / 输入框
    ├── api.ts            # fetch SSE 流解析 / API 请求
    ├── CommandMenu.tsx   # / Slash Command 自动补全菜单
    ├── ToolCard.tsx      # 工具调用卡片（折叠结果）
    └── ModelPicker.tsx   # 模型下拉切换
```

消息全程使用 OpenAI 格式 dict，供应商无关；核心循环见 `agent/loop.py`。Web 端实时事件通过 SSE 推送（`session` / `text` / `tool_start` / `tool_result` / `error` / `approval_required` / `review` / `cancelled` / `done`）。

## 测试

```bash
uv run pytest        # 134 个测试，离线可跑（FakeProvider 替代真实 LLM，含 Web 接口与 Phase 6 功能测试）
```

## 路线图

- **Phase 1（已交付）**：骨架 + litellm 抽象 + 5 工具 + tool-use 循环 + REPL + 测试
- **Phase 2（已交付）**：`edit_file` 结构化精确替换（唯一匹配校验 + dry_run 预览）、diff 语法高亮渲染、`/run` 一键改代码 + 变更汇总
- **Phase 3（已交付）**：`parallel_tasks` 子 agent 并行、AGENTS.md 规则加载、LLM 摘要上下文压缩
- **Phase 4（已交付）**：核心全异步化、FastAPI + SSE 后端、React/Vite Web UI、多会话持久化、`easycode web` 子命令
- **Phase 5（已交付）**：模型 Web 直配 + 独立凭据文件（`~/.easycode/credentials.json`，脱敏）、多工作目录 + 路径归属判定、三档权限分级 + CLI/Web 批准交互（SSE + `POST /api/approval/{id}`）、MCP client（自研 stdio/HTTP 传输，工具 `mcp__<server>__<tool>`）、上下文压缩接入生产（token 预算 + LLM 摘要）
- **Phase 5.5（已交付）**：会话中断（Web 停止按钮 `POST /api/sessions/{id}/cancel` + CLI `Ctrl+C`，中断自动回滚半成品消息）+ 撤销/重做（`/undo` `/redo` + Web `↶↷`，文件内容快照回滚，不依赖 git）
- **Phase 6（已交付）**：多 Agent / Skill / Command 系统（`.easycode/agents/*.md` + `task` 工具委派、`SKILL.md` + `use_skill` 渐进式披露、Markdown 模板命令与 `$ARGUMENTS` 展开、Web 端 `/` 自动补全）