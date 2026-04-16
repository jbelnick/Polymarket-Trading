#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Polymarket Trading Bot — Startup Script
#
# Runs daily at 06:00 UTC via cron:
#   0 6 * * * /path/to/polymarket-bot/start.sh >> /path/to/polymarket-bot/cron.log 2>&1
#
# Or launch manually:
#   ./start.sh           — full bot (scanner + brain + executor + exit monitor)
#   ./start.sh --scan    — scanner only (read-only, no wallet)
#   ./start.sh --analyze — refresh target wallets from poly_data
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

# ── Step 1: Update data from poly_data ─────────────────────────────────────
if [ -d "$HOME/poly_data" ]; then
    echo "$(date) — Updating poly_data …"
    cd "$HOME/poly_data"
    uv run python -c "from update_utils.process_live import process_live; process_live()" || true
    cd "$BOT_DIR"
fi

# ── Step 2: Refresh market scan ────────────────────────────────────────────
echo "$(date) — Fetching active markets …"
mkdir -p data
polymarket markets list --limit 500 -o json > data/markets.json 2>/dev/null || true

# ── Step 3: Launch bot ─────────────────────────────────────────────────────
case "${1:-}" in
    --scan)
        echo "$(date) — Starting scanner only (read-only)"
        exec python main.py --scan-only
        ;;
    --analyze)
        echo "$(date) — Analyzing wallet data"
        exec python main.py --analyze "${HOME}/poly_data/processed/trades.csv"
        ;;
    *)
        echo "$(date) — Starting 4 agents"
        exec python main.py
        ;;
esac
