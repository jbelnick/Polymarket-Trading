"""
Kalshi Trading Bot — Step 2: The Brain (Two-Tier)

Cost optimization: Haiku pre-screens → Sonnet deep-analyzes.

Tier 1 (Haiku ~$0.001/call):
  "Is this market worth analyzing? Yes/no + rough probability."
  Kills ~80% of markets before Sonnet sees them.

Tier 2 (Sonnet ~$0.01/call):
  Full 4-check analysis: base rate, news, volume/OI, disposition.
  Only runs on markets Haiku flagged as interesting.

Cache layer:
  If a market was analyzed <10 min ago and price moved <1%, skip it.
  Cuts API calls by 60-70% during quiet periods.

If 3/4 checks agree → generate thesis.
If thesis confidence > 75% → size with Kelly.
If Kelly says overbet → cut to quarter-Kelly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    BANKROLL,
    BRAIN_INTERVAL_SEC,
    CACHE_PRICE_CHANGE_THRESHOLD,
    CACHE_TTL_SEC,
    CLAUDE_MODEL_DEEP,
    CLAUDE_MODEL_FAST,
    DATA_DIR,
    MAX_KELLY_FRACTION,
    MIN_CHECKS_PASSING,
    MIN_KELLY_FRACTION,
    MIN_THESIS_CONFIDENCE,
    THESIS_FILE,
)
from models import Action, Side, Thesis

logger = logging.getLogger(__name__)


# ── Analysis cache ─────────────────────────────────────────────────────────────

_analysis_cache: dict[str, dict] = {}
# { ticker: { "time": float, "price": float, "result": Thesis | None } }


def _cache_valid(ticker: str, current_price: float) -> bool:
    """Check if a cached analysis is still fresh."""
    entry = _analysis_cache.get(ticker)
    if entry is None:
        return False
    age = time.time() - entry["time"]
    if age > CACHE_TTL_SEC:
        return False
    price_change = abs(current_price - entry["price"])
    if price_change > CACHE_PRICE_CHANGE_THRESHOLD:
        return False
    return True


def _cache_get(ticker: str) -> Thesis | None:
    entry = _analysis_cache.get(ticker)
    return entry["result"] if entry else None


def _cache_set(ticker: str, price: float, result: Thesis | None) -> None:
    _analysis_cache[ticker] = {
        "time": time.time(),
        "price": price,
        "result": result,
    }


def get_cache_stats() -> dict:
    now = time.time()
    active = sum(1 for e in _analysis_cache.values() if now - e["time"] < CACHE_TTL_SEC)
    return {"total_cached": len(_analysis_cache), "active": active}


# ── Kelly criterion ────────────────────────────────────────────────────────────


def kelly_size(
    p_win: float,
    market_price: float,
    bankroll: float = BANKROLL,
    max_fraction: float = MAX_KELLY_FRACTION,
) -> float:
    """
    Full Kelly:  f* = (p × b − q) / b
    Capped at quarter-Kelly to limit drawdowns.
    Returns 0 for negative-EV or dust-sized bets.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    b = (1 / market_price) - 1
    q = 1 - p_win
    f_star = (p_win * b - q) / b

    if f_star <= 0:
        return 0.0

    f_capped = min(f_star, max_fraction)

    if f_capped < MIN_KELLY_FRACTION:
        return 0.0

    return round(bankroll * f_capped, 2)


# ── Tier 1: Haiku pre-screen ──────────────────────────────────────────────────


def _build_prescreen_prompt(market: dict) -> str:
    return f"""You are a prediction market screener. Quickly assess whether this market
has a tradeable mispricing worth deeper analysis.

MARKET:
  Title: {market['title']}
  YES price: ${market['yes_price']:.2f} (implied probability: {market['yes_price']:.0%})
  Category: {market.get('category', 'unknown')}
  Hours to resolution: {market.get('hours_to_resolution', 'unknown')}
  24h volume: {market.get('volume_24h', 0):,} contracts

Answer with ONLY valid JSON:
{{
  "interesting": true or false,
  "estimated_probability": 0.XX,
  "reason": "one sentence why or why not"
}}"""


async def prescreen_with_haiku(
    client: anthropic.AsyncAnthropic,
    market: dict,
) -> tuple[bool, float]:
    """
    Cheap Haiku call to decide if a market is worth full Sonnet analysis.
    Returns (is_interesting, rough_probability).
    """
    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL_FAST,
            max_tokens=200,
            messages=[{"role": "user", "content": _build_prescreen_prompt(market)}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        result = json.loads(text)
        interesting = result.get("interesting", False)
        probability = float(result.get("estimated_probability", 0.5))

        # Check if Haiku sees enough edge
        gap = abs(probability - market["yes_price"])
        if gap < 0.05:
            interesting = False

        logger.info(
            "  HAIKU %s: %s (prob=%.0f%%, gap=%.0f%%) — %s",
            "PASS" if interesting else "KILL",
            market["title"][:50],
            probability * 100,
            gap * 100,
            result.get("reason", "")[:60],
        )

        return interesting, probability

    except (json.JSONDecodeError, KeyError) as exc:
        logger.debug("Haiku parse failed for %s: %s", market["title"][:30], exc)
        return False, 0.5
    except anthropic.APIError as exc:
        logger.error("Haiku API error: %s", exc)
        return False, 0.5


# ── Tier 2: Sonnet deep analysis ──────────────────────────────────────────────


def _build_analysis_prompt(market: dict) -> str:
    return f"""You are an expert prediction market analyst. Analyze this Kalshi market
and provide your assessment.

MARKET:
  Ticker: {market['ticker']}
  Title: {market['title']}
  Current YES price: ${market['yes_price']:.2f} (implied probability: {market['yes_price']:.0%})
  Current NO price: ${market.get('no_price', 1 - market['yes_price']):.2f}
  Category: {market.get('category', 'unknown')}
  Hours to resolution: {market.get('hours_to_resolution', 'unknown')}
  24h volume: {market.get('volume_24h', 0):,} contracts
  Open interest: {market.get('open_interest', 0):,} contracts

Run these 4 checks:

1. BASE RATE: What does historical data and base rates suggest for this outcome?
   Consider similar past events, polling data, statistical models.

2. NEWS: Has anything material changed in the last 6 hours that shifts probability?
   Consider breaking news, official statements, data releases.

3. VOLUME / OPEN INTEREST: Does the volume and open interest pattern suggest
   informed trading? Is smart money accumulating on one side?

4. DISPOSITION: Is the crowd making a cognitive error?
   Consider: anchoring bias, recency bias, narrative bias, availability heuristic,
   overreaction to news, underreaction to base rates.

For each check, output:
- SIGNAL: BULLISH (favors YES) / BEARISH (favors NO) / NEUTRAL
- REASONING: 1-2 sentences

Then provide your OVERALL ASSESSMENT:
- PROBABILITY: Your estimated true probability of YES (0.00 to 1.00)
- CONFIDENCE: How confident you are in this estimate (0.00 to 1.00)
- ACTION: BUY or SELL
- SIDE: YES or NO (which side to trade)
- CHECKS_PASSING: How many of the 4 checks agree with your direction (0-4)
- THESIS: 2-3 sentence explanation of your trade thesis

Respond ONLY with valid JSON in this exact format:
{{
  "base_rate": {{"signal": "...", "reasoning": "..."}},
  "news": {{"signal": "...", "reasoning": "..."}},
  "volume_oi": {{"signal": "...", "reasoning": "..."}},
  "disposition": {{"signal": "...", "reasoning": "..."}},
  "probability": 0.XX,
  "confidence": 0.XX,
  "action": "buy" or "sell",
  "side": "yes" or "no",
  "checks_passing": N,
  "thesis": "..."
}}"""


async def analyze_market_deep(
    client: anthropic.AsyncAnthropic,
    market: dict,
) -> Thesis | None:
    """Full Sonnet analysis with 4-check framework."""
    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL_DEEP,
            max_tokens=1024,
            messages=[{"role": "user", "content": _build_analysis_prompt(market)}],
        )

        text = response.content[0].text.strip()
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
                "  SONNET SKIP %s — only %d/%d checks",
                market["title"][:50],
                checks,
                4,
            )
            return None

        if confidence < MIN_THESIS_CONFIDENCE:
            logger.info(
                "  SONNET SKIP %s — confidence %.0f%%",
                market["title"][:50],
                confidence * 100,
            )
            return None

        action = Action.BUY if analysis["action"] == "buy" else Action.SELL
        side = Side.YES if analysis["side"] == "yes" else Side.NO

        logger.info(
            "  SONNET PASS %s — %s %s (prob=%.0f%%, conf=%.0f%%, %d/4 checks)",
            market["title"][:50],
            action.value.upper(),
            side.value.upper(),
            analysis["probability"] * 100,
            confidence * 100,
            checks,
        )

        return Thesis(
            ticker=market.get("ticker", ""),
            title=market["title"],
            base_rate=analysis["probability"],
            news_signal=analysis["news"]["signal"],
            volume_signal=analysis["volume_oi"]["signal"] == "BULLISH",
            disposition_bias=analysis["disposition"]["reasoning"],
            confidence=confidence,
            action=action,
            side=side,
            checks_passing=checks,
            reasoning=analysis.get("thesis", ""),
        )

    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.warning("Sonnet parse failed for %s: %s", market["title"][:40], exc)
        return None
    except anthropic.APIError as exc:
        logger.error("Sonnet API error: %s", exc)
        return None


# ── Two-tier pipeline ─────────────────────────────────────────────────────────


async def analyze_market(
    client: anthropic.AsyncAnthropic,
    market: dict,
) -> Thesis | None:
    """
    Two-tier analysis pipeline:
      1. Check cache — skip if fresh
      2. Haiku pre-screen — kill ~80% cheaply
      3. Sonnet deep analysis — full 4-check framework
    """
    ticker = market.get("ticker", "")
    price = market["yes_price"]

    # ── Cache check ────────────────────────────────────────────────────────
    if _cache_valid(ticker, price):
        logger.debug("CACHE HIT %s — skipping", ticker)
        return _cache_get(ticker)

    # ── Tier 1: Haiku pre-screen ───────────────────────────────────────────
    interesting, haiku_prob = await prescreen_with_haiku(client, market)
    if not interesting:
        _cache_set(ticker, price, None)
        return None

    # ── Tier 2: Sonnet deep analysis ───────────────────────────────────────
    thesis = await analyze_market_deep(client, market)
    _cache_set(ticker, price, thesis)
    return thesis


# ── Thesis sizing ──────────────────────────────────────────────────────────────


def size_thesis(thesis: Thesis, yes_price: float) -> float:
    """Run Kelly sizing on an accepted thesis. Returns position size in USD."""
    if thesis.side == Side.YES:
        price = yes_price
        p_win = thesis.base_rate
    else:
        price = 1 - yes_price
        p_win = 1 - thesis.base_rate

    return kelly_size(p_win=p_win, market_price=price)


def dollars_to_contracts(size_dollars: float, price: float) -> int:
    """Convert a dollar position size to contract count."""
    if price <= 0:
        return 0
    return max(1, int(size_dollars / price))


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
    logger.info("Tier 1: %s (pre-screen) → Tier 2: %s (deep)", CLAUDE_MODEL_FAST, CLAUDE_MODEL_DEEP)

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    while True:
        try:
            markets_path = DATA_DIR / "markets.json"
            if not markets_path.exists():
                logger.info("No markets.json yet — waiting for scanner")
                await asyncio.sleep(BRAIN_INTERVAL_SEC)
                continue

            markets = json.loads(markets_path.read_text())
            logger.info("Evaluating %d prescreened markets", len(markets))

            cache_stats = get_cache_stats()
            logger.info(
                "Cache: %d entries (%d active)",
                cache_stats["total_cached"],
                cache_stats["active"],
            )

            actionable: list[dict] = []
            haiku_calls = 0
            sonnet_calls = 0
            cache_hits = 0

            for mkt in markets:
                ticker = mkt.get("ticker", "")
                price = mkt["yes_price"]

                # Track what tier we hit
                was_cached = _cache_valid(ticker, price)
                if was_cached:
                    cache_hits += 1

                thesis = await analyze_market(client, mkt)

                if not was_cached:
                    haiku_calls += 1
                    if thesis is not None:
                        sonnet_calls += 1

                if thesis is None:
                    continue

                position_size = size_thesis(thesis, mkt["yes_price"])
                if position_size <= 0:
                    logger.info(
                        "SKIP %s — Kelly says no bet (size=$0)",
                        mkt["title"][:60],
                    )
                    continue

                price = mkt["yes_price"] if thesis.side == Side.YES else (1 - mkt["yes_price"])
                contracts = dollars_to_contracts(position_size, price)

                actionable.append(
                    {
                        "ticker": mkt["ticker"],
                        "event_ticker": mkt.get("event_ticker", ""),
                        "title": mkt["title"],
                        "yes_price": mkt["yes_price"],
                        "action": thesis.action.value,
                        "side": thesis.side.value,
                        "confidence": thesis.confidence,
                        "probability": thesis.base_rate,
                        "checks_passing": thesis.checks_passing,
                        "position_size_dollars": position_size,
                        "contract_count": contracts,
                        "price_cents": int(price * 100),
                        "reasoning": thesis.reasoning,
                        "volume_signal": thesis.volume_signal,
                    }
                )

            save_theses(actionable)
            logger.info(
                "Cycle done: %d actionable | %d cache hits, %d Haiku calls, %d Sonnet calls",
                len(actionable),
                cache_hits,
                haiku_calls,
                sonnet_calls,
            )

        except Exception:
            logger.exception("Brain cycle failed")

        await asyncio.sleep(BRAIN_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(brain_loop())
