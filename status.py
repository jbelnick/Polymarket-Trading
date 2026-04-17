"""
Polymarket Trading Bot — Status Dashboard

One-shot CLI snapshot of the bot's current state. Reads the JSON files
the running bot writes to data/ and fetches live midpoints from the
CLOB to compute unrealized P&L.

    python status.py            # full summary
    python status.py --watch    # refresh every 10 seconds
    python status.py --no-live  # skip the live price fetch (offline)
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    DATA_DIR,
    DRY_RUN,
    POSITIONS_FILE,
    THESIS_FILE,
    TRADES_LOG,
)


def _fmt_time(unix: float | None) -> str:
    if not unix:
        return "—"
    return datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_hours(seconds: float) -> str:
    h = seconds / 3600
    if h < 1:
        return f"{int(seconds // 60)}m"
    if h < 24:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def _pct(x: float) -> str:
    return f"{x*100:+.1f}%"


def _money(x: float) -> str:
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):.2f}"


def _read_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default if default is not None else []


def _load_env() -> dict:
    env_path = DATA_DIR.parent / ".env"
    out = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _last_log_events() -> dict:
    """Scrape a few interesting recent events out of log.txt."""
    log = DATA_DIR.parent / "log.txt"
    if not log.exists():
        return {}

    info = {}
    try:
        lines = log.read_text().splitlines()
    except Exception:
        return {}

    # Walk backwards looking for specific signals
    for line in reversed(lines[-500:]):
        if "actionable theses this cycle" in line and "last_cycle" not in info:
            info["last_cycle"] = line[:19]
        if "markets parsed," in line and "last_scan" not in info:
            info["last_scan"] = line[:19]
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok == "markets" and i > 0 and parts[i - 1].isdigit():
                    info["parsed"] = int(parts[i - 1])
                if tok == "survived":
                    try:
                        info["survived"] = int(parts[i - 1])
                    except (ValueError, IndexError):
                        pass
        if all(k in info for k in ("last_cycle", "last_scan")):
            break
    return info


def fetch_live_prices(token_ids: list[str]) -> dict[str, float]:
    """Get current midpoint for each token (one HTTP call per)."""
    from scanner import fetch_midpoint
    out = {}
    for tid in token_ids:
        try:
            out[tid] = fetch_midpoint(tid)
        except Exception:
            out[tid] = 0.0
    return out


def render(live: bool = True) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    positions = _read_json(POSITIONS_FILE)
    theses = _read_json(THESIS_FILE)
    trades = _read_json(TRADES_LOG)
    env = _load_env()
    log_info = _last_log_events()

    open_pos = [p for p in positions if p.get("exit_time") is None]
    closed_pos = [p for p in positions if p.get("exit_time") is not None]

    bankroll = float(env.get("BANKROLL", "800"))
    deployed = sum(p["size"] for p in open_pos)
    free = bankroll - deployed

    print("=" * 78)
    print(f" Polymarket Bot — {now_str}   mode: {'DRY_RUN' if DRY_RUN else 'LIVE'}")
    print("=" * 78)

    # ── Bankroll ──────────────────────────────────────────────────
    print(f"\n  Bankroll  ${bankroll:,.0f}   deployed ${deployed:,.2f}   free ${free:,.2f}")

    # ── Scanner / brain ───────────────────────────────────────────
    if log_info:
        print(
            f"  Last scan    {log_info.get('last_scan','—')}   "
            f"parsed={log_info.get('parsed','?')}  "
            f"survived={log_info.get('survived','?')}"
        )
    print(f"  Theses in queue: {len(theses)}")

    # ── Open positions ────────────────────────────────────────────
    print(f"\n  Open positions: {len(open_pos)}")
    if open_pos:
        live_prices: dict[str, float] = {}
        if live:
            print("  fetching live prices …")
            live_prices = fetch_live_prices([p["token_id"] for p in open_pos])

        total_unrealized = 0.0
        print()
        print("  " + "─" * 74)
        print(f"  {'SIDE':<4} {'SIZE':>7} {'ENTRY':>7} {'NOW':>7} {'PNL':>10} {'HELD':>6}  QUESTION")
        print("  " + "─" * 74)
        for p in open_pos:
            tid = p["token_id"]
            cur = live_prices.get(tid, 0.0)
            shares = p["size"] / p["entry_price"] if p["entry_price"] > 0 else 0
            if p["side"] == "BUY":
                unreal = (cur - p["entry_price"]) * shares if cur else 0.0
            else:
                unreal = (p["entry_price"] - cur) * shares if cur else 0.0
            total_unrealized += unreal
            held = _fmt_hours(time.time() - p["entry_time"])
            print(
                f"  {p['side']:<4} ${p['size']:>6.2f} "
                f"{p['entry_price']:>7.4f} {cur:>7.4f} "
                f"{_money(unreal):>10} {held:>6}  "
                f"{p['question'][:40]}"
            )
        print("  " + "─" * 74)
        print(f"  {'TOTAL UNREALIZED P&L:':>40} {_money(total_unrealized):>10}")

    # ── Closed positions summary ──────────────────────────────────
    if closed_pos:
        wins = [p for p in closed_pos if (p.get("pnl") or 0) > 0]
        total_pnl = sum((p.get("pnl") or 0) for p in closed_pos)
        print(
            f"\n  Closed: {len(closed_pos)}   "
            f"wins {len(wins)}/{len(closed_pos)} ({100*len(wins)/len(closed_pos):.0f}%)   "
            f"realized P&L {_money(total_pnl)}"
        )
        # Break down exits by reason
        reasons: dict[str, int] = {}
        for p in closed_pos:
            r = p.get("exit_reason") or "—"
            reasons[r] = reasons.get(r, 0) + 1
        if reasons:
            print("  Exits by reason: " + "  ".join(f"{r}={n}" for r, n in reasons.items()))

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket bot status dashboard")
    parser.add_argument("--watch", action="store_true", help="refresh every 10s")
    parser.add_argument("--no-live", action="store_true", help="skip live price fetch")
    args = parser.parse_args()

    if not args.watch:
        render(live=not args.no_live)
        return

    try:
        while True:
            # Clear screen (ANSI)
            print("\033[2J\033[H", end="")
            render(live=not args.no_live)
            print("  (refreshing every 10s — Ctrl+C to stop)")
            time.sleep(10)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
