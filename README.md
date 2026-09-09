# EasyCode

EasyCode 是一个基于 Python 的编程 Agent，提供命令行入口和 Web 界面。它将大模型对话与文件操作、命令执行、上下文管理和任务委派结合，用于理解代码、辅助修改和处理多步骤编程任务。

后端使用 FastAPI，通过 SSE 向 React 界面推送模型输出、工具执行状态和审批请求；模型接入基于 LiteLLM。

## 核心能力

- **工具调用**：读取、检索和编辑文件，执行 Shell 命令，展示修改差异与执行结果。
- **任务委派**：支持并行子任务和自定义 Agent，子任务使用独立上下文，结果返回主会话。
- **上下文管理**：结合历史摘要、近期消息保留和工具输出裁剪，控制长会话的上下文规模。
- **会话管理**：按项目组织会话，支持历史持久化、流式响应、任务中断和撤销 / 重做。
- **权限控制**：提供操作审批、路径规则和 macOS 沙箱支持，可配置主工作目录与附加目录。
- **扩展机制**：支持项目规则、自定义 Skill、斜杠命令，以及通过 MCP 接入外部工具。
- **模型配置**：支持模型切换，为不同模型独立配置 API Key、接口地址和接口格式。

## 快速开始

需要 Python 3.11 及以上版本和 uv；构建 Web 界面还需要 Node.js 与 npm。仓库 CI 使用 Python 3.14 和 Node.js 22。

在项目根目录安装依赖并构建界面：

```bash
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
```

启动 Web 服务：

```bash
uv run easycode web
```

打开 <http://127.0.0.1:8000>，添加模型并填写 API Key、接口地址及模型名称，再选择项目目录开始会话。也可通过 `--port` 指定端口：

```bash
uv run easycode web --port 9000
```

命令行入口及参数可通过以下命令查看：

```bash
uv run easycode --help
uv run easycode main --help
```

## 使用说明

### 模型与配置

通过 Web 界面管理模型。项目设置保存在 `easycode.config.json`，模型凭据单独存放于用户目录下的 `~/.easycode/credentials.json`。这两类本地文件均不纳入版本控制。

新建会话时选择主工作目录，可按需添加其他工作目录。项目根目录中的 `AGENTS.md` 可用于提供代码规范和项目说明。

### 常用命令

| 命令 | 用途 |
| --- | --- |
| `/model` | 查看模型列表 |
| `/model <别名>` | 切换模型 |
| `/run <任务>` | 执行编程任务并汇总修改差异 |
| `/undo` | 撤销上一回合 |
| `/redo` | 重做已撤销的回合 |
| `/skills` | 查看可用技能 |
| `/agents` | 查看可委派的 Agent |
| `/help` | 查看命令帮助 |

Web 界面还提供停止、回滚到指定消息和重做操作。

### 扩展方式

扩展文件可以放在项目的 `.easycode/` 目录，或用户目录下的 `~/.easycode/` 中。

| 类型 | 路径 | 作用 |
| --- | --- | --- |
| Agent | `agents/<name>.md` | 定义角色、可用工具和任务说明 |
| Skill | `skills/<name>/SKILL.md` | 按需加载特定任务的操作说明 |
| 命令 | `commands/<name>.md` | 定义可复用的提示词模板 |

可参考 [Agent 示例](examples/agents/plan.md)、[Skill 示例](examples/skills/skill-creator/SKILL.md)和[命令示例](examples/commands/btw.md)。外部工具通过配置文件中的 `mcp_servers` 接入，支持 stdio 和 HTTP 传输。

### 使用范围

Web 服务默认监听本机地址，适合本地项目使用。文件和命令操作受所选权限模式约束；macOS 沙箱能力依赖系统支持。

撤销 / 重做覆盖文件编辑工具记录的修改，Shell 命令产生的文件变更不在其追踪范围内。撤销栈保存在内存中，服务重启后不会恢复。

## 核心设计

一次任务从用户消息开始：模型生成回答或工具调用，Agent 执行工具并将结果加入上下文，再继续请求模型，直到任务结束。CLI 和 Web 界面共用 Agent 核心，Web 层负责会话管理和事件推送。

| 模块 | 职责 |
| --- | --- |
| `src/easycode/agent/` | 异步工具调用循环、上下文管理、摘要与任务委派 |
| `src/easycode/models/` | 模型接口抽象与流式响应处理 |
| `src/easycode/tools/` | 工具注册、文件操作与命令执行 |
| `src/easycode/web/` | HTTP 接口、SSE 事件和会话存储 |
| `src/easycode/approval.py`、`sandbox/` | 操作审批与执行边界 |
| `src/easycode/snapshot.py` | 文件快照与撤销 / 重做 |
| `frontend/` | React + TypeScript 交互界面 |
| `tests/` | 后端单元测试与接口测试 |

## 验证

测试使用模拟模型响应和隔离数据，无需真实模型凭据。

```bash
# 后端检查与测试
uv run --frozen ruff check src tests
uv run --frozen pytest

# 前端检查、构建与测试
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run test:run
```
