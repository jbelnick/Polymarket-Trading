"""
Polymarket Trading Bot — Step 0: Data Analyzer

Analyzes historical trade data from poly_data to identify top-performing wallets.
Input:  processed/trades.csv (from github.com/warproxxx/poly_data)
Output: data/targets.json — ranked list of profitable wallets to track.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import polars as pl

from config import DATA_DIR, TARGETS_FILE
from models import TargetWallet

logger = logging.getLogger(__name__)

# ── Defaults ───────────────────────────────────────────────────────────────────
DEFAULT_MIN_TRADES = 100
DEFAULT_MIN_WIN_RATE = 0.70
DEFAULT_TOP_N = 50


def analyze_wallets(
    trades_csv: str | Path,
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    top_n: int = DEFAULT_TOP_N,
) -> list[TargetWallet]:
    """
    Scan every wallet in the trade history.  Return the *top_n* by total PnL
    that also clear the minimum trade-count and win-rate filters.
    """
    logger.info("Loading trades from %s …", trades_csv)

    df = pl.scan_csv(str(trades_csv)).collect(streaming=True)

    logger.info("Loaded %s rows. Grouping by wallet …", f"{len(df):,}")

    wallets = (
        df.group_by("maker")
        .agg(
            [
                pl.count().alias("trades"),
                (pl.col("profit") > 0).mean().alias("win_rate"),
                pl.col("profit").sum().alias("total_pnl"),
            ]
        )
        .filter(
            (pl.col("trades") >= min_trades) & (pl.col("win_rate") > min_win_rate)
        )
        .sort("total_pnl", descending=True)
        .head(top_n)
    )

    targets: list[TargetWallet] = []
    for row in wallets.iter_rows(named=True):
        targets.append(
            TargetWallet(
                address=row["maker"],
                trades=row["trades"],
                win_rate=round(row["win_rate"], 4),
                total_pnl=round(row["total_pnl"], 2),
            )
        )

    logger.info(
        "Found %d wallets with %d+ trades and >%.0f%% win rate",
        len(targets),
        min_trades,
        min_win_rate * 100,
    )

    return targets


def analyze_exit_behavior(
    trades_csv: str | Path,
    target_addresses: list[str],
) -> dict:
    """
    Study how the target wallets exit positions.
    Returns aggregate stats: hold-to-settlement %, avg profit captured, exit triggers.
    """
    logger.info("Analyzing exit behavior for %d target wallets …", len(target_addresses))

    df = pl.scan_csv(str(trades_csv)).collect(streaming=True)

    target_trades = df.filter(pl.col("maker").is_in(target_addresses))

    if target_trades.is_empty():
        logger.warning("No trades found for target wallets.")
        return {}

    total_exits = len(target_trades)

    # Trades that resolved at 0 or 1 are settlement exits
    settlement_exits = target_trades.filter(
        (pl.col("exit_price") == 0.0) | (pl.col("exit_price") == 1.0)
    )
    early_exits = total_exits - len(settlement_exits)

    early_exit_pct = round(early_exits / total_exits, 4) if total_exits > 0 else 0.0

    # Average profit captured as fraction of max potential
    profit_captured = target_trades.select(
        (
            (pl.col("exit_price") - pl.col("entry_price")).abs()
            / (pl.col("max_potential_profit").abs() + 1e-9)
        ).alias("fraction_captured")
    )

    avg_captured = round(profit_captured["fraction_captured"].mean() or 0.0, 4)

    return {
        "total_exits": total_exits,
        "early_exit_pct": early_exit_pct,
        "avg_profit_captured": avg_captured,
        "settlement_exit_pct": round(1 - early_exit_pct, 4),
    }


def save_targets(targets: list[TargetWallet], path: Path = TARGETS_FILE) -> None:
    """Persist target wallets to JSON."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "address": t.address,
            "trades": t.trades,
            "win_rate": t.win_rate,
            "total_pnl": t.total_pnl,
        }
        for t in targets
    ]
    path.write_text(json.dumps(payload, indent=2))
    logger.info("Saved %d targets to %s", len(targets), path)


def load_targets(path: Path = TARGETS_FILE) -> list[TargetWallet]:
    """Load previously saved target wallets."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [
        TargetWallet(
            address=d["address"],
            trades=d["trades"],
            win_rate=d["win_rate"],
            total_pnl=d["total_pnl"],
        )
        for d in data
    ]


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    csv_path = sys.argv[1] if len(sys.argv) > 1 else "processed/trades.csv"
    targets = analyze_wallets(csv_path)
    save_targets(targets)

    if targets:
        print(f"\nTop 5 wallets by PnL:")
        for i, t in enumerate(targets[:5], 1):
            print(
                f"  {i}. {t.address[:10]}…  "
                f"trades={t.trades}  win_rate={t.win_rate:.1%}  pnl=${t.total_pnl:,.2f}"
            )
