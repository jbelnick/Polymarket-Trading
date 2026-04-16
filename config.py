"""
Polymarket Trading Bot — Configuration

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

# ── Polymarket CLI ─────────────────────────────────────────────────────────────
POLYMARKET_CLI = os.getenv("POLYMARKET_CLI", "polymarket")
MARKET_SCAN_LIMIT = int(os.getenv("MARKET_SCAN_LIMIT", "500"))

# ── Wallet / Auth ──────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
API_KEY = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")

# ── Claude API ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Scanner thresholds ─────────────────────────────────────────────────────────
MIN_EDGE_GAP = float(os.getenv("MIN_EDGE_GAP", "0.07"))        # 7% minimum mispricing
MIN_BOOK_DEPTH = float(os.getenv("MIN_BOOK_DEPTH", "500"))      # $500 on both sides
MIN_HOURS_TO_RESOLUTION = float(os.getenv("MIN_HOURS", "4"))    # too late below 4h
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS", "168"))  # too slow above 7d
MIN_MARKET_VOLUME = float(os.getenv("MIN_MARKET_VOLUME", "50000"))  # $50K minimum

# ── Brain thresholds ──────────────────────────────────────────────────────────
MIN_CHECKS_PASSING = int(os.getenv("MIN_CHECKS_PASSING", "3"))  # 3 of 4 checks agree
MIN_THESIS_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.75"))  # 75%

# ── Kelly sizing ───────────────────────────────────────────────────────────────
BANKROLL = float(os.getenv("BANKROLL", "800"))
MAX_KELLY_FRACTION = float(os.getenv("MAX_KELLY", "0.25"))      # quarter-Kelly cap
MIN_KELLY_FRACTION = float(os.getenv("MIN_KELLY", "0.02"))      # floor — skip dust

# ── Execution ──────────────────────────────────────────────────────────────────
CONSENSUS_FULL = 2          # agents agreeing → full position
CONSENSUS_HALF = 1          # single agent    → half position
WHALE_COPY_DELAY_SEC = 60   # seconds to wait before mirroring whale trade

# ── Exit triggers ──────────────────────────────────────────────────────────────
TARGET_PROFIT_FRACTION = float(os.getenv("TARGET_PROFIT_FRAC", "0.85"))  # 85% of gap
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOL_SPIKE_MULT", "3.0"))     # 3× avg
STALE_THESIS_HOURS = float(os.getenv("STALE_HOURS", "24"))              # 24h max hold
STALE_PRICE_THRESHOLD = float(os.getenv("STALE_PRICE_THRESH", "0.02"))  # <2% move = stale

# ── Scan loop timing ──────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL", "300"))       # 5 minutes
BRAIN_INTERVAL_SEC = int(os.getenv("BRAIN_INTERVAL", "60"))      # 1 minute
EXIT_CHECK_INTERVAL_SEC = int(os.getenv("EXIT_INTERVAL", "30"))  # 30 seconds

# ── Disabled categories ───────────────────────────────────────────────────────
DISABLED_CATEGORIES = set(
    os.getenv("DISABLED_CATEGORIES", "sports").lower().split(",")
)
