"""
Polymarket Trading Bot — Step 4: Exit Monitor

Three exit triggers — the part nobody talks about:

  1. TARGET HIT  — take profit at 85% of expected move
  2. VOLUME SPIKE — 3× normal volume = smart money leaving
  3. STALE THESIS — thesis is stale after 24h with <2% price move

91% of exits from top wallets happen BEFORE resolution.
Average exit: 73% of max potential profit captured.
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
    DRY_RUN,
    EXIT_CHECK_INTERVAL_SEC,
    STALE_PRICE_THRESHOLD,
    STALE_THESIS_HOURS,
    TARGET_PROFIT_FRACTION,
    TRADES_LOG,
    VOLUME_SPIKE_MULTIPLIER,
)
from models import ExitReason, Position, Side
from scanner import fetch_midpoint

logger = logging.getLogger(__name__)


# ── Volume tracking ────────────────────────────────────────────────────────────

# Rolling window of 10-minute volume samples per token
_volume_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
_VOLUME_WINDOW_SEC = 600  # 10 minutes


def record_volume(token_id: str, volume: float) -> None:
    """Record a volume data point for a token."""
    now = time.time()
    _volume_history[token_id].append((now, volume))
    # Prune old entries (keep last hour)
    cutoff = now - 3600
    _volume_history[token_id] = [
        (t, v) for t, v in _volume_history[token_id] if t > cutoff
    ]


def get_avg_volume_10min(token_id: str) -> float:
    """Average volume per 10-minute window over the last hour."""
    history = _volume_history.get(token_id, [])
    if len(history) < 2:
        return float("inf")  # not enough data — don't trigger

    # Sum volume deltas across the history
    total_volume = sum(v for _, v in history)
    time_span = history[-1][0] - history[0][0]

    if time_span <= 0:
        return float("inf")

    windows = time_span / _VOLUME_WINDOW_SEC
    return total_volume / max(windows, 1)


def get_recent_volume_10min(token_id: str) -> float:
    """Volume in the most recent 10-minute window."""
    now = time.time()
    cutoff = now - _VOLUME_WINDOW_SEC
    history = _volume_history.get(token_id, [])
    return sum(v for t, v in history if t > cutoff)


# ── Exit checks ────────────────────────────────────────────────────────────────


def check_target_hit(position: Position, current_price: float) -> bool:
    """
    Exit trigger #1: Take profit at 85% of expected move.
    Top wallets don't hold to settlement — they capture 73% on average.
    """
    if position.side == Side.BUY:
        target = position.entry_price + (position.expected_gap * TARGET_PROFIT_FRACTION)
        return current_price >= target
    else:
        target = position.entry_price - (position.expected_gap * TARGET_PROFIT_FRACTION)
        return current_price <= target


def check_volume_spike(position: Position) -> bool:
    """
    Exit trigger #2: Volume spike — 3× normal = smart money leaving.
    This is the exit trigger nobody talks about.
    """
    avg = get_avg_volume_10min(position.token_id)
    recent = get_recent_volume_10min(position.token_id)

    if avg == float("inf") or avg <= 0:
        return False

    return recent > avg * VOLUME_SPIKE_MULTIPLIER


def check_stale_thesis(position: Position, current_price: float) -> bool:
    """
    Exit trigger #3: Thesis is stale after 24h with <2% price move.
    If nothing happened, the edge is probably gone.
    """
    if position.hours_held < STALE_THESIS_HOURS:
        return False

    price_change = abs(current_price - position.entry_price)
    return price_change < STALE_PRICE_THRESHOLD


def evaluate_exit(position: Position, current_price: float) -> ExitReason | None:
    """
    Run all three exit checks. Returns the first triggered reason, or None.
    Priority: target hit > volume spike > stale thesis.
    """
    if check_target_hit(position, current_price):
        return ExitReason.TARGET_HIT

    if check_volume_spike(position):
        return ExitReason.VOLUME_EXIT

    if check_stale_thesis(position, current_price):
        return ExitReason.STALE_THESIS

    return None


# ── Exit execution ─────────────────────────────────────────────────────────────


async def close_position(position: Position, reason: ExitReason, exit_price: float) -> bool:
    """
    Close an open position by submitting the opposite-side FAK order
    at the inside of the book, then record the resulting PnL on the
    Position object.

    The `exit_price` passed in is the midpoint used for PnL bookkeeping;
    the actual submitted limit is read from the order book at send time.
    """
    # Import lazily to avoid a circular import (exit_monitor ↔ executor).
    from executor import (
        CLOB_BUY,
        CLOB_SELL,
        _inside_spread_price,
        get_clob_client,
        save_positions,
    )
    from py_clob_client.clob_types import OrderArgs, OrderType

    # Closing a long = sell; closing a short = buy.
    closing_side = Side.SELL if position.side == Side.BUY else Side.BUY
    shares = round(position.size / position.entry_price, 2) if position.entry_price > 0 else 0

    if shares < 5:
        logger.warning(
            "EXIT skipped — position %s too small to close (shares=%.2f)",
            position.token_id[:12],
            shares,
        )
        return False

    fill_price = exit_price  # default if we can't read the book or DRY_RUN

    if DRY_RUN:
        logger.info(
            "DRY_RUN EXIT %s: %s %.2f shares @ %.4f — %s",
            reason.value,
            closing_side.value,
            shares,
            fill_price,
            position.question[:50],
        )
    else:
        limit_price = _inside_spread_price(position.token_id, closing_side)
        if limit_price is None or limit_price <= 0 or limit_price >= 1:
            logger.warning(
                "EXIT skipped — no inside price for %s",
                position.token_id[:12],
            )
            return False

        side_constant = CLOB_BUY if closing_side == Side.BUY else CLOB_SELL

        try:
            client = get_clob_client()
            signed = client.create_order(
                OrderArgs(
                    token_id=position.token_id,
                    price=limit_price,
                    size=shares,
                    side=side_constant,
                )
            )
            resp = client.post_order(signed, OrderType.FAK)
        except Exception:
            logger.exception("Exit order failed for %s", position.token_id[:12])
            return False

        if not resp or not resp.get("success"):
            logger.error("EXIT rejected: %s — resp=%s", position.question[:50], resp)
            return False

        filled_shares = float(resp.get("takingAmount") or 0)
        if filled_shares <= 0:
            logger.warning(
                "EXIT zero-fill (FAK): %s — book too thin at %.4f",
                position.question[:50],
                limit_price,
            )
            return False

        fill_price = limit_price

    # Record the exit
    position.exit_price = fill_price
    position.exit_time = time.time()
    position.exit_reason = reason

    if position.side == Side.BUY:
        position.pnl = round((fill_price - position.entry_price) * shares, 2)
    else:
        position.pnl = round((position.entry_price - fill_price) * shares, 2)

    logger.info(
        "EXIT %s: %s — entry=%.4f exit=%.4f pnl=$%.2f held=%.1fh — %s",
        reason.value,
        position.question[:50],
        position.entry_price,
        fill_price,
        position.pnl,
        position.hours_held,
        position.token_id[:12],
    )

    # Persist updated state (the list lives in executor._open_positions).
    try:
        save_positions()
    except Exception:
        logger.exception("Failed to persist positions after exit")

    return True


def update_trade_log(position: Position) -> None:
    """Update the trade log with exit information."""
    log_path = TRADES_LOG
    if not log_path.exists():
        return

    trades = json.loads(log_path.read_text())
    for trade in trades:
        if trade.get("token_id") == position.token_id and "exit_price" not in trade:
            trade["exit_price"] = position.exit_price
            trade["exit_time"] = position.exit_time
            trade["exit_reason"] = position.exit_reason.value if position.exit_reason else None
            trade["pnl"] = position.pnl
            trade["hours_held"] = round(position.hours_held, 2)
            break

    log_path.write_text(json.dumps(trades, indent=2))


# ── Stats ──────────────────────────────────────────────────────────────────────


def compute_stats(positions: list[Position]) -> dict:
    """Compute aggregate trading statistics."""
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


async def exit_monitor_loop(get_positions_fn) -> None:
    """
    Continuous exit monitoring loop.
    Checks every open position against all exit triggers.
    """
    logger.info("Exit monitor starting — interval %ds", EXIT_CHECK_INTERVAL_SEC)

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
                    current_price = fetch_midpoint(position.token_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch price for %s: %s",
                        position.token_id[:12],
                        exc,
                    )
                    continue

                reason = evaluate_exit(position, current_price)

                if reason is not None:
                    success = await close_position(position, reason, current_price)
                    if success:
                        update_trade_log(position)

        except Exception:
            logger.exception("Exit monitor cycle failed")

        await asyncio.sleep(EXIT_CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # When run standalone, import positions from executor
    from executor import get_open_positions

    asyncio.run(exit_monitor_loop(get_open_positions))
