"""
One-shot scanner smoke test.

Runs a single cycle end-to-end: calls polymarket-cli, parses, prescreens,
and prints counts + a breakdown of which filters killed what, plus the
first 5 survivors. No loop, no Claude calls, no order placement.

    python scan_test.py
"""

from __future__ import annotations

import logging

from config import (
    DISABLED_CATEGORIES,
    MAX_HOURS_TO_RESOLUTION,
    MIN_BOOK_DEPTH,
    MIN_HOURS_TO_RESOLUTION,
    MIN_MARKET_VOLUME,
)
from scanner import fetch_markets, parse_market, prescreen_market


def filter_breakdown(markets: list) -> dict[str, int]:
    """Count how many markets fail each prescreen check, independently."""
    counts = {
        "depth":    sum(1 for m in markets if min(m.bids_depth, m.asks_depth) < MIN_BOOK_DEPTH),
        "too_late": sum(1 for m in markets if m.hours_to_resolution < MIN_HOURS_TO_RESOLUTION),
        "too_slow": sum(1 for m in markets if m.hours_to_resolution > MAX_HOURS_TO_RESOLUTION),
        "volume":   sum(1 for m in markets if m.volume_24h < MIN_MARKET_VOLUME),
        "category": sum(1 for m in markets if m.category in DISABLED_CATEGORIES),
    }
    return counts


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

    print("Filter thresholds (from config / .env):")
    print(f"  MIN_BOOK_DEPTH           = ${MIN_BOOK_DEPTH:,.0f}  (per side)")
    print(f"  MIN_HOURS_TO_RESOLUTION  = {MIN_HOURS_TO_RESOLUTION:.0f}h")
    print(f"  MAX_HOURS_TO_RESOLUTION  = {MAX_HOURS_TO_RESOLUTION:.0f}h")
    print(f"  MIN_MARKET_VOLUME        = ${MIN_MARKET_VOLUME:,.0f}  (24h)")
    print(f"  DISABLED_CATEGORIES      = {sorted(DISABLED_CATEGORIES)}")
    print()

    bd = filter_breakdown(parsed)
    print("How many of the 500 fail each filter (not mutually exclusive):")
    for name, count in bd.items():
        pct = (count / len(parsed) * 100) if parsed else 0
        print(f"  {name:10s} {count:4d}  ({pct:.0f}%)")
    print()

    # Sanity stats on the full parsed set
    if parsed:
        vols = sorted((m.volume_24h for m in parsed), reverse=True)
        hrs = sorted(m.hours_to_resolution for m in parsed)
        depths = sorted((min(m.bids_depth, m.asks_depth) for m in parsed), reverse=True)
        print("Distribution of the 500 parsed markets:")
        print(f"  volume_24h  max={vols[0]:,.0f}  p50={vols[len(vols)//2]:,.0f}  p90={vols[len(vols)//10]:,.0f}")
        print(f"  depth       max={depths[0]:,.0f}  p50={depths[len(depths)//2]:,.0f}  p90={depths[len(depths)//10]:,.0f}")
        print(f"  hours       min={hrs[0]:.0f}  p50={hrs[len(hrs)//2]:.0f}  max={hrs[-1]:.0f}")
        print()

    if not survivors:
        print("No markets passed. Loosen thresholds in .env — see suggestions below.")
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
