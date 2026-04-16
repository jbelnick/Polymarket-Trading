"""
Polymarket Trading Bot — Data models

Dataclasses shared across all modules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, Enum):
    TARGET_HIT = "TARGET_HIT"
    VOLUME_EXIT = "VOLUME_EXIT"
    STALE_THESIS = "STALE_THESIS"
    MANUAL = "MANUAL"


class AgentType(str, Enum):
    ARBITRAGE = "arbitrage"
    CONVERGENCE = "convergence"
    WHALE_COPY = "whale_copy"


# ── Market data ────────────────────────────────────────────────────────────────


@dataclass
class Market:
    """Snapshot of a Polymarket market."""

    condition_id: str
    question: str
    token_id: str
    midpoint: float
    bids_depth: float
    asks_depth: float
    volume_24h: float
    hours_to_resolution: float
    category: str = ""
    outcomes: list[str] = field(default_factory=list)


@dataclass
class ScoredMarket:
    """Market that passed the scanner filter."""

    market: Market
    gap: float            # |claude_estimate - midpoint|
    depth: float          # min(bids, asks)
    hours: float
    ev: float             # gap × depth × 0.001
    claude_estimate: float = 0.0


# ── Wallet / target tracking ──────────────────────────────────────────────────


@dataclass
class TargetWallet:
    """A profitable wallet identified from poly_data."""

    address: str
    trades: int
    win_rate: float
    total_pnl: float
    avg_hold_hours: float = 0.0
    last_seen: float = 0.0


# ── Trade lifecycle ───────────────────────────────────────────────────────────


@dataclass
class Thesis:
    """Claude's analysis output for a market."""

    market_id: str
    question: str
    base_rate: float
    news_signal: str
    whale_present: bool
    disposition_bias: str
    confidence: float
    direction: Side
    checks_passing: int
    reasoning: str = ""


@dataclass
class AgentVote:
    """Single agent's evaluation of a market."""

    agent: AgentType
    action: Side
    confidence: float
    reasoning: str = ""


@dataclass
class Position:
    """An open position tracked by the bot."""

    market_id: str
    token_id: str
    question: str
    side: Side
    entry_price: float
    size: float                       # USD notional
    expected_gap: float               # mispricing the bot is capturing
    entry_time: float = field(default_factory=time.time)
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    pnl: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def hours_held(self) -> float:
        end = self.exit_time or time.time()
        return (end - self.entry_time) / 3600
