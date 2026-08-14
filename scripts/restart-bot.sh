#!/bin/bash
# 拉取最新代码并重启 bot（screen 会话名 nb）。
# 用法：bash /home/admin/nbbot/qwwshs/qwwshs/scripts/restart-bot.sh
set -e
export PATH="$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

echo "==> git pull"
git pull

echo "==> 结束旧 nb 会话"
screen -S nb -X quit 2>/dev/null || true
sleep 1

if [ -x .venv/bin/nb ]; then
    CMD=".venv/bin/nb run"
else
    CMD="nb run"
fi
echo "==> 启动新 nb 会话"
screen -dmS nb bash -c "cd '$DIR' && exec $CMD"

echo "OK: 已重启 nb（screen 会话名 nb），查看日志: screen -r nb"
