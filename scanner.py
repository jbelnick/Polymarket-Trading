"""
Polymarket Trading Bot — Step 1: Market Scanner

Pulls active markets via polymarket-cli, scores them, and writes survivors
to data/queue.json for the brain to evaluate.

Runs in a loop (SCAN_INTERVAL_SEC) or once when called directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from config import (
    DATA_DIR,
    DISABLED_CATEGORIES,
    MAX_HOURS_TO_RESOLUTION,
    MARKET_SCAN_LIMIT,
    MIN_BOOK_DEPTH,
    MIN_EDGE_GAP,
    MIN_HOURS_TO_RESOLUTION,
    MIN_MARKET_VOLUME,
    POLYMARKET_CLI,
    QUEUE_FILE,
    SCAN_INTERVAL_SEC,
)
from models import Market, ScoredMarket

logger = logging.getLogger(__name__)


# ── Polymarket CLI wrappers ────────────────────────────────────────────────────


def _run_cli(*args: str) -> dict | list:
    """Run a polymarket-cli command and return parsed JSON."""
    cmd = [POLYMARKET_CLI, *args, "-o", "json"]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"polymarket-cli failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch_markets(limit: int = MARKET_SCAN_LIMIT) -> list[dict]:
    """Pull every active market as JSON."""
    return _run_cli("markets", "list", "--limit", str(limit))


def fetch_order_book(token_id: str) -> dict:
    """Get the order book for a token."""
    return _run_cli("clob", "book", token_id)


def fetch_midpoint(token_id: str) -> float:
    """Get the current midpoint price for a token."""
    data = _run_cli("clob", "midpoint", token_id)
    return float(data.get("mid", data.get("midpoint", 0)))


# ── Market parsing ─────────────────────────────────────────────────────────────


def parse_market(raw: dict) -> Market | None:
    """Convert raw CLI JSON into a Market dataclass. Returns None if unparseable."""
    try:
        token_id = raw.get("token_id") or raw.get("tokens", [{}])[0].get("token_id", "")
        if not token_id:
            return None

        return Market(
            condition_id=raw.get("condition_id", ""),
            question=raw.get("question", ""),
            token_id=token_id,
            midpoint=float(raw.get("midpoint", 0) or 0),
            bids_depth=float(raw.get("bids_depth", 0) or 0),
            asks_depth=float(raw.get("asks_depth", 0) or 0),
            volume_24h=float(raw.get("volume_24h", 0) or raw.get("volume", 0) or 0),
            hours_to_resolution=float(raw.get("hours_to_resolution", 0) or 0),
            category=raw.get("category", "").lower(),
            outcomes=raw.get("outcomes", []),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.debug("Skipping unparseable market: %s", exc)
        return None


# ── Scoring ────────────────────────────────────────────────────────────────────


def score_market(market: Market, claude_estimate: float) -> ScoredMarket | None:
    """
    Score a market on three factors:
      1. gap between market price and Claude's probability estimate
      2. order book depth — at least MIN_BOOK_DEPTH on both sides
      3. hours until resolution — sweet spot is MIN_HOURS … MAX_HOURS

    Returns None (killed) if any filter fails.
    """
    price = market.midpoint
    gap = abs(claude_estimate - price)
    depth = min(market.bids_depth, market.asks_depth)
    hours = market.hours_to_resolution

    # ── Hard kills ─────────────────────────────────────────────────────────
    if gap < MIN_EDGE_GAP:
        return None  # edge too thin
    if depth < MIN_BOOK_DEPTH:
        return None  # can't fill
    if hours < MIN_HOURS_TO_RESOLUTION:
        return None  # too late
    if hours > MAX_HOURS_TO_RESOLUTION:
        return None  # too slow
    if market.volume_24h < MIN_MARKET_VOLUME:
        return None  # slippage risk
    if market.category in DISABLED_CATEGORIES:
        return None  # disabled category (e.g. sports)

    ev = round(gap * depth * 0.001, 2)

    return ScoredMarket(
        market=market,
        gap=round(gap, 3),
        depth=depth,
        hours=hours,
        ev=ev,
        claude_estimate=claude_estimate,
    )


# ── Quick pre-screen (no Claude call yet) ─────────────────────────────────────


def prescreen_market(market: Market) -> bool:
    """
    Cheap filter before we spend a Claude API call on probability estimation.
    Checks depth, hours, volume, and category only.
    """
    depth = min(market.bids_depth, market.asks_depth)
    if depth < MIN_BOOK_DEPTH:
        return False
    if market.hours_to_resolution < MIN_HOURS_TO_RESOLUTION:
        return False
    if market.hours_to_resolution > MAX_HOURS_TO_RESOLUTION:
        return False
    if market.volume_24h < MIN_MARKET_VOLUME:
        return False
    if market.category in DISABLED_CATEGORIES:
        return False
    return True


# ── Persistence ────────────────────────────────────────────────────────────────


def save_queue(scored: list[ScoredMarket], path: Path = QUEUE_FILE) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "condition_id": s.market.condition_id,
            "question": s.market.question,
            "token_id": s.market.token_id,
            "midpoint": s.market.midpoint,
            "gap": s.gap,
            "depth": s.depth,
            "hours": s.hours,
            "ev": s.ev,
            "claude_estimate": s.claude_estimate,
            "category": s.market.category,
        }
        for s in scored
    ]
    path.write_text(json.dumps(payload, indent=2))
    logger.info("Saved %d scored markets to %s", len(scored), path)


def load_queue(path: Path = QUEUE_FILE) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


# ── Main loop ──────────────────────────────────────────────────────────────────


async def scan_loop() -> None:
    """Continuous scanning loop. Writes queue.json each cycle."""
    logger.info("Scanner starting — interval %ds", SCAN_INTERVAL_SEC)

    while True:
        try:
            raw_markets = fetch_markets()
            logger.info("Fetched %d markets from CLI", len(raw_markets))

            markets = [m for raw in raw_markets if (m := parse_market(raw)) is not None]
            prescreened = [m for m in markets if prescreen_market(m)]
            logger.info(
                "%d markets parsed, %d survived pre-screen",
                len(markets),
                len(prescreened),
            )

            # At this stage we don't have Claude estimates yet.
            # Save prescreened markets for the brain to estimate and score.
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            prescreened_payload = [
                {
                    "condition_id": m.condition_id,
                    "question": m.question,
                    "token_id": m.token_id,
                    "midpoint": m.midpoint,
                    "bids_depth": m.bids_depth,
                    "asks_depth": m.asks_depth,
                    "volume_24h": m.volume_24h,
                    "hours_to_resolution": m.hours_to_resolution,
                    "category": m.category,
                }
                for m in prescreened
            ]
            (DATA_DIR / "markets.json").write_text(
                json.dumps(prescreened_payload, indent=2)
            )

        except Exception:
            logger.exception("Scanner cycle failed")

        await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(scan_loop())
