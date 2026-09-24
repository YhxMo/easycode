"""Approval policy: which tool calls need a human nod before running.

Three permission modes (config global / CLI flag / Web session):

- ``ask`` (default): workspace/temp actions run automatically; editing
  external files, the data-home state dirs (~/.easycode/{sessions,agents,
  skills,commands}), and network-ish shell commands ask the user first.
- ``auto-review``: everything runs; changes are summarized afterwards.
- ``allow-all``: everything runs; no tracking at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path

from easycode.models.base import ToolCall
from easycode.policy import PERM_ALLOW_ALL, SANDBOX_DANGER_FULL_ACCESS
from easycode.workspace import PathContext, ToolGrant, validate_writable_roots

NETWORK_HINTS = (
    "curl",
    "wget",
    "git clone",
    "git pull",
    "git push",
    "pip install",
    "npm install",
    "npx ",
    "cargo install",
    "docker pull",
    "rsync",
    "ssh ",
    "scp ",
    "http://",
    "https://",
    "ftp://",
)

NETWORK_HINT_RE = re.compile("|".join(re.escape(h) for h in NETWORK_HINTS), re.IGNORECASE)

FILE_EDIT_TOOLS = {"write_file", "edit_file"}

#: 读取类工具：目标落在项目目录之外（classify() == "external"，如 ~/.ssh）时
#: 在 ask / auto-review 模式下必须先经审批（SC-105），防止静默读取外部敏感文件。
READ_TOOLS = {"read_file"}

TRUNC_LIMIT = 80


def approval_key(tc: ToolCall, *, grant: ToolGrant | None = None) -> str:
    """Per-session 'always allow' key: tool + first/relevant argument.

    Shared by CLI and Web so 'always allow' semantics are identical. File
    tools match by their parent-directory scope so the Web UI can display a
    ``/dir/*`` pattern that mirrors opencode's permission dialog.

    For ``execute_shell`` the key carries a readable command prefix (for the
    UI) followed by a stable SHA-256 of the *full* command, so authorization
    equivalence depends on the complete command: two commands that share the
    first :data:`TRUNC_LIMIT` characters but differ later never collide, and
    an identical command always yields the same key. When ``grant`` is given
    the digest also binds the conveyed capability (network / explicit writable
    roots, sorted+normalised) so a permission narrowed or widened along those
    axes is never confused with a plain command-only grant.
    """
    if tc.name in FILE_EDIT_TOOLS:
        return f"{tc.name}:{approval_scope(tc)}"
    if tc.name == "execute_shell":
        command = str(tc.arguments.get("command", ""))
        material = command
        if grant is not None:
            caps = [
                "net" if grant.network_allowed else "no-net",
                ",".join(sorted(str(r.resolve()) for r in grant.writable_roots)),
            ]
            material = command + "\x00" + "|".join(caps)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{tc.name}:{command[:TRUNC_LIMIT]}:{digest}"
    return tc.name


def approval_scope(tc: ToolCall) -> str:
    """Human-facing boundary that an approval grants when 'always allowed'.

    - File tools: the parent directory with a ``/*`` glob (this is the scope
      the user is granting, matching the ``/dir/*`` pattern in the UI).
    - Shell: the command text (truncated).
    - Everything else: the tool name.
    """
    if tc.name in FILE_EDIT_TOOLS:
        path = str(tc.arguments.get("path", "")).rstrip("/")
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        return f"{parent}/*" if parent else "/*"
    if tc.name == "execute_shell":
        return str(tc.arguments.get("command", ""))[:TRUNC_LIMIT]
    return tc.name


def needs_approval(tc: ToolCall, ctx: PathContext, mode: str) -> bool:
    """Decision layer: True when the tool call must be confirmed first.

    Auto-review and allow-all never block; ask blocks external-file edits and
    network-looking shell commands.
    """
    if mode == PERM_ALLOW_ALL:
        return False
    if tc.name in READ_TOOLS:
        path = tc.arguments.get("path", "")
        if not path:
            return False
        return ctx.classify(ctx.resolve(str(path))) == "external"
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        if not path:
            return False
        return not ctx.in_allowed(ctx.resolve(str(path)))
    if tc.name == "execute_shell":
        if tc.arguments.get("sandbox_permissions") == "require_escalated":
            return True
        if _looks_like_network(str(tc.arguments.get("command", ""))):
            return True
        declared = tc.arguments.get("writable_roots") or []
        if declared:
            # Only prompt when at least one declared root is *actually external*
            # (or invalid). Roots entirely inside the workspace (secondary /
            # extra_safe) must never prompt. An invalid declaration still prompts
            # so the tool can fail closed with a structured error.
            roots, err = validate_writable_roots(list(declared), None)
            if err is not None:
                return True
            if any(not ctx.in_allowed(r) for r in roots):
                return True
        return False
    return False


def approval_reason(tc: ToolCall, ctx: PathContext) -> str:
    """Categorical, human-readable explanation for the approval prompt.

    Kept free of the concrete command/path (the UI shows that separately as
    the ``scope`` line), so the prompt reads like opencode's permission
    dialog category line.
    """
    if tc.name in READ_TOOLS:
        return "读取项目目录之外的文件"
    if tc.name in FILE_EDIT_TOOLS:
        path = tc.arguments.get("path", "")
        resolved = ctx.resolve(str(path))
        for protected in ctx.protected_paths():
            if resolved.is_relative_to(protected):
                return "修改受保护目录 (.git/.easycode/项目配置)"
        if ctx.is_model_state(resolved):
            return "修改会话或扩展数据目录 (~/.easycode)"
        return "访问项目目录之外的文件"
    if tc.name == "execute_shell":
        if tc.arguments.get("sandbox_permissions") == "require_escalated":
            return "执行需要额外权限的命令"
        if _looks_like_network(str(tc.arguments.get("command", ""))):
            return "执行疑似联网命令"
        return "执行 Shell 命令"
    return f"工具需要批准: {tc.name}"


def _looks_like_network(command: str) -> bool:
    return bool(NETWORK_HINT_RE.search(command))


def grant_for_toolcall(tc: ToolCall, ctx: PathContext) -> ToolGrant:
    """Minimal, precise grant derived from an *approved* tool call.

    This is the single-call authorization an approval actually conveys. It is
    deliberately narrow:

    - ``execute_shell``: network is granted only when the command looks like a
      network operation (or was marked escalated); no external writable roots
      are inferred from the shell string — the sandbox keeps Seatbelt's write
      boundary unless the approval supplied explicit ``writable_roots``.
    - file tools: the exact parent directory of the target becomes the granted
      writable root (a precise target grant, not the whole ``/dir/*`` scope);
      when the target is already inside an allowed directory no grant is needed.
    - anything else (MCP, builtins): an empty grant.
    """
    if tc.name in FILE_EDIT_TOOLS:
        path = str(tc.arguments.get("path", "")).strip()
        if not path:
            return ToolGrant()
        p = ctx.resolve(path)
        if ctx.in_allowed(p):
            return ToolGrant()
        # A protected target (credentials, or any workspace/extra root's
        # .git/.easycode, incl. the target's parent being one) must never be
        # granted — the approval can only emit a precise safe grant, else the
        # file tool's own hard-deny rejects the write.
        if ctx.is_protected_path(p):
            return ToolGrant()
        return ToolGrant(writable_roots=(p.parent,))
    if tc.name == "execute_shell":
        command = str(tc.arguments.get("command", ""))
        network = (
            tc.arguments.get("sandbox_permissions") == "require_escalated"
        ) or _looks_like_network(command)
        declared = tc.arguments.get("writable_roots") or []
        # The grant's writable roots come *only* from the model's explicit,
        # validated declaration (never by parsing the command string). If ANY
        # declared root is invalid, the whole list fails closed — a partial
        # grant is never kept.
        roots, err = validate_writable_roots(list(declared), None)
        if err is not None:
            roots = []
        return ToolGrant(network_allowed=network, writable_roots=tuple(roots))
    return ToolGrant()


# ---------------------------------------------------------------- destructive denylist

# A clearly destructive shell command is denied outright — under the sandboxed
# presets (ask / auto-review) regardless of the permission mode. This is a
# policy gate (a conservative deny-list) layered on top of the Seatbelt
# sandbox — the sandbox remains the actual file/network boundary. Ordinary
# workspace commands (git status, pytest, ls, curl -I) are unaffected and
# return ``None``. Under ``danger-full-access`` (allow-all) the denylist is
# off entirely — see :func:`definitive_deny_reason`.
DESTRUCTIVE_GIT_RESET_RE = re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE)
DESTRUCTIVE_GIT_CLEAN_RE = re.compile(r"\bgit\s+clean\b", re.IGNORECASE)
DESTRUCTIVE_GIT_PUSH_FORCE_RE = re.compile(
    r"\bgit\s+push\b[^;\n|&]*--force(?:-with-lease)?\b", re.IGNORECASE
)


def _expand_path(raw: str) -> Path:
    """Expand ``~``/``$VAR`` the way a shell would (best-effort)."""
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def _rm_recursive_root_reason(command: str) -> str | None:
    """Categorical reason when a recursive ``rm`` targets the filesystem root
    or the user's home directory (``~`` / ``$HOME``)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    rm_idx = None
    for i, tok in enumerate(tokens):
        if tok == "rm":
            rm_idx = i
            break
    if rm_idx is None:
        return None
    recursive = False
    targets: list[str] = []
    after_dashdash = False
    for tok in tokens[rm_idx + 1 :]:
        if tok == "--":
            after_dashdash = True
            continue
        if not after_dashdash and tok.startswith("-") and len(tok) > 1:
            flags = tok.lstrip("-")
            if "r" in flags or "R" in flags:
                recursive = True
            continue
        targets.append(tok)
    if not recursive:
        return None
    root = Path("/").resolve()
    home = Path.home().resolve()
    for t in targets:
        try:
            expanded = _expand_path(t).resolve()
        except (OSError, ValueError):
            continue
        if expanded in (root, home):
            return "rm 递归删除系统根目录/用户主目录，已阻止"
    return None


def destructive_command_reason(command: str) -> str | None:
    """Return a categorical reason when a shell command is clearly destructive.

    Conservative deny-list; real file/network enforcement stays in the
    Seatbelt sandbox. Returns ``None`` for ordinary workspace commands so they
    are never blocked by this check.
    """
    norm = " ".join(str(command or "").split())
    if not norm:
        return None
    if DESTRUCTIVE_GIT_RESET_RE.search(norm):
        return "git reset --hard 会丢弃未提交的改动"
    if DESTRUCTIVE_GIT_CLEAN_RE.search(norm):
        return "git clean 会删除未跟踪文件"
    if DESTRUCTIVE_GIT_PUSH_FORCE_RE.search(norm):
        return "git push --force 会强制改写远端提交历史"
    rm_reason = _rm_recursive_root_reason(norm)
    if rm_reason:
        return rm_reason
    return None


def definitive_deny_reason(tc: ToolCall, ctx: PathContext) -> str | None:
    """Categorical denial for a tool call.

    Only ``execute_shell`` is in scope; anything else (file tools, MCP,
    builtins) is handled by the existing approval policy. Returning a reason
    means the call must be rejected without running — even when an approval
    handler is missing or a reviewer would have approved it.

    Under ``danger-full-access`` (the ``allow-all`` preset) the denylist is
    off, matching Codex's ``danger-full-access`` semantics: the sandbox and
    approvals are already gone, so no in-process denylist remains. Selective
    limits under allow-all are the job of ``permission_rules``.
    """
    if ctx.sandbox_mode == SANDBOX_DANGER_FULL_ACCESS:
        return None
    if tc.name != "execute_shell":
        return None
    return destructive_command_reason(str(tc.arguments.get("command", "")))
