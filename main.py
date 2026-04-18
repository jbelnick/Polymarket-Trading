"""
Kalshi Trading Bot — Main Orchestrator

Launches all four agents as concurrent async tasks:
  1. Scanner  — scores markets → queue.json
  2. Brain    — evaluates queue → thesis.json
  3. Executor — consensus + Kelly → trades on Kalshi
  4. Exit Mon — volume + target + decay triggers

Usage:
    python main.py                    # run all agents
    python main.py --scan-only        # scanner only (read-only)
    python main.py --demo             # use Kalshi demo environment
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from config import DATA_DIR, KALSHI_ENV, LOG_FILE

logger = logging.getLogger("kalshi-bot")


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
    from kalshi_client import KalshiClient
    from scanner import scan_loop

    client = KalshiClient()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    env = KALSHI_ENV.upper()

    # Verify connection
    try:
        balance = client.get_balance_dollars()
        logger.info("Connected to Kalshi (%s) — balance: $%.2f", env, balance)
    except Exception as exc:
        logger.warning("Could not fetch balance: %s (continuing anyway)", exc)

    logger.info("=" * 60)
    logger.info("Kalshi Trading Bot starting — %s [%s]", now, env)
    logger.info("4 agents going live")
    logger.info("=" * 60)

    await asyncio.gather(
        scan_loop(client),
        brain_loop(),
        executor_loop(client),
        exit_monitor_loop(get_open_positions, client),
    )


async def run_scan_only() -> None:
    """Scanner only — read-only mode."""
    from kalshi_client import KalshiClient
    from scanner import scan_loop

    client = KalshiClient()
    logger.info("Starting scanner in read-only mode")
    await scan_loop(client)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi Trading Bot")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Run scanner only (read-only)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Force demo environment (overrides KALSHI_ENV)",
    )
    args = parser.parse_args()

    if args.demo:
        os.environ["KALSHI_ENV"] = "demo"

    setup_logging()

    if args.scan_only:
        asyncio.run(run_scan_only())
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
