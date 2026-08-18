#!/usr/bin/env bash
# macOS 双击启动：后台拉起中控台并打开管理台
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
chmod +x "$ROOT/start.sh" 2>/dev/null || true

echo "=========================================="
echo "  mc2api — 一键启动"
echo "=========================================="
echo

"$ROOT/start.sh" start --open
code=$?

echo
if [[ $code -eq 0 ]]; then
  echo "已启动。关闭本窗口不影响服务继续运行。"
  echo "停止服务请在终端执行: ./start.sh stop"
else
  echo "启动失败，请查看 data/server.log"
fi
echo
echo "按回车键关闭窗口…"
read -r _
exit "$code"
