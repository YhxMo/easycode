#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8000}"
LOG_FILE="${EASYCODE_WEB_LOG:-/tmp/easycode-web.log}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  printf '用法: %s [端口]\n' "$0" >&2
  exit 2
fi

# Only stop processes that are actually Easy Code web servers on this port.
candidate_pids="$(
  {
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
    pgrep -f "easycode web --port $PORT" 2>/dev/null || true
  } | sort -u
)"

while IFS= read -r pid; do
  [[ -z "$pid" ]] && continue
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command_line" in
    *"easycode web --port $PORT"*)
      kill "$pid" 2>/dev/null || true
      ;;
  esac
done <<< "$candidate_pids"

for _ in {1..20}; do
  if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf '端口 %s 仍被其他进程占用，未启动 Easy Code。\n' "$PORT" >&2
  exit 1
fi

cd "$ROOT_DIR"

# Build before starting: the server serves frontend/dist, so a stale build is
# what a reviewer would actually be testing. A failed build stops here rather
# than starting on the previous dist.
printf '构建前端…\n'
if ! npm --prefix frontend run build; then
  printf '前端构建失败，未启动 Easy Code（不拿旧 dist 验收）。\n' >&2
  exit 1
fi

nohup uv run easycode web --port "$PORT" >"$LOG_FILE" 2>&1 < /dev/null &
server_pid=$!

printf 'Easy Code Web 已重启: http://127.0.0.1:%s\n' "$PORT"
printf 'PID: %s\n日志: %s\n' "$server_pid" "$LOG_FILE"
