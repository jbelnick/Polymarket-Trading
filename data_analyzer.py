"""
Kalshi Trading Bot — Data Analyzer (Optional)

Analyzes Kalshi trade history to identify patterns.
Unlike Polymarket, Kalshi doesn't have a public dataset of all trades —
this module works with your own fill history from the Kalshi API.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from config import DATA_DIR
from kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


def export_fill_history(client: KalshiClient, limit: int = 1000) -> list[dict]:
    """Pull your fill history from Kalshi and save to data/fills.json."""
    logger.info("Fetching fill history (limit=%d) …", limit)

    all_fills: list[dict] = []
    cursor = None

    while len(all_fills) < limit:
        batch_size = min(100, limit - len(all_fills))
        data = client.get_fills(limit=batch_size, cursor=cursor)
        fills = data.get("fills", [])
        if not fills:
            break
        all_fills.extend(fills)
        cursor = data.get("cursor")
        if not cursor:
            break

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "fills.json"
    path.write_text(json.dumps(all_fills, indent=2))
    logger.info("Saved %d fills to %s", len(all_fills), path)

    return all_fills


def compute_performance(fills: list[dict]) -> dict:
    """Compute aggregate performance from fill history."""
    if not fills:
        return {"total_fills": 0}

    total_cost = 0.0
    total_revenue = 0.0

    for fill in fills:
        price = float(fill.get("price", 0)) / 100  # cents → dollars
        count = int(fill.get("count", 0))
        action = fill.get("action", "")
        side = fill.get("side", "")

        cost = price * count
        if action == "buy":
            total_cost += cost
        elif action == "sell":
            total_revenue += cost

    return {
        "total_fills": len(fills),
        "total_cost": round(total_cost, 2),
        "total_revenue": round(total_revenue, 2),
        "realized_pnl": round(total_revenue - total_cost, 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    client = KalshiClient()
    fills = export_fill_history(client)
    stats = compute_performance(fills)

    print(f"\nPerformance Summary:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
