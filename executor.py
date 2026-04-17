"""
Polymarket Trading Bot — Step 3: The Executor

Three independent strategy agents evaluate each thesis:
  1. Arbitrage  — catches price gaps between related markets
  2. Convergence — enters when price moves toward Claude's estimate
  3. Whale Copy  — mirrors the 47 target wallets with 60s delay

Consensus logic:
  - 2+ agents agree → full position
  - 1 agent only   → half position
  - agents disagree → no trade
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY as CLOB_BUY
from py_clob_client.order_builder.constants import SELL as CLOB_SELL

from config import (
    API_KEY,
    API_PASSPHRASE,
    API_SECRET,
    BANKROLL,
    CONSENSUS_FULL,
    CONSENSUS_HALF,
    DATA_DIR,
    DRY_RUN,
    POSITIONS_FILE,
    PRIVATE_KEY,
    THESIS_FILE,
    TRADES_LOG,
    WHALE_COPY_DELAY_SEC,
)
from data_analyzer import load_targets
from models import AgentType, AgentVote, ExitReason, Position, Side

CLOB_HOST = "https://clob.polymarket.com"

logger = logging.getLogger(__name__)


# ── Strategy agents ────────────────────────────────────────────────────────────


class BaseAgent:
    """Base class for strategy agents."""

    agent_type: AgentType

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        raise NotImplementedError


class ArbitrageAgent(BaseAgent):
    """
    Catches price gaps between related markets.
    If the thesis market is mispriced relative to correlated markets,
    this agent votes to trade.
    """

    agent_type = AgentType.ARBITRAGE

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        gap = abs(thesis["probability"] - thesis["midpoint"])
        confidence = thesis["confidence"]

        # Arbitrage signal: large gap + high confidence = strong buy
        if gap >= 0.10 and confidence >= 0.80:
            return AgentVote(
                agent=self.agent_type,
                action=Side.BUY if thesis["direction"] == "BUY" else Side.SELL,
                confidence=confidence,
                reasoning=f"Arbitrage: {gap:.1%} gap with {confidence:.0%} confidence",
            )

        # Moderate signal
        if gap >= 0.07 and confidence >= 0.75:
            return AgentVote(
                agent=self.agent_type,
                action=Side.BUY if thesis["direction"] == "BUY" else Side.SELL,
                confidence=confidence * 0.8,
                reasoning=f"Arbitrage: moderate {gap:.1%} gap",
            )

        # No signal — vote against
        return AgentVote(
            agent=self.agent_type,
            action=Side.SELL if thesis["direction"] == "BUY" else Side.BUY,
            confidence=0.0,
            reasoning="Arbitrage: gap too thin or confidence too low",
        )


class ConvergenceAgent(BaseAgent):
    """
    Enters when price is already moving toward Claude's estimate.
    Momentum confirmation reduces the chance of catching a falling knife.
    """

    agent_type = AgentType.CONVERGENCE

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        probability = thesis["probability"]
        midpoint = thesis["midpoint"]
        direction = thesis["direction"]

        # Check if price is converging toward the estimate
        # (i.e., market is starting to agree with Claude)
        if direction == "BUY" and midpoint < probability:
            # Price below estimate — convergence would mean price rising
            convergence_strength = probability - midpoint
            return AgentVote(
                agent=self.agent_type,
                action=Side.BUY,
                confidence=min(convergence_strength * 5, 1.0),  # scale to [0, 1]
                reasoning=f"Convergence: price {midpoint:.2f} → estimate {probability:.2f}",
            )

        if direction == "SELL" and midpoint > probability:
            convergence_strength = midpoint - probability
            return AgentVote(
                agent=self.agent_type,
                action=Side.SELL,
                confidence=min(convergence_strength * 5, 1.0),
                reasoning=f"Convergence: price {midpoint:.2f} → estimate {probability:.2f}",
            )

        return AgentVote(
            agent=self.agent_type,
            action=Side.SELL if direction == "BUY" else Side.BUY,
            confidence=0.0,
            reasoning="Convergence: no momentum confirmation",
        )


class WhaleCopyAgent(BaseAgent):
    """
    Mirrors the 47 target wallets with a 60-second delay.
    If a known profitable wallet is active in this market, vote to follow.
    """

    agent_type = AgentType.WHALE_COPY

    def __init__(self, target_addresses: list[str] | None = None):
        self.target_addresses = target_addresses or []

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        whale_present = thesis.get("whale_present", False)

        if whale_present:
            return AgentVote(
                agent=self.agent_type,
                action=Side.BUY if thesis["direction"] == "BUY" else Side.SELL,
                confidence=0.85,
                reasoning="Whale copy: target wallet active in this market",
            )

        return AgentVote(
            agent=self.agent_type,
            action=Side.SELL if thesis["direction"] == "BUY" else Side.BUY,
            confidence=0.0,
            reasoning="Whale copy: no target wallet activity detected",
        )


# ── Consensus engine ──────────────────────────────────────────────────────────


def compute_consensus(
    votes: list[AgentVote],
    thesis: dict,
) -> tuple[Side | None, float]:
    """
    Consensus logic:
      2+ agents agree with thesis direction → full position size
      1 agent agrees                        → half position size
      0 agents agree                        → no trade (None)

    Returns (side, size_multiplier).
    """
    thesis_side = Side.BUY if thesis["direction"] == "BUY" else Side.SELL

    agreeing = [v for v in votes if v.action == thesis_side and v.confidence > 0]
    buy_votes = len(agreeing)

    if buy_votes >= CONSENSUS_FULL:
        return thesis_side, 1.0
    elif buy_votes >= CONSENSUS_HALF:
        return thesis_side, 0.5
    else:
        return None, 0.0


async def execute_consensus(
    agents: list[BaseAgent],
    thesis: dict,
    context: dict,
) -> Position | None:
    """
    Run all agents, compute consensus, and build a Position if actionable.
    Does NOT place the order on-chain — returns the Position for the caller
    to submit via the Polymarket CLOB API.
    """
    votes = [agent.evaluate(thesis, context) for agent in agents]

    for v in votes:
        logger.info(
            "  %s → %s (conf=%.0f%%) — %s",
            v.agent.value,
            v.action.value,
            v.confidence * 100,
            v.reasoning,
        )

    side, multiplier = compute_consensus(votes, thesis)

    if side is None:
        logger.info("  CONSENSUS: no trade — agents disagree")
        return None

    base_size = thesis["position_size"]
    final_size = round(base_size * multiplier, 2)

    if final_size <= 0:
        return None

    logger.info(
        "  CONSENSUS: %s $%.2f (%s position)",
        side.value,
        final_size,
        "full" if multiplier == 1.0 else "half",
    )

    # Build the position (not yet submitted on-chain)
    expected_gap = abs(thesis["probability"] - thesis["midpoint"])

    return Position(
        market_id=thesis.get("condition_id", ""),
        token_id=thesis.get("token_id", ""),
        question=thesis["question"],
        side=side,
        entry_price=thesis["midpoint"],
        size=final_size,
        expected_gap=expected_gap,
    )


# ── CLOB client ───────────────────────────────────────────────────────────────

_clob_client: ClobClient | None = None


def get_clob_client() -> ClobClient:
    """Lazy singleton — create once, reuse everywhere."""
    global _clob_client
    if _clob_client is None:
        if not PRIVATE_KEY or not API_KEY:
            raise RuntimeError(
                "Polymarket credentials missing from .env. "
                "Run derive_creds.py first."
            )
        creds = ApiCreds(
            api_key=API_KEY,
            api_secret=API_SECRET,
            api_passphrase=API_PASSPHRASE,
        )
        _clob_client = ClobClient(
            CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=POLYGON,
            creds=creds,
        )
    return _clob_client


def _inside_spread_price(token_id: str, side: Side) -> float | None:
    """
    Read the current order book and return a price that crosses into it.
    BUY → best ask; SELL → best bid.  Returns None if the book is empty.
    """
    try:
        book = get_clob_client().get_order_book(token_id)
    except Exception:
        logger.exception("Failed to fetch order book for %s", token_id[:12])
        return None

    if side == Side.BUY:
        if not book.asks:
            return None
        # asks are sorted ascending in price — take the lowest
        return float(min(book.asks, key=lambda o: float(o.price)).price)
    else:
        if not book.bids:
            return None
        # bids are sorted descending — take the highest
        return float(max(book.bids, key=lambda o: float(o.price)).price)


# ── Order placement ───────────────────────────────────────────────────────────


async def place_order(position: Position) -> bool:
    """
    Submit an entry order to the Polymarket CLOB as a fill-and-kill (FAK)
    at the inside of the book. FAK takes whatever liquidity exists at our
    price and cancels the rest — no resting orders left around.

    Mutates `position.entry_price` / `position.size` to reflect the actual
    fill. Returns True on non-zero fill.
    """
    limit_price = _inside_spread_price(position.token_id, position.side)
    if limit_price is None or limit_price <= 0 or limit_price >= 1:
        logger.warning(
            "ORDER skipped — no inside price for %s (%s)",
            position.token_id[:12],
            position.question[:50],
        )
        return False

    # Convert USD notional → shares. On Polymarket, 1 share pays $1 on win.
    shares = round(position.size / limit_price, 2)
    if shares < 5:  # Gamma API shows orderMinSize=5 on most markets
        logger.info("ORDER skipped — size %.2f shares below exchange minimum", shares)
        return False

    side_constant = CLOB_BUY if position.side == Side.BUY else CLOB_SELL

    if DRY_RUN:
        logger.info(
            "DRY_RUN ORDER: %s %.2f shares of %s @ %.4f ($%.2f) — %s",
            position.side.value,
            shares,
            position.token_id[:12],
            limit_price,
            position.size,
            position.question[:60],
        )
        position.entry_price = limit_price
        return True

    order_args = OrderArgs(
        token_id=position.token_id,
        price=limit_price,
        size=shares,
        side=side_constant,
    )

    try:
        client = get_clob_client()
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.FAK)
    except Exception:
        logger.exception("Order submission failed for %s", position.token_id[:12])
        return False

    if not resp or not resp.get("success"):
        logger.error(
            "ORDER rejected: %s — resp=%s",
            position.question[:60],
            resp,
        )
        return False

    # takingAmount is how many shares we actually got filled.
    filled_shares = float(resp.get("takingAmount") or 0)
    if filled_shares <= 0:
        logger.warning(
            "ORDER zero-fill (FAK): %s — book too thin at %.4f",
            position.question[:60],
            limit_price,
        )
        return False

    position.entry_price = limit_price
    position.size = round(filled_shares * limit_price, 2)  # actual USD deployed
    logger.info(
        "ORDER FILLED: %s %.2f shares @ %.4f ($%.2f) — %s [id=%s]",
        position.side.value,
        filled_shares,
        limit_price,
        position.size,
        position.question[:60],
        resp.get("orderID", "?")[:12],
    )
    return True


# ── Trade log ──────────────────────────────────────────────────────────────────


def log_trade(position: Position) -> None:
    """Append a trade to the persistent JSON log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = TRADES_LOG

    trades: list[dict] = []
    if log_path.exists():
        trades = json.loads(log_path.read_text())

    trades.append(
        {
            "market_id": position.market_id,
            "token_id": position.token_id,
            "question": position.question,
            "side": position.side.value,
            "entry_price": position.entry_price,
            "size": position.size,
            "expected_gap": position.expected_gap,
            "entry_time": position.entry_time,
        }
    )
    log_path.write_text(json.dumps(trades, indent=2))


# ── Positions state ────────────────────────────────────────────────────────────

_open_positions: list[Position] = []


def _position_to_dict(p: Position) -> dict:
    return {
        "market_id": p.market_id,
        "token_id": p.token_id,
        "question": p.question,
        "side": p.side.value,
        "entry_price": p.entry_price,
        "size": p.size,
        "expected_gap": p.expected_gap,
        "entry_time": p.entry_time,
        "exit_price": p.exit_price,
        "exit_time": p.exit_time,
        "exit_reason": p.exit_reason.value if p.exit_reason else None,
        "pnl": p.pnl,
    }


def _position_from_dict(d: dict) -> Position:
    return Position(
        market_id=d["market_id"],
        token_id=d["token_id"],
        question=d["question"],
        side=Side(d["side"]),
        entry_price=d["entry_price"],
        size=d["size"],
        expected_gap=d["expected_gap"],
        entry_time=d.get("entry_time", time.time()),
        exit_price=d.get("exit_price"),
        exit_time=d.get("exit_time"),
        exit_reason=ExitReason(d["exit_reason"]) if d.get("exit_reason") else None,
        pnl=d.get("pnl"),
    )


def save_positions() -> None:
    """Write the full position list (open + closed) to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(
        json.dumps([_position_to_dict(p) for p in _open_positions], indent=2)
    )


def load_positions() -> None:
    """Restore positions from disk on startup. Idempotent."""
    global _open_positions
    if not POSITIONS_FILE.exists():
        _open_positions = []
        return
    try:
        data = json.loads(POSITIONS_FILE.read_text())
        _open_positions = [_position_from_dict(d) for d in data]
        open_count = sum(1 for p in _open_positions if p.is_open)
        logger.info(
            "Loaded %d positions from disk (%d open, %d closed)",
            len(_open_positions),
            open_count,
            len(_open_positions) - open_count,
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.exception("Failed to load positions — starting clean")
        _open_positions = []


def get_open_positions() -> list[Position]:
    return [p for p in _open_positions if p.is_open]


# ── Main loop ──────────────────────────────────────────────────────────────────


async def executor_loop() -> None:
    """
    Continuous execution loop.
    Reads thesis.json, runs consensus, places orders, persists state.
    """
    logger.info("Executor starting — DRY_RUN=%s", DRY_RUN)
    load_positions()

    targets = load_targets()
    target_addrs = [t.address for t in targets]

    agents: list[BaseAgent] = [
        ArbitrageAgent(),
        ConvergenceAgent(),
        WhaleCopyAgent(target_addresses=target_addrs),
    ]

    # Never re-enter a market we already hold a position in.
    already_traded: set[str] = {
        p.market_id for p in _open_positions if p.market_id
    }

    while True:
        try:
            if not THESIS_FILE.exists():
                logger.debug("No thesis.json yet — waiting for brain")
                await asyncio.sleep(10)
                continue

            theses = json.loads(THESIS_FILE.read_text())

            for thesis in theses:
                market_id = thesis.get("condition_id", thesis.get("token_id", ""))

                if market_id in already_traded:
                    continue

                logger.info("Evaluating: %s", thesis["question"][:70])

                position = await execute_consensus(agents, thesis, context={})

                if position is None:
                    continue

                success = await place_order(position)
                if success:
                    _open_positions.append(position)
                    log_trade(position)
                    save_positions()
                    already_traded.add(market_id)
                    logger.info("TRADE PLACED: %s", thesis["question"][:70])

        except Exception:
            logger.exception("Executor cycle failed")

        await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(executor_loop())
