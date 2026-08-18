#!/usr/bin/env bash
# mc2api — 一键启停
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export MC_CONSOLE_HOST="${MC_CONSOLE_HOST:-127.0.0.1}"
export MC_CONSOLE_PORT="${MC_CONSOLE_PORT:-18095}"
export MC_CONSOLE_DATA="${MC_CONSOLE_DATA:-$ROOT/data}"

DATA_DIR="$MC_CONSOLE_DATA"
PID_FILE="$DATA_DIR/server.pid"
LOG_FILE="$DATA_DIR/server.log"
HOST="$MC_CONSOLE_HOST"
PORT="$MC_CONSOLE_PORT"
BASE_URL="http://${HOST}:${PORT}"
ADMIN_URL="${BASE_URL}/admin"
GATEWAY_URL="${BASE_URL}/v1"

mkdir -p "$DATA_DIR"
cd "$ROOT"

die() { echo "错误: $*" >&2; exit 1; }
info() { echo "$*"; }

need_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    die "未找到 python3，请先安装 Python 3.9+"
  fi
}

is_running() {
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  fi
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    # confirm it is our server
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "server.py"; then
      echo "$pid"
      return 0
    fi
  fi
  # fallback: listener on port
  local p
  p="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [[ -n "${p}" ]]; then
    echo "$p"
    return 0
  fi
  return 1
}

wait_ready() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS --max-time 1 "${BASE_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

cmd_status() {
  local pid
  if pid="$(is_running)"; then
    info "状态: 运行中 (pid=${pid})"
    info "管理台: ${ADMIN_URL}"
    info "网关:   ${GATEWAY_URL}"
    if curl -fsS --max-time 2 "${BASE_URL}/healthz" >/dev/null 2>&1; then
      info "健康检查: OK"
    else
      info "健康检查: 端口已监听，但 /healthz 暂未就绪"
    fi
    return 0
  fi
  info "状态: 未运行"
  return 1
}

cmd_stop() {
  local pid
  if ! pid="$(is_running)"; then
    info "未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  info "正在停止 pid=${pid} ..."
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.15
  done
  if kill -0 "$pid" 2>/dev/null; then
    info "强制结束 pid=${pid}"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  info "已停止"
}

cmd_start() {
  local open_browser="${1:-0}"
  need_python
  local pid
  if pid="$(is_running)"; then
    info "已在运行 (pid=${pid})"
    info "管理台: ${ADMIN_URL}"
    info "网关:   ${GATEWAY_URL}"
    if [[ "$open_browser" == "1" ]]; then
      open_admin
    fi
    return 0
  fi

  info "启动 mc2api ..."
  info "  listen  ${BASE_URL}"
  info "  data    ${DATA_DIR}"
  info "  log     ${LOG_FILE}"

  nohup python3 -u "$ROOT/server.py" >>"$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" >"$PID_FILE"

  if wait_ready; then
    info "启动成功 (pid=${pid})"
    info "管理台: ${ADMIN_URL}"
    info "网关:   ${GATEWAY_URL}"
    if [[ -f "$DATA_DIR/default_client_key.txt" ]]; then
      info "默认 Key: $(tr -d '[:space:]' <"$DATA_DIR/default_client_key.txt")"
    fi
    if [[ "$open_browser" == "1" ]]; then
      open_admin
    fi
  else
    info "进程已拉起 (pid=${pid})，但健康检查超时，请查看日志:"
    info "  tail -f \"$LOG_FILE\""
    return 1
  fi
}

cmd_restart() {
  local open_browser="${1:-0}"
  cmd_stop || true
  sleep 0.3
  cmd_start "$open_browser"
}

cmd_fg() {
  need_python
  if is_running >/dev/null; then
    die "已在后台运行，请先执行: $0 stop"
  fi
  info "前台运行 ${BASE_URL} （Ctrl+C 退出）"
  exec python3 -u "$ROOT/server.py"
}

cmd_logs() {
  touch "$LOG_FILE"
  tail -n "${1:-80}" -f "$LOG_FILE"
}

open_admin() {
  if command -v open >/dev/null 2>&1; then
    open "$ADMIN_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$ADMIN_URL" >/dev/null 2>&1 || true
  fi
}

usage() {
  cat <<EOF
mc2api

用法:
  $0                 后台启动（默认）
  $0 start [--open]  后台启动；加 --open 自动打开管理台
  $0 stop            停止
  $0 restart [--open] 重启
  $0 status          查看状态
  $0 logs [N]        跟踪日志（默认最近 80 行）
  $0 fg              前台运行（调试用）
  $0 open            打开管理台浏览器

环境变量:
  MC_CONSOLE_HOST   默认 127.0.0.1
  MC_CONSOLE_PORT   默认 18095
  MC_CONSOLE_DATA   默认 ./data
EOF
}

main() {
  local cmd="${1:-start}"
  shift || true
  local open_flag=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --open|-o) open_flag=1 ;;
    esac
  done

  case "$cmd" in
    start)   cmd_start "$open_flag" ;;
    stop)    cmd_stop ;;
    restart) cmd_restart "$open_flag" ;;
    status)  cmd_status ;;
    logs)    cmd_logs "${1:-80}" ;;
    fg|run)  cmd_fg ;;
    open)    open_admin; info "$ADMIN_URL" ;;
    -h|--help|help) usage ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
