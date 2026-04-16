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

from config import (
    BANKROLL,
    CONSENSUS_FULL,
    CONSENSUS_HALF,
    DATA_DIR,
    THESIS_FILE,
    TRADES_LOG,
    WHALE_COPY_DELAY_SEC,
)
from data_analyzer import load_targets
from models import AgentType, AgentVote, Position, Side

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


# ── Order placement stub ──────────────────────────────────────────────────────


async def place_order(position: Position) -> bool:
    """
    Submit an order to the Polymarket CLOB.

    This is a stub — in production, wire this up to the polymarket-cli
    or the py-clob-client SDK:

        from py_clob_client.client import ClobClient
        client = ClobClient(host, key=API_KEY, ...)
        client.create_and_post_order(...)

    Returns True if order was accepted.
    """
    logger.info(
        "ORDER: %s %s $%.2f @ %.4f — %s",
        position.side.value,
        position.token_id[:12],
        position.size,
        position.entry_price,
        position.question[:60],
    )
    # TODO: Replace with real CLOB order submission
    # For now, log and return success
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


def get_open_positions() -> list[Position]:
    return [p for p in _open_positions if p.is_open]


# ── Main loop ──────────────────────────────────────────────────────────────────


async def executor_loop() -> None:
    """
    Continuous execution loop.
    Reads thesis.json, runs consensus, places orders.
    """
    logger.info("Executor starting")

    targets = load_targets()
    target_addrs = [t.address for t in targets]

    agents: list[BaseAgent] = [
        ArbitrageAgent(),
        ConvergenceAgent(),
        WhaleCopyAgent(target_addresses=target_addrs),
    ]

    already_traded: set[str] = set()

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
                    already_traded.add(market_id)
                    logger.info("TRADE PLACED: %s", thesis["question"][:70])

        except Exception:
            logger.exception("Executor cycle failed")

        await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(executor_loop())
