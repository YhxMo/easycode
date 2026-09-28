<h1 align="center">EasyCode</h1>

<p align="center">A local coding agent that runs the model-and-tool loop in Python, with a terminal UI and a React web app, and a permission model that asks before it works outside your project.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Node.js-22-3776AB?style=flat-square" alt="Node.js 22">
  <img src="https://img.shields.io/badge/React-19-3776AB?style=flat-square" alt="React 19">
</p>

EasyCode connects a model to a small set of file and shell tools and lets it work through a task step
by step: the model asks for a tool, the tool runs, the result goes back to the model, and the turn ends
when the model stops asking for tools. The same loop backs both front ends.

## Highlights

| Highlight | What it means in practice |
| --- | --- |
| Everything stays local | The agent, its configuration, its credentials and its conversation history live in your project and in `~/.easycode`. Nothing leaves the machine except the model calls you configure. |
| Reads, searches, edits and runs | File tools plus a shell inside the workspace; every tool call is traced in the UI and file changes are shown as diffs. |
| Permissions with a real boundary | Three modes, an approval prompt (or an independent reviewer) for anything outside the workspace, and a macOS Seatbelt sandbox for shell commands. |
| Sessions that keep running | Several sessions run their turns at once, and each keeps its own tab, draft, panel state and scroll position. |
| Extensible by adding files | `AGENTS.md` rules, skills, prompt templates and MCP servers are files you drop into the project or your user directory. |

## Architecture

Both front ends drive the same agent loop: the CLI calls it directly, while the web app goes through
FastAPI, which also streams text, tool results and approval requests to the browser over SSE. Model
access goes through LiteLLM, so any compatible endpoint can be configured.

```text
            ┌────────────────────┐            ┌────────────────────┐
            │    CLI (Typer)     │            │    React web UI    │
            │   prompt-toolkit   │            │ Vite + TypeScript  │
            └──────────┬─────────┘            └──────────┬─────────┘
                       │                                 │ HTTP + SSE
                       └────────────────┬────────────────┘
                                        ▼
                      ┌────────────────────────────────────┐
                      │             Agent loop             │
                      │    context budget + compaction     │
                      └───────┬────────────────────┬───────┘
                   model call │                    │  tool call
                              ▼                    ▼
                      ┌─────────────────┐  ┌─────────────────┐
                      │  Model adapter  │  │   Permission    │
                      │    (LiteLLM)    │  │  macOS sandbox  │
                      └─────────────────┘  └───────┬─────────┘
                                                   ▼
                                      ┌───────────────────────────┐
                                      │  Files · Shell · skills   │
                                      │ sub-agents · MCP servers  │
                                      └───────────────────────────┘
```

## Screenshots

A real session in a throwaway project. The task: add `top_words` to `wordcount.py`, cover it with a
test and run `pytest`. The agent reads the files, edits the module, writes the test, runs the suite,
and after its own test expectation fails once, fixes the test and runs it again.

![EasyCode web UI: a conversation with an expanded tool trace on the left and the change panel with a diff on the right](./assets/web-overview.png)

The right panel tracks the session's task list, context, changed files and a preview of any file the
session touched. `/` lists the commands and skills registered in the workspace, `@` references files.

![The command menu open above the composer, listing a skill and a prompt template](./assets/web-commands.png)

Full access removes the sandbox and the approvals, so it asks for an explicit confirmation first.

![A confirmation dialog warning that full access closes the file sandbox and the approvals](./assets/web-full-access-confirm.png)

## Quick Install

```bash
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
```

Requires Python 3.11+, uv, and Node.js with npm.

## Quick Start

```bash
uv run easycode web
```

Open <http://127.0.0.1:8000>, add a model (model id, API key and base URL), choose a project
directory, and start a session. A first task that exercises most of the agent:

> Read this project's entry point, follow how configuration is loaded, add a validation step to it,
> and run the related tests.

The server binds to localhost only; `uv run easycode web --port 9000` changes the port. The CLI reads
the same configuration:

```bash
uv run easycode main --root /path/to/project
uv run easycode main --help
```

## What it does

- Tools: read, search (glob and regex), exact replace, write, and shell commands inside the workspace.
- Context: a budget that counts the system prompt and the tool schemas, trims old tool output, and
  keeps a rolling summary of the task.
- Task list: for multi-step work the model maintains a todo list, shown in the panel and in the
  message stream.
- Delegation: named sub-agents and parallel sub-tasks work in their own context and can never exceed
  the parent's tool permissions.
- Projects and sessions: several projects, extra working directories, pinning, model switching, and
  stopping a running turn.
- Approvals: workspace writes run directly; anything outside asks for approval, or is decided by a
  second model in auto-review mode.
- Shell sandbox: on macOS shell commands run inside a Seatbelt profile that allows the workspace and
  nothing else.
- MCP servers: connect external stdio or Streamable HTTP servers and let the model call their tools.
  Servers come from three scopes — your personal file, the app's startup config, and the current
  project — and the extensions dialog (sidebar → 扩展 → MCP 服务) edits all three, stores tokens
  separately from the configuration, and shows why a server is not connecting. `/mcp:<service> <task>`
  asks for one service by name: the model is told to prefer its tools, a service that is off or
  unreachable fails the turn with `mcp_unavailable` instead of quietly answering without it, and the
  menu only ever offers the services of the project the conversation runs in.
- Extensions in one place: the dialog's Skills tab lists what is installed for the personal scope and
  the current project, and imports a local folder by copying the whole package into
  `.easycode/skills/` (or `~/.easycode/skills/`). Nothing is executed during the import, the source is
  not referenced again, and the same name may exist in both scopes — each row says where it came from
  and whether a higher scope shadows it.
- Pinning: a pinned conversation is listed once, in the pinned section, and hovering it says which
  project it belongs to — the row no longer has to carry both facts in one tooltip.

## Configuration

`easycode.config.json` in the project holds the model list, enabled tools, permission mode, workspace
projects, MCP servers and `max_tool_iterations` (an optional cap on model calls per turn; unset means
the model ends the turn). Credentials live in `~/.easycode/credentials.json` and conversations in
`~/.easycode/sessions/`. None of it is committed.

MCP servers are configured in three places, lowest priority first: `~/.easycode/mcp.json`, the
application's own `easycode.config.json`, and the current project's `easycode.config.json` — which is
usually the same file as the one before it. A name defined in a higher scope replaces the whole entry
from the lower one, so setting `enabled: false` in a project is how you switch off a personal server
there without touching anyone else's. Secrets never go into these files: the configuration only names
where a secret comes from, and the value is kept in `~/.easycode/mcp-credentials.json` (mode `0600`),
writable only through the settings panel.

## Extending

| Kind | Path | Example |
| --- | --- | --- |
| Agent | `agents/<name>.md` | [code-planning agent](examples/agents/plan.md) |
| Skill | `skills/<name>/SKILL.md` | [skill authoring guide](examples/skills/skill-creator/SKILL.md) |
| Command | `commands/<name>.md` | [prompt template](examples/commands/btw.md) |

Each of these can live in the project's `.easycode/` directory or in `~/.easycode/`. MCP servers are
configured in the three scopes described above and speak stdio or HTTP; the extensions dialog edits
those same files, so hand-written entries keep working.

An imported skill is called with `/name`, and `/name your task` appends the task to the skill's
instructions before sending them. Relative paths inside a skill resolve against its installed
directory, which is stated to the model along with the body.

The shell sandbox treats the data directory `~/.easycode/` as a writable root (writes to state
directories such as `sessions/` need approval), so a script inside a personal skill runs under the
workspace mode; paths outside the workspace are still refused.

## Project layout

| Path | What is there |
| --- | --- |
| `src/easycode/agent/` | The execution loop, context budget and compaction, prompts, built-in tools |
| `src/easycode/tools/` | File and shell tools |
| `src/easycode/policy.py`, `approval.py`, `sandbox/` | Permission modes, approvals, macOS sandbox |
| `src/easycode/web/`, `frontend/` | FastAPI application and the React UI |
| `tests/`, `frontend/src/__tests__/` | Backend and frontend tests |
| `examples/` | Example agent, skill and command, plus a zero-dependency demo project |

## Development

```bash
uv run --frozen ruff check src tests
uv run --frozen pytest
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run test:run
```

Tests use a simulated model and temporary directories, so they need no credentials. CI runs both jobs
on every push and pull request; the shell sandbox itself is macOS-only, so its integration cases are
skipped on Linux.

## Design notes

- Forward-only execution: finished tool calls and their results stay in the conversation. There is no
  undo, redo or automatic file rollback, and tool calls stay paired with their results so a turn can
  always continue.
- Stopping is not a rollback: the text produced so far is kept, unfinished tool calls get an
  interruption result, and already-started shell commands may still finish.
- Deletion cancels first: deleting a session or a project cancels the running turn and waits for it to
  unwind before removing files, and a failed delete is reported instead of silently succeeding.
- Every boundary is enforced on the execution path: file tools, the shell sandbox, sub-agents, MCP
  processes and the read-only preview APIs each re-check the session's own path context.
