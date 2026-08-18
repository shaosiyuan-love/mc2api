#!/usr/bin/env bash
# mc2api — 一键启停（macOS / Linux / Windows Git Bash）
set -eu

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
PYTHON_BIN=""

mkdir -p "$DATA_DIR"
cd "$ROOT"

die() { echo "错误: $*" >&2; exit 1; }
info() { echo "$*"; }

# Git Bash / MSYS 下 sh 调用时 [[ 与部分 bash 特性仍可用；避免 pipefail 在旧 sh 上炸
is_windows() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT) return 0 ;;
    *) return 1 ;;
  esac
}

need_python() {
  PYTHON_BIN=""
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    # Windows 常只有 python；排除 Store 占位符
    if python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" 2>/dev/null; then
      PYTHON_BIN="$(command -v python)"
    fi
  fi
  if [[ -z "${PYTHON_BIN}" ]]; then
    die "未找到 Python 3.9+。Windows 请安装并勾选 Add python.exe to PATH，然后重开终端。"
  fi
  info "Python: ${PYTHON_BIN} ($("${PYTHON_BIN}" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo '?'))"
}

health_ok() {
  local url="${BASE_URL}/healthz"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
  fi
  # 无 curl 时用 Python 探测
  if [[ -n "${PYTHON_BIN}" ]] || command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    local py="${PYTHON_BIN:-}"
    [[ -z "$py" ]] && command -v python3 >/dev/null 2>&1 && py="$(command -v python3)"
    [[ -z "$py" ]] && py="$(command -v python)"
    "$py" - "$url" <<'PY' 2>/dev/null
import sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as r:
        sys.exit(0 if getattr(r, "status", 200) < 500 else 1)
except Exception:
    sys.exit(1)
PY
    return $?
  fi
  return 1
}

port_in_use() {
  # 尽量不依赖 lsof（Windows 常无）
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 | grep -q .
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -an 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" | grep -qi LISTEN
    return $?
  fi
  # 最后用健康检查反推
  health_ok
}

is_running() {
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  fi
  if [[ -n "${pid}" ]]; then
    if kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
    # Windows 上 kill -0 可能对非本 shell 子进程不准，再看端口/健康
  fi
  if health_ok; then
    echo "${pid:-up}"
    return 0
  fi
  if port_in_use; then
    echo "${pid:-port}"
    return 0
  fi
  return 1
}

wait_ready() {
  local i
  for i in $(seq 1 60); do
    if health_ok; then
      return 0
    fi
    # 进程若已死，尽早失败
    if [[ -f "$PID_FILE" ]]; then
      local pid
      pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
      if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        # 给 Windows 一点时间；若日志已有 traceback 则失败
        if [[ -f "$LOG_FILE" ]] && grep -qE 'Error|Traceback|Address already in use' "$LOG_FILE" 2>/dev/null; then
          return 1
        fi
      fi
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
    if health_ok; then
      info "健康检查: OK"
    else
      info "健康检查: 未就绪（可打开 ${ADMIN_URL} 或查看日志）"
    fi
    return 0
  fi
  info "状态: 未运行"
  return 1
}

cmd_stop() {
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  fi
  if [[ -n "${pid}" ]]; then
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
  elif ! health_ok && ! port_in_use; then
    info "未在运行"
    rm -f "$PID_FILE"
    return 0
  else
    info "未找到 pid 文件，但端口/服务仍在；请手动结束占用 ${PORT} 的进程"
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

  # 清空上次启动尾部，便于排查
  : >>"$LOG_FILE"

  if is_windows; then
    # Git Bash: 用 start 脱离当前终端，避免关掉窗口杀进程
    if command -v py >/dev/null 2>&1 && [[ "$PYTHON_BIN" == *python* ]]; then
      :
    fi
    nohup "$PYTHON_BIN" -u "$ROOT/server.py" >>"$LOG_FILE" 2>&1 &
  else
    nohup "$PYTHON_BIN" -u "$ROOT/server.py" >>"$LOG_FILE" 2>&1 &
  fi
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
    else
      info "浏览器打开管理台: 执行  $0 open   或访问 ${ADMIN_URL}"
    fi
  else
    info "启动失败或健康检查超时。最近日志："
    if [[ -f "$LOG_FILE" ]]; then
      tail -n 40 "$LOG_FILE" 2>/dev/null || true
    fi
    info "完整日志: ${LOG_FILE}"
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
  if is_running >/dev/null 2>&1; then
    die "已在后台运行，请先执行: $0 stop"
  fi
  info "前台运行 ${BASE_URL} （Ctrl+C 退出）"
  exec "$PYTHON_BIN" -u "$ROOT/server.py"
}

cmd_logs() {
  touch "$LOG_FILE"
  tail -n "${1:-80}" -f "$LOG_FILE"
}

open_admin() {
  info "打开 ${ADMIN_URL}"
  if is_windows; then
    # Git Bash / MSYS
    if command -v cmd.exe >/dev/null 2>&1; then
      cmd.exe /c start "" "${ADMIN_URL}" >/dev/null 2>&1 || true
    elif command -v powershell.exe >/dev/null 2>&1; then
      powershell.exe -NoProfile -Command "Start-Process '${ADMIN_URL}'" >/dev/null 2>&1 || true
    else
      start "${ADMIN_URL}" >/dev/null 2>&1 || true
    fi
    return 0
  fi
  if command -v open >/dev/null 2>&1; then
    open "$ADMIN_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$ADMIN_URL" >/dev/null 2>&1 || true
  else
    info "请手动在浏览器打开: ${ADMIN_URL}"
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

Windows (Git Bash):
  bash ./start.sh start --open
  # 或双击 start.bat

环境变量:
  MC_CONSOLE_HOST   默认 127.0.0.1
  MC_CONSOLE_PORT   默认 18095
  MC_CONSOLE_DATA   默认 ./data
EOF
}

main() {
  # 兼容: sh ./start.sh 无参数时，部分环境 shift 会报 shift count out of range
  local cmd="start"
  if [[ "${1-}" != "" ]]; then
    cmd="$1"
    shift
  fi
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
    open)    open_admin ;;
    -h|--help|help) usage ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
