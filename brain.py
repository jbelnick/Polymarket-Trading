"""
Polymarket Trading Bot — Step 2: The Brain

For every market in the scan queue, Claude runs 4 checks:
  1. Base rate — what does historical data say?
  2. News — has anything changed in the last 6 h?
  3. Whale check — are any of the 47 targets in this market?
  4. Disposition — is the crowd making a cognitive error?

If 3/4 agree → generate thesis.
If thesis confidence > 75% → size with Kelly.
If Kelly says overbet → cut to quarter-Kelly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    BANKROLL,
    BRAIN_INTERVAL_SEC,
    CLAUDE_MODEL,
    DATA_DIR,
    MAX_KELLY_FRACTION,
    MIN_CHECKS_PASSING,
    MIN_KELLY_FRACTION,
    MIN_THESIS_CONFIDENCE,
    TARGETS_FILE,
    THESIS_FILE,
)
from data_analyzer import load_targets
from models import Market, Side, Thesis

logger = logging.getLogger(__name__)


# ── Kelly criterion ────────────────────────────────────────────────────────────


def kelly_size(
    p_win: float,
    market_price: float,
    bankroll: float = BANKROLL,
    max_fraction: float = MAX_KELLY_FRACTION,
) -> float:
    """
    Full Kelly:  f* = (p × b − q) / b
      p = estimated probability of winning
      b = payout ratio = (1 / price) − 1
      q = 1 − p

    Capped at quarter-Kelly to limit drawdowns.
    Returns 0 for negative-EV or dust-sized bets.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    b = (1 / market_price) - 1
    q = 1 - p_win
    f_star = (p_win * b - q) / b

    if f_star <= 0:
        return 0.0  # negative EV — kill trade

    f_capped = min(f_star, max_fraction)

    if f_capped < MIN_KELLY_FRACTION:
        return 0.0  # skip dust

    return round(bankroll * f_capped, 2)


# ── Claude analysis ───────────────────────────────────────────────────────────


def _build_analysis_prompt(market: dict, target_addresses: list[str]) -> str:
    """Build the 4-check analysis prompt for Claude."""
    return f"""You are an expert prediction market analyst. Analyze this Polymarket market
and provide your assessment.

MARKET:
  Question: {market['question']}
  Current price (implied probability): {market['midpoint']}
  Category: {market.get('category', 'unknown')}
  Hours to resolution: {market.get('hours_to_resolution', 'unknown')}
  24h volume: ${market.get('volume_24h', 0):,.0f}
  Order book depth (bids): ${market.get('bids_depth', 0):,.0f}
  Order book depth (asks): ${market.get('asks_depth', 0):,.0f}

Run these 4 checks:

1. BASE RATE: What does historical data and base rates suggest for this outcome?
   Consider similar past events, polling data, statistical models.

2. NEWS: Has anything material changed in the last 6 hours that shifts probability?
   Consider breaking news, official statements, data releases.

3. WHALE CHECK: Are sophisticated traders (known profitable wallets) active in this market?
   Known target wallets in our database: {len(target_addresses)} wallets tracked.

4. DISPOSITION: Is the crowd making a cognitive error?
   Consider: anchoring bias, recency bias, narrative bias, availability heuristic,
   overreaction to news, underreaction to base rates.

For each check, output:
- SIGNAL: BULLISH / BEARISH / NEUTRAL
- REASONING: 1-2 sentences

Then provide your OVERALL ASSESSMENT:
- PROBABILITY: Your estimated true probability (0.00 to 1.00)
- CONFIDENCE: How confident you are in this estimate (0.00 to 1.00)
- DIRECTION: BUY (you think probability is higher than market) or SELL (lower)
- CHECKS_PASSING: How many of the 4 checks agree with your direction (0-4)
- THESIS: 2-3 sentence explanation of your trade thesis

Respond ONLY with valid JSON in this exact format:
{{
  "base_rate": {{"signal": "...", "reasoning": "..."}},
  "news": {{"signal": "...", "reasoning": "..."}},
  "whale_check": {{"signal": "...", "reasoning": "..."}},
  "disposition": {{"signal": "...", "reasoning": "..."}},
  "probability": 0.XX,
  "confidence": 0.XX,
  "direction": "BUY" or "SELL",
  "checks_passing": N,
  "thesis": "..."
}}"""


async def analyze_market(
    client: anthropic.AsyncAnthropic,
    market: dict,
    target_addresses: list[str],
) -> Thesis | None:
    """
    Ask Claude to analyze a single market.  Returns a Thesis if actionable,
    None otherwise.
    """
    prompt = _build_analysis_prompt(market, target_addresses)

    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        analysis = json.loads(text)

        checks = analysis.get("checks_passing", 0)
        confidence = analysis.get("confidence", 0)

        if checks < MIN_CHECKS_PASSING:
            logger.info(
                "SKIP %s — only %d/%d checks passing",
                market["question"][:60],
                checks,
                4,
            )
            return None

        if confidence < MIN_THESIS_CONFIDENCE:
            logger.info(
                "SKIP %s — confidence %.0f%% < threshold",
                market["question"][:60],
                confidence * 100,
            )
            return None

        direction = Side.BUY if analysis["direction"] == "BUY" else Side.SELL

        return Thesis(
            market_id=market.get("condition_id", ""),
            question=market["question"],
            base_rate=analysis["probability"],
            news_signal=analysis["news"]["signal"],
            whale_present=analysis["whale_check"]["signal"] == "BULLISH",
            disposition_bias=analysis["disposition"]["reasoning"],
            confidence=confidence,
            direction=direction,
            checks_passing=checks,
            reasoning=analysis.get("thesis", ""),
        )

    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.warning("Failed to parse Claude response for %s: %s", market["question"][:40], exc)
        return None
    except anthropic.APIError as exc:
        logger.error("Claude API error: %s", exc)
        return None


# ── Thesis sizing ──────────────────────────────────────────────────────────────


def size_thesis(thesis: Thesis, market_price: float) -> float:
    """Run Kelly sizing on an accepted thesis. Returns position size in USD."""
    return kelly_size(
        p_win=thesis.base_rate if thesis.direction == Side.BUY else (1 - thesis.base_rate),
        market_price=market_price if thesis.direction == Side.BUY else (1 - market_price),
    )


# ── Persistence ────────────────────────────────────────────────────────────────


def save_theses(theses: list[dict], path: Path = THESIS_FILE) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(theses, indent=2))
    logger.info("Saved %d theses to %s", len(theses), path)


def load_theses(path: Path = THESIS_FILE) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


# ── Main loop ──────────────────────────────────────────────────────────────────


async def brain_loop() -> None:
    """Continuous brain loop. Reads markets.json, produces thesis.json."""
    logger.info("Brain starting — interval %ds", BRAIN_INTERVAL_SEC)

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    targets = load_targets()
    target_addrs = [t.address for t in targets]

    # Cap how many markets we evaluate per cycle. At ~10s per Claude call,
    # 30 markets = ~5 min, matching the scanner cycle. Evaluating 140+ would
    # take 20+ min and the next scan would land before the first thesis.json
    # ever got written.
    max_per_cycle = int(os.getenv("BRAIN_MAX_PER_CYCLE", "30"))
    # Skip SELL theses on markets priced below this — the 1000× liability-to-
    # premium ratio on tail shorts is a poor match for bankroll-preservation.
    min_sell_price = float(os.getenv("MIN_SELL_PRICE", "0.05"))

    while True:
        try:
            markets_path = DATA_DIR / "markets.json"
            if not markets_path.exists():
                logger.info("No markets.json yet — waiting for scanner")
                await asyncio.sleep(BRAIN_INTERVAL_SEC)
                continue

            markets = json.loads(markets_path.read_text())

            # Work highest-volume first so we don't waste API calls on thin markets.
            markets.sort(key=lambda m: float(m.get("volume_24h") or 0), reverse=True)
            markets = markets[:max_per_cycle]

            logger.info(
                "Evaluating top %d prescreened markets by volume this cycle",
                len(markets),
            )

            actionable: list[dict] = []

            for mkt in markets:
                thesis = await analyze_market(client, mkt, target_addrs)
                if thesis is None:
                    continue

                # Guard against tail shorts. Claude often claims "0%" on
                # improbable events, but probability=0 is almost never true,
                # and selling at 0.01 means collecting $0.01 premium against
                # a $1 liability — asymmetric losses if we're wrong.
                if (
                    thesis.direction == Side.SELL
                    and mkt["midpoint"] < min_sell_price
                ):
                    logger.info(
                        "SKIP %s — tail short filter (mid=%.3f < %.2f)",
                        mkt["question"][:60],
                        mkt["midpoint"],
                        min_sell_price,
                    )
                    continue

                position_size = size_thesis(thesis, mkt["midpoint"])
                if position_size <= 0:
                    logger.info(
                        "SKIP %s — Kelly says no bet (size=$0)",
                        mkt["question"][:60],
                    )
                    continue

                actionable.append(
                    {
                        "condition_id": mkt.get("condition_id", ""),
                        "token_id": mkt.get("token_id", ""),
                        "question": mkt["question"],
                        "midpoint": mkt["midpoint"],
                        "direction": thesis.direction.value,
                        "confidence": thesis.confidence,
                        "probability": thesis.base_rate,
                        "checks_passing": thesis.checks_passing,
                        "position_size": position_size,
                        "reasoning": thesis.reasoning,
                        "whale_present": thesis.whale_present,
                    }
                )
                # Persist each thesis as we find it so the executor can start
                # working immediately — don't wait for the full cycle to end.
                save_theses(actionable)
                logger.info(
                    "THESIS %s %s $%.2f conf=%.0f%% — %s",
                    thesis.direction.value,
                    ("BUY" if thesis.direction == Side.BUY else "SELL"),
                    position_size,
                    thesis.confidence * 100,
                    mkt["question"][:60],
                )

            logger.info("%d actionable theses this cycle", len(actionable))

        except Exception:
            logger.exception("Brain cycle failed")

        await asyncio.sleep(BRAIN_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(brain_loop())
