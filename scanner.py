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
    """
    Pull the `limit` active+open markets resolving soonest.

    The CLI's --order volume_num sorts by lifetime volume, which correlates
    with how long a market has been open, so it biases toward long-dated
    markets. --order volume24hr accepts but doesn't sort correctly.
    --order endDate ascending gets us the soonest-resolving markets, which
    matches the MIN_HOURS..MAX_HOURS prescreen window.
    """
    return _run_cli(
        "markets",
        "list",
        "--active", "true",
        "--closed", "false",
        "--order", "endDate",
        "--ascending",
        "--limit", str(limit),
    )


def fetch_order_book(token_id: str) -> dict:
    """Get the order book for a token."""
    return _run_cli("clob", "book", token_id)


def fetch_midpoint(token_id: str) -> float:
    """Get the current midpoint price for a token."""
    data = _run_cli("clob", "midpoint", token_id)
    return float(data.get("mid", data.get("midpoint", 0)))


# ── Market parsing ─────────────────────────────────────────────────────────────


def _parse_json_list(value) -> list:
    """The CLI returns some list fields as JSON-encoded strings (e.g. '["Yes","No"]')."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _hours_until(iso_date: str | None) -> float:
    """Convert an ISO timestamp (e.g. '2026-07-31T12:00:00Z' or '2026-07-31') into hours from now."""
    if not iso_date:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta / 3600)


def parse_market(raw: dict) -> Market | None:
    """Convert a polymarket-cli `markets list` JSON object into a Market. Returns None if unusable."""
    try:
        # Skip anything we obviously can't trade
        if raw.get("closed") or raw.get("archived") or raw.get("active") is False:
            return None
        if not raw.get("acceptingOrders", True):
            return None

        # Token id — CLI returns both outcome tokens as a stringified JSON list.
        # We take the first (YES) as the canonical token; direction flips happen elsewhere.
        token_ids = _parse_json_list(raw.get("clobTokenIds") or raw.get("token_ids"))
        token_id = token_ids[0] if token_ids else raw.get("token_id", "")
        if not token_id:
            return None

        outcomes = _parse_json_list(raw.get("outcomes"))

        # Midpoint — prefer best-bid/ask mean, fall back to last trade.
        best_bid = float(raw.get("bestBid") or 0)
        best_ask = float(raw.get("bestAsk") or 0)
        if best_bid > 0 and best_ask > 0:
            midpoint = (best_bid + best_ask) / 2
        else:
            midpoint = float(raw.get("lastTradePrice") or raw.get("midpoint") or 0)

        # Book depth — CLI gives a single liquidity total, not per-side. Approximate 50/50.
        # Real bid/ask depth can be fetched per-token with `polymarket clob book <token_id>`
        # but that's an extra N calls; for the prescreen filter this is close enough.
        liquidity_total = float(
            raw.get("liquidityClob") or raw.get("liquidityNum") or raw.get("liquidity") or 0
        )
        bids_depth = asks_depth = liquidity_total / 2

        volume_24h = float(
            raw.get("volume24hr") or raw.get("volume24hrClob") or raw.get("volume_24h") or 0
        )

        hours_to_resolution = _hours_until(raw.get("endDate") or raw.get("endDateIso"))

        # Category is often null at the top level; try the parent event as a fallback.
        category = raw.get("category")
        if not category:
            events = raw.get("events") or []
            if events and isinstance(events[0], dict):
                category = events[0].get("category") or events[0].get("subcategory")
        category = (category or "").lower()

        return Market(
            condition_id=raw.get("conditionId") or raw.get("condition_id", ""),
            question=raw.get("question", ""),
            token_id=token_id,
            midpoint=midpoint,
            bids_depth=bids_depth,
            asks_depth=asks_depth,
            volume_24h=volume_24h,
            hours_to_resolution=hours_to_resolution,
            category=category,
            outcomes=outcomes,
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
