"""
One-shot scanner smoke test.

Runs a single cycle end-to-end: calls polymarket-cli, parses, prescreens,
and prints counts + the first 5 survivors. No loop, no Claude calls,
no order placement.

    python scan_test.py
"""

from __future__ import annotations

import logging

from scanner import fetch_markets, parse_market, prescreen_market


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    raw = fetch_markets(limit=500)
    parsed = [m for r in raw if (m := parse_market(r)) is not None]
    survivors = [m for m in parsed if prescreen_market(m)]

    print()
    print(f"  fetched:    {len(raw)}")
    print(f"  parsed:     {len(parsed)}")
    print(f"  prescreen:  {len(survivors)}")
    print()

    if not survivors:
        print("No markets passed the default thresholds.")
        print("Try loosening them in .env, e.g.:")
        print("  MIN_MARKET_VOLUME=5000")
        print("  MAX_HOURS=720")
        return

    print("First 5 survivors:")
    for m in survivors[:5]:
        q = m.question[:60]
        print(f"  {q!r}")
        print(
            f"    mid={m.midpoint:.3f}  "
            f"vol24=${m.volume_24h:,.0f}  "
            f"depth=${m.bids_depth:,.0f}  "
            f"hrs={m.hours_to_resolution:.0f}  "
            f"cat={m.category or '—'}"
        )


if __name__ == "__main__":
    main()
