#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Kalshi Trading Bot — Startup Script
#
# Runs daily at 06:00 UTC via cron:
#   0 6 * * * /path/to/kalshi-bot/start.sh >> /path/to/kalshi-bot/cron.log 2>&1
#
# Or launch manually:
#   ./start.sh           — full bot (scanner + brain + executor + exit monitor)
#   ./start.sh --scan    — scanner only (read-only)
#   ./start.sh --demo    — use Kalshi demo environment
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "$(date) — Kalshi Trading Bot starting (env=${KALSHI_ENV:-demo})"

case "${1:-}" in
    --scan)
        echo "$(date) — Starting scanner only (read-only)"
        exec python main.py --scan-only
        ;;
    --demo)
        echo "$(date) — Starting in demo mode"
        export KALSHI_ENV=demo
        exec python main.py --demo
        ;;
    *)
        echo "$(date) — Starting 4 agents"
        exec python main.py
        ;;
esac
