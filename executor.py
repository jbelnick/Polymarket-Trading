"""
Kalshi Trading Bot — Step 3: The Executor

Three independent strategy agents evaluate each thesis:
  1. Arbitrage  — catches price gaps between related markets
  2. Convergence — enters when price moves toward Claude's estimate
  3. Volume Profile — confirms via volume/OI patterns

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
)
from kalshi_client import KalshiClient
from models import Action, AgentType, AgentVote, Position, Side

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
    If the thesis market is mispriced relative to Claude's estimate,
    this agent votes to trade.
    """

    agent_type = AgentType.ARBITRAGE

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        gap = abs(thesis["probability"] - thesis["yes_price"])
        confidence = thesis["confidence"]

        if gap >= 0.10 and confidence >= 0.80:
            return AgentVote(
                agent=self.agent_type,
                action=Action(thesis["action"]),
                side=Side(thesis["side"]),
                confidence=confidence,
                reasoning=f"Arbitrage: {gap:.1%} gap with {confidence:.0%} confidence",
            )

        if gap >= 0.07 and confidence >= 0.75:
            return AgentVote(
                agent=self.agent_type,
                action=Action(thesis["action"]),
                side=Side(thesis["side"]),
                confidence=confidence * 0.8,
                reasoning=f"Arbitrage: moderate {gap:.1%} gap",
            )

        return AgentVote(
            agent=self.agent_type,
            action=Action.SELL if thesis["action"] == "buy" else Action.BUY,
            side=Side(thesis["side"]),
            confidence=0.0,
            reasoning="Arbitrage: gap too thin or confidence too low",
        )


class ConvergenceAgent(BaseAgent):
    """
    Enters when price is already moving toward Claude's estimate.
    Momentum confirmation reduces false signals.
    """

    agent_type = AgentType.CONVERGENCE

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        probability = thesis["probability"]
        yes_price = thesis["yes_price"]

        if thesis["side"] == "yes" and thesis["action"] == "buy":
            if yes_price < probability:
                strength = probability - yes_price
                return AgentVote(
                    agent=self.agent_type,
                    action=Action.BUY,
                    side=Side.YES,
                    confidence=min(strength * 5, 1.0),
                    reasoning=f"Convergence: YES price {yes_price:.2f} → estimate {probability:.2f}",
                )

        if thesis["side"] == "no" and thesis["action"] == "buy":
            no_price = 1 - yes_price
            no_estimate = 1 - probability
            if no_price < no_estimate:
                strength = no_estimate - no_price
                return AgentVote(
                    agent=self.agent_type,
                    action=Action.BUY,
                    side=Side.NO,
                    confidence=min(strength * 5, 1.0),
                    reasoning=f"Convergence: NO price {no_price:.2f} → estimate {no_estimate:.2f}",
                )

        return AgentVote(
            agent=self.agent_type,
            action=Action.SELL if thesis["action"] == "buy" else Action.BUY,
            side=Side(thesis["side"]),
            confidence=0.0,
            reasoning="Convergence: no momentum confirmation",
        )


class VolumeProfileAgent(BaseAgent):
    """
    Confirms via volume and open interest signals.
    High volume + increasing OI on the thesis side = smart money accumulating.
    """

    agent_type = AgentType.VOLUME_PROFILE

    def evaluate(self, thesis: dict, context: dict) -> AgentVote:
        volume_signal = thesis.get("volume_signal", False)

        if volume_signal:
            return AgentVote(
                agent=self.agent_type,
                action=Action(thesis["action"]),
                side=Side(thesis["side"]),
                confidence=0.80,
                reasoning="Volume: smart money signal detected",
            )

        return AgentVote(
            agent=self.agent_type,
            action=Action.SELL if thesis["action"] == "buy" else Action.BUY,
            side=Side(thesis["side"]),
            confidence=0.0,
            reasoning="Volume: no accumulation signal",
        )


# ── Consensus engine ──────────────────────────────────────────────────────────


def compute_consensus(
    votes: list[AgentVote],
    thesis: dict,
) -> tuple[Action | None, Side | None, float]:
    """
    Consensus logic:
      2+ agents agree with thesis → full position size
      1 agent agrees              → half position size
      0 agents agree              → no trade (None)

    Returns (action, side, size_multiplier).
    """
    thesis_action = Action(thesis["action"])
    thesis_side = Side(thesis["side"])

    agreeing = [
        v for v in votes
        if v.action == thesis_action and v.side == thesis_side and v.confidence > 0
    ]

    if len(agreeing) >= CONSENSUS_FULL:
        return thesis_action, thesis_side, 1.0
    elif len(agreeing) >= CONSENSUS_HALF:
        return thesis_action, thesis_side, 0.5
    else:
        return None, None, 0.0


async def execute_consensus(
    agents: list[BaseAgent],
    thesis: dict,
    context: dict,
) -> Position | None:
    """
    Run all agents, compute consensus, and build a Position if actionable.
    """
    votes = [agent.evaluate(thesis, context) for agent in agents]

    for v in votes:
        logger.info(
            "  %s → %s %s (conf=%.0f%%) — %s",
            v.agent.value,
            v.action.value,
            v.side.value,
            v.confidence * 100,
            v.reasoning,
        )

    action, side, multiplier = compute_consensus(votes, thesis)

    if action is None:
        logger.info("  CONSENSUS: no trade — agents disagree")
        return None

    base_contracts = thesis["contract_count"]
    final_contracts = max(1, int(base_contracts * multiplier))
    price = thesis["price_cents"] / 100

    logger.info(
        "  CONSENSUS: %s %s %d contracts @ $%.2f (%s position)",
        action.value.upper(),
        side.value.upper(),
        final_contracts,
        price,
        "full" if multiplier == 1.0 else "half",
    )

    expected_gap = abs(thesis["probability"] - thesis["yes_price"])

    return Position(
        ticker=thesis["ticker"],
        title=thesis["title"],
        action=action,
        side=side,
        entry_price=price,
        count=final_contracts,
        expected_gap=expected_gap,
    )


# ── Order placement (LIVE — Kalshi CLOB) ──────────────────────────────────────


async def place_order(client: KalshiClient, position: Position) -> bool:
    """
    Submit an order to the Kalshi exchange.
    Returns True if the order was accepted.
    """
    try:
        price_cents = int(position.entry_price * 100)

        result = client.place_order(
            ticker=position.ticker,
            action=position.action.value,
            side=position.side.value,
            count=position.count,
            type="limit",
            yes_price=price_cents if position.side == Side.YES else None,
            no_price=price_cents if position.side == Side.NO else None,
        )

        order = result.get("order", result)
        position.order_id = order.get("order_id", "")

        logger.info(
            "ORDER PLACED: %s %s %s %d @ %dc — %s (id=%s)",
            position.action.value.upper(),
            position.side.value.upper(),
            position.ticker,
            position.count,
            price_cents,
            position.title[:50],
            position.order_id[:12] if position.order_id else "?",
        )
        return True

    except Exception as exc:
        logger.error("Order failed for %s: %s", position.ticker, exc)
        return False


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
            "ticker": position.ticker,
            "title": position.title,
            "action": position.action.value,
            "side": position.side.value,
            "entry_price": position.entry_price,
            "count": position.count,
            "expected_gap": position.expected_gap,
            "entry_time": position.entry_time,
            "order_id": position.order_id,
        }
    )
    log_path.write_text(json.dumps(trades, indent=2))


# ── Positions state ────────────────────────────────────────────────────────────

_open_positions: list[Position] = []


def get_open_positions() -> list[Position]:
    return [p for p in _open_positions if p.is_open]


# ── Main loop ──────────────────────────────────────────────────────────────────


async def executor_loop(client: KalshiClient | None = None) -> None:
    """
    Continuous execution loop.
    Reads thesis.json, runs consensus, places orders on Kalshi.
    """
    logger.info("Executor starting")

    if client is None:
        client = KalshiClient()

    agents: list[BaseAgent] = [
        ArbitrageAgent(),
        ConvergenceAgent(),
        VolumeProfileAgent(),
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
                ticker = thesis.get("ticker", "")
                if ticker in already_traded:
                    continue

                logger.info("Evaluating: %s", thesis["title"][:70])

                position = await execute_consensus(agents, thesis, context={})
                if position is None:
                    continue

                success = await place_order(client, position)
                if success:
                    _open_positions.append(position)
                    log_trade(position)
                    already_traded.add(ticker)
                    logger.info("TRADE PLACED: %s", thesis["title"][:70])

        except Exception:
            logger.exception("Executor cycle failed")

        await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(executor_loop())
