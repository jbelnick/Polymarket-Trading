"""
Kalshi Trading Bot — Configuration

All tunable parameters in one place. Override via environment variables.
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
QUEUE_FILE = DATA_DIR / "queue.json"
TARGETS_FILE = DATA_DIR / "targets.json"
THESIS_FILE = DATA_DIR / "thesis.json"
TRADES_LOG = DATA_DIR / "trades.json"
LOG_FILE = BASE_DIR / "log.txt"

# ── Kalshi API ─────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")  # "demo" or "prod"

KALSHI_BASE_URL = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com",
}[KALSHI_ENV]

KALSHI_API_BASE = KALSHI_BASE_URL + "/trade-api/v2"
MARKET_SCAN_LIMIT = int(os.getenv("MARKET_SCAN_LIMIT", "500"))

# ── Claude API ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL_FAST = os.getenv("CLAUDE_MODEL_FAST", "claude-haiku-4-5-20251001")  # cheap pre-screen
CLAUDE_MODEL_DEEP = os.getenv("CLAUDE_MODEL_DEEP", "claude-sonnet-4-20250514")  # full analysis

# ── Scanner thresholds ─────────────────────────────────────────────────────────
MIN_EDGE_GAP = float(os.getenv("MIN_EDGE_GAP", "0.07"))         # 7% minimum mispricing
MIN_BOOK_DEPTH = float(os.getenv("MIN_BOOK_DEPTH", "500"))      # $500 on both sides
MIN_HOURS_TO_RESOLUTION = float(os.getenv("MIN_HOURS", "4"))    # too late below 4h
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS", "168"))  # too slow above 7d
MIN_MARKET_VOLUME = float(os.getenv("MIN_MARKET_VOLUME", "50000"))  # $50K minimum

# ── Brain thresholds ──────────────────────────────────────────────────────────
MIN_CHECKS_PASSING = int(os.getenv("MIN_CHECKS_PASSING", "3"))     # 3 of 4 checks agree
MIN_THESIS_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.75")) # 75%

# ── Kelly sizing ───────────────────────────────────────────────────────────────
BANKROLL = float(os.getenv("BANKROLL", "100"))
MAX_KELLY_FRACTION = float(os.getenv("MAX_KELLY", "0.25"))      # quarter-Kelly cap
MIN_KELLY_FRACTION = float(os.getenv("MIN_KELLY", "0.02"))      # floor — skip dust

# ── Execution ──────────────────────────────────────────────────────────────────
CONSENSUS_FULL = 2          # agents agreeing → full position
CONSENSUS_HALF = 1          # single agent    → half position

# ── Exit triggers ──────────────────────────────────────────────────────────────
TARGET_PROFIT_FRACTION = float(os.getenv("TARGET_PROFIT_FRAC", "0.85"))  # 85% of gap
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOL_SPIKE_MULT", "3.0"))     # 3× avg
STALE_THESIS_HOURS = float(os.getenv("STALE_HOURS", "24"))              # 24h max hold
STALE_PRICE_THRESHOLD = float(os.getenv("STALE_PRICE_THRESH", "0.02"))  # <2% move = stale

# ── Loop intervals ────────────────────────────────────────────────────────────
# Scanner: free (just REST calls to Kalshi) — run often
# Brain:   expensive (Claude calls) — throttle to control cost
# Executor: always evaluating the latest theses
# Exit monitor: free (price checks) — run often

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL", "1800"))       # 30 minutes
BRAIN_INTERVAL_SEC = int(os.getenv("BRAIN_INTERVAL", "7200"))     # 2 hours (cost control)
EXIT_CHECK_INTERVAL_SEC = int(os.getenv("EXIT_INTERVAL", "30"))   # 30 seconds
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL", "3600"))               # 1 hour — matches brain cadence
CACHE_PRICE_CHANGE_THRESHOLD = float(os.getenv("CACHE_PRICE_THRESH", "0.02"))  # 2% move invalidates

# ── Disabled categories ───────────────────────────────────────────────────────
DISABLED_CATEGORIES = set(
    os.getenv("DISABLED_CATEGORIES", "sports").lower().split(",")
)
