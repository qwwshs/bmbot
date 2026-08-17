#!/bin/bash
# Pull latest code and restart bot (screen session: nb).
# Usage: bash /home/admin/nbbot/qwwshs/scripts/restart-bot.sh
set -e
export PATH="$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

echo "==> git pull"
git pull

echo "==> syncing new chart data into constants"
python3 scripts/sync-constants.py || echo "sync-constants skipped (non-fatal)"

echo "==> rebuilding /bmchartlist all cache image (if constants changed)"
python3 scripts/build-all-charts.py || echo "build-all-charts skipped (non-fatal)"

echo "==> stopping old nb session"
screen -S nb -X quit 2>/dev/null || true
sleep 1

if [ -x .venv/bin/nb ]; then
    CMD=".venv/bin/nb run"
else
    CMD="nb run"
fi
echo "==> starting new nb session"
screen -dmS nb bash -c "cd '$DIR' && exec $CMD"

echo "OK: nb restarted (screen session: nb). View log: screen -r nb"
