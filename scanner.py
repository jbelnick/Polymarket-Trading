"""
Kalshi Trading Bot — Step 1: Market Scanner

Pulls active markets via the Kalshi API, scores them, and writes survivors
to data/queue.json for the brain to evaluate.

Runs in a loop (SCAN_INTERVAL_SEC) or once when called directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
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
    QUEUE_FILE,
    SCAN_INTERVAL_SEC,
)
from kalshi_client import KalshiClient
from models import Market, ScoredMarket

logger = logging.getLogger(__name__)


# ── Market parsing ─────────────────────────────────────────────────────────────


def _hours_until(iso_ts: str | None) -> float:
    """Convert an ISO 8601 timestamp to hours from now."""
    if not iso_ts:
        return 0.0
    try:
        # Handle both "Z" and "+00:00" suffixes
        ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        delta = dt - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        return 0.0


def parse_market(raw: dict, client: KalshiClient | None = None) -> Market | None:
    """Convert raw Kalshi API JSON into a Market dataclass."""
    try:
        ticker = raw.get("ticker", "")
        if not ticker:
            return None

        status = raw.get("status", "")
        if status != "open":
            return None

        yes_price = (raw.get("yes_price", 0) or 0)
        no_price = (raw.get("no_price", 0) or 0)

        # Kalshi returns prices in cents — convert to dollars
        if yes_price > 1:
            yes_price = yes_price / 100
        if no_price > 1:
            no_price = no_price / 100

        yes_bid = (raw.get("yes_bid", 0) or 0)
        yes_ask = (raw.get("yes_ask", 0) or 0)
        if yes_bid > 1:
            yes_bid = yes_bid / 100
        if yes_ask > 1:
            yes_ask = yes_ask / 100

        hours = _hours_until(
            raw.get("close_time")
            or raw.get("expiration_time")
            or raw.get("expected_expiration_time")
        )

        return Market(
            ticker=ticker,
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", raw.get("question", "")),
            subtitle=raw.get("subtitle", ""),
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            volume=int(raw.get("volume", 0) or 0),
            volume_24h=int(raw.get("volume_24h", 0) or 0),
            open_interest=int(raw.get("open_interest", 0) or 0),
            hours_to_resolution=hours,
            category=raw.get("category", "").lower(),
            status=status,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("Skipping unparseable market: %s", exc)
        return None


# ── Scoring ────────────────────────────────────────────────────────────────────


def score_market(market: Market, claude_estimate: float) -> ScoredMarket | None:
    """
    Score a market on three factors:
      1. Gap between market price and Claude's probability estimate
      2. Order book depth
      3. Hours until resolution — sweet spot is MIN_HOURS … MAX_HOURS

    Returns None (killed) if any filter fails.
    """
    price = market.yes_price
    gap = abs(claude_estimate - price)
    depth = min(market.yes_bid, market.yes_ask) * market.open_interest if market.open_interest else 0
    hours = market.hours_to_resolution

    if gap < MIN_EDGE_GAP:
        return None
    if hours < MIN_HOURS_TO_RESOLUTION:
        return None
    if hours > MAX_HOURS_TO_RESOLUTION:
        return None
    if market.volume_24h < MIN_MARKET_VOLUME:
        return None
    if market.category in DISABLED_CATEGORIES:
        return None

    ev = round(gap * max(depth, 1) * 0.001, 2)

    return ScoredMarket(
        market=market,
        gap=round(gap, 3),
        depth_dollars=depth,
        hours=hours,
        ev=ev,
        claude_estimate=claude_estimate,
    )


# ── Pre-screen (no Claude call yet) ───────────────────────────────────────────


def prescreen_market(market: Market) -> bool:
    """Cheap filter before spending a Claude API call."""
    if market.hours_to_resolution < MIN_HOURS_TO_RESOLUTION:
        return False
    if market.hours_to_resolution > MAX_HOURS_TO_RESOLUTION:
        return False
    if market.volume_24h < MIN_MARKET_VOLUME:
        return False
    if market.category in DISABLED_CATEGORIES:
        return False
    if market.yes_price <= 0.03 or market.yes_price >= 0.97:
        return False  # too close to resolution — no edge
    return True


# ── Persistence ────────────────────────────────────────────────────────────────


def save_queue(scored: list[ScoredMarket], path: Path = QUEUE_FILE) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "ticker": s.market.ticker,
            "event_ticker": s.market.event_ticker,
            "title": s.market.title,
            "yes_price": s.market.yes_price,
            "no_price": s.market.no_price,
            "volume_24h": s.market.volume_24h,
            "open_interest": s.market.open_interest,
            "hours_to_resolution": s.hours,
            "gap": s.gap,
            "depth_dollars": s.depth_dollars,
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


async def scan_loop(client: KalshiClient | None = None) -> None:
    """Continuous scanning loop. Writes markets.json each cycle."""
    logger.info("Scanner starting — interval %ds", SCAN_INTERVAL_SEC)

    if client is None:
        client = KalshiClient()

    while True:
        try:
            raw_markets = client.get_all_markets(status="open", limit=MARKET_SCAN_LIMIT)
            logger.info("Fetched %d markets from Kalshi", len(raw_markets))

            markets = [m for raw in raw_markets if (m := parse_market(raw)) is not None]
            prescreened = [m for m in markets if prescreen_market(m)]
            logger.info(
                "%d markets parsed, %d survived pre-screen",
                len(markets),
                len(prescreened),
            )

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            prescreened_payload = [
                {
                    "ticker": m.ticker,
                    "event_ticker": m.event_ticker,
                    "title": m.title,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "volume_24h": m.volume_24h,
                    "open_interest": m.open_interest,
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
