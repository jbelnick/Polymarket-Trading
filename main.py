"""
Polymarket Trading Bot — Main Orchestrator

Launches all four agents as concurrent async tasks:
  1. Scanner  — scores markets → queue.json
  2. Brain    — evaluates queue → thesis.json
  3. Executor — consensus + Kelly → trades
  4. Exit Mon — volume + target + decay triggers

Usage:
    python main.py                    # run all agents
    python main.py --scan-only        # scanner only (read-only, no wallet needed)
    python main.py --analyze <csv>    # analyze wallet data from poly_data
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from config import DATA_DIR, LOG_FILE

logger = logging.getLogger("polymarket-bot")


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=handlers,
    )


async def run_all() -> None:
    """Launch all four agent loops concurrently."""
    from brain import brain_loop
    from executor import executor_loop, get_open_positions
    from exit_monitor import exit_monitor_loop
    from scanner import scan_loop

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info("=" * 60)
    logger.info("Polymarket Trading Bot starting — %s", now)
    logger.info("4 agents going live")
    logger.info("=" * 60)

    await asyncio.gather(
        scan_loop(),
        brain_loop(),
        executor_loop(),
        exit_monitor_loop(get_open_positions),
    )


async def run_scan_only() -> None:
    """Scanner only — read-only mode, no wallet needed."""
    from scanner import scan_loop

    logger.info("Starting scanner in read-only mode")
    await scan_loop()


def run_analyze(csv_path: str) -> None:
    """Analyze wallet data from poly_data."""
    from data_analyzer import analyze_wallets, save_targets

    targets = analyze_wallets(csv_path)
    save_targets(targets)

    print(f"\nFound {len(targets)} target wallets")
    if targets:
        print(f"\nTop 10 by PnL:")
        for i, t in enumerate(targets[:10], 1):
            print(
                f"  {i:2d}. {t.address[:12]}…  "
                f"trades={t.trades:>5d}  "
                f"win_rate={t.win_rate:.1%}  "
                f"pnl=${t.total_pnl:>12,.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Run scanner only (read-only, no wallet needed)",
    )
    parser.add_argument(
        "--analyze",
        metavar="CSV",
        help="Analyze wallet data from poly_data trades CSV",
    )
    args = parser.parse_args()

    setup_logging()

    if args.analyze:
        run_analyze(args.analyze)
    elif args.scan_only:
        asyncio.run(run_scan_only())
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
