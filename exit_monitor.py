"""
Kalshi Trading Bot — Step 4: Exit Monitor

Three exit triggers:

  1. TARGET HIT  — take profit at 85% of expected move
  2. VOLUME SPIKE — 3× normal volume = smart money leaving
  3. STALE THESIS — thesis is stale after 24h with <2% price move

Top traders don't hold to settlement — they capture ~73% of max profit and move on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from config import (
    DATA_DIR,
    EXIT_CHECK_INTERVAL_SEC,
    STALE_PRICE_THRESHOLD,
    STALE_THESIS_HOURS,
    TARGET_PROFIT_FRACTION,
    TRADES_LOG,
    VOLUME_SPIKE_MULTIPLIER,
)
from kalshi_client import KalshiClient
from models import Action, ExitReason, Position, Side

logger = logging.getLogger(__name__)


# ── Volume tracking ────────────────────────────────────────────────────────────

_volume_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
_VOLUME_WINDOW_SEC = 600  # 10 minutes


def record_volume(ticker: str, volume: float) -> None:
    now = time.time()
    _volume_history[ticker].append((now, volume))
    cutoff = now - 3600
    _volume_history[ticker] = [
        (t, v) for t, v in _volume_history[ticker] if t > cutoff
    ]


def get_avg_volume_10min(ticker: str) -> float:
    history = _volume_history.get(ticker, [])
    if len(history) < 2:
        return float("inf")
    total_volume = sum(v for _, v in history)
    time_span = history[-1][0] - history[0][0]
    if time_span <= 0:
        return float("inf")
    windows = time_span / _VOLUME_WINDOW_SEC
    return total_volume / max(windows, 1)


def get_recent_volume_10min(ticker: str) -> float:
    now = time.time()
    cutoff = now - _VOLUME_WINDOW_SEC
    history = _volume_history.get(ticker, [])
    return sum(v for t, v in history if t > cutoff)


# ── Exit checks ────────────────────────────────────────────────────────────────


def check_target_hit(position: Position, current_price: float) -> bool:
    """Exit trigger #1: Take profit at 85% of expected move."""
    if position.action == Action.BUY:
        target = position.entry_price + (position.expected_gap * TARGET_PROFIT_FRACTION)
        return current_price >= target
    else:
        target = position.entry_price - (position.expected_gap * TARGET_PROFIT_FRACTION)
        return current_price <= target


def check_volume_spike(position: Position) -> bool:
    """Exit trigger #2: Volume spike — 3× normal = smart money leaving."""
    avg = get_avg_volume_10min(position.ticker)
    recent = get_recent_volume_10min(position.ticker)
    if avg == float("inf") or avg <= 0:
        return False
    return recent > avg * VOLUME_SPIKE_MULTIPLIER


def check_stale_thesis(position: Position, current_price: float) -> bool:
    """Exit trigger #3: Thesis is stale after 24h with <2% price move."""
    if position.hours_held < STALE_THESIS_HOURS:
        return False
    price_change = abs(current_price - position.entry_price)
    return price_change < STALE_PRICE_THRESHOLD


def evaluate_exit(position: Position, current_price: float) -> ExitReason | None:
    """Run all three exit checks. Returns the first triggered reason, or None."""
    if check_target_hit(position, current_price):
        return ExitReason.TARGET_HIT
    if check_volume_spike(position):
        return ExitReason.VOLUME_EXIT
    if check_stale_thesis(position, current_price):
        return ExitReason.STALE_THESIS
    return None


# ── Exit execution ─────────────────────────────────────────────────────────────


async def close_position(
    client: KalshiClient,
    position: Position,
    reason: ExitReason,
    exit_price: float,
) -> bool:
    """
    Close an open position by placing an opposing order on Kalshi.
    BUY position → SELL to close.
    """
    try:
        close_action = "sell" if position.action == Action.BUY else "buy"
        price_cents = int(exit_price * 100)

        client.place_order(
            ticker=position.ticker,
            action=close_action,
            side=position.side.value,
            count=position.count,
            type="limit",
            yes_price=price_cents if position.side == Side.YES else None,
            no_price=price_cents if position.side == Side.NO else None,
        )

        position.exit_price = exit_price
        position.exit_time = time.time()
        position.exit_reason = reason

        if position.action == Action.BUY:
            position.pnl = round((exit_price - position.entry_price) * position.count, 2)
        else:
            position.pnl = round((position.entry_price - exit_price) * position.count, 2)

        logger.info(
            "EXIT %s: %s — entry=$%.2f exit=$%.2f pnl=$%.2f held=%.1fh — %s",
            reason.value,
            position.title[:50],
            position.entry_price,
            exit_price,
            position.pnl,
            position.hours_held,
            position.ticker,
        )
        return True

    except Exception as exc:
        logger.error("Failed to close %s: %s", position.ticker, exc)
        return False


def update_trade_log(position: Position) -> None:
    """Update the trade log with exit information."""
    log_path = TRADES_LOG
    if not log_path.exists():
        return

    trades = json.loads(log_path.read_text())
    for trade in trades:
        if trade.get("ticker") == position.ticker and "exit_price" not in trade:
            trade["exit_price"] = position.exit_price
            trade["exit_time"] = position.exit_time
            trade["exit_reason"] = position.exit_reason.value if position.exit_reason else None
            trade["pnl"] = position.pnl
            trade["hours_held"] = round(position.hours_held, 2)
            break

    log_path.write_text(json.dumps(trades, indent=2))


# ── Stats ──────────────────────────────────────────────────────────────────────


def compute_stats(positions: list[Position]) -> dict:
    closed = [p for p in positions if not p.is_open and p.pnl is not None]
    if not closed:
        return {"trades": 0}

    wins = [p for p in closed if p.pnl > 0]
    total_pnl = sum(p.pnl for p in closed)

    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed), 4),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2),
        "avg_hold_hours": round(
            sum(p.hours_held for p in closed) / len(closed), 1
        ),
        "exits_by_reason": {
            reason.value: len([p for p in closed if p.exit_reason == reason])
            for reason in ExitReason
        },
    }


# ── Main loop ──────────────────────────────────────────────────────────────────


async def exit_monitor_loop(get_positions_fn, client: KalshiClient | None = None) -> None:
    """Continuous exit monitoring loop."""
    logger.info("Exit monitor starting — interval %ds", EXIT_CHECK_INTERVAL_SEC)

    if client is None:
        client = KalshiClient()

    while True:
        try:
            positions = get_positions_fn()
            open_positions = [p for p in positions if p.is_open]

            if not open_positions:
                await asyncio.sleep(EXIT_CHECK_INTERVAL_SEC)
                continue

            logger.debug("Monitoring %d open positions", len(open_positions))

            for position in open_positions:
                try:
                    current_price = client.get_midpoint(position.ticker)

                    # Record volume for spike detection
                    market = client.get_market(position.ticker)
                    vol = market.get("volume_24h", market.get("volume", 0))
                    record_volume(position.ticker, vol)

                except Exception as exc:
                    logger.warning("Failed to fetch data for %s: %s", position.ticker, exc)
                    continue

                reason = evaluate_exit(position, current_price)

                if reason is not None:
                    success = await close_position(client, position, reason, current_price)
                    if success:
                        update_trade_log(position)

        except Exception:
            logger.exception("Exit monitor cycle failed")

        await asyncio.sleep(EXIT_CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from executor import get_open_positions
    asyncio.run(exit_monitor_loop(get_open_positions))
