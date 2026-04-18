"""
Kalshi Trading Bot — Data models

Dataclasses shared across all modules.
Kalshi terminology: events → series, markets → contracts with tickers.
Prices are in cents (1–99) internally, converted to dollars (0.01–0.99) at the edges.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class ExitReason(str, Enum):
    TARGET_HIT = "TARGET_HIT"
    VOLUME_EXIT = "VOLUME_EXIT"
    STALE_THESIS = "STALE_THESIS"
    MANUAL = "MANUAL"


class AgentType(str, Enum):
    ARBITRAGE = "arbitrage"
    CONVERGENCE = "convergence"
    VOLUME_PROFILE = "volume_profile"


# ── Market data ────────────────────────────────────────────────────────────────


@dataclass
class Market:
    """Snapshot of a Kalshi market (contract)."""

    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    yes_price: float         # in dollars (0.01–0.99)
    no_price: float          # in dollars (0.01–0.99)
    yes_bid: float           # best bid for YES
    yes_ask: float           # best ask for YES
    volume: int              # total contracts traded
    volume_24h: int          # last 24h
    open_interest: int       # contracts outstanding
    hours_to_resolution: float
    category: str = ""
    status: str = "open"


@dataclass
class ScoredMarket:
    """Market that passed the scanner filter."""

    market: Market
    gap: float               # |claude_estimate - yes_price|
    depth_dollars: float     # estimated fill capacity
    hours: float
    ev: float                # gap × depth × 0.001
    claude_estimate: float = 0.0


# ── Trade lifecycle ───────────────────────────────────────────────────────────


@dataclass
class Thesis:
    """Claude's analysis output for a market."""

    ticker: str
    title: str
    base_rate: float
    news_signal: str
    volume_signal: bool
    disposition_bias: str
    confidence: float
    action: Action           # BUY or SELL
    side: Side               # YES or NO
    checks_passing: int
    reasoning: str = ""


@dataclass
class AgentVote:
    """Single agent's evaluation of a market."""

    agent: AgentType
    action: Action
    side: Side
    confidence: float
    reasoning: str = ""


@dataclass
class Position:
    """An open position tracked by the bot."""

    ticker: str
    title: str
    action: Action
    side: Side
    entry_price: float              # dollars (0.01–0.99)
    count: int                      # number of contracts
    expected_gap: float             # mispricing the bot is capturing
    entry_time: float = field(default_factory=time.time)
    order_id: Optional[str] = None
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

    @property
    def notional(self) -> float:
        return self.entry_price * self.count
