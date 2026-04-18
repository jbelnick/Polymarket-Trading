"""
Smoke tests for the Kalshi trading bot.
Verifies core logic without needing API keys or network access.
"""

import json
import time

# ── 1. Kelly Sizing ────────────────────────────────────────────────────────────

from brain import kelly_size, dollars_to_contracts

print("=" * 60)
print("TEST 1: Kelly Sizing")
print("=" * 60)

# Claude says 82% chance, market price $0.65, bankroll $100
size = kelly_size(0.82, 0.65, 100)
print(f"  82% prob, $0.65 price, $100 bankroll → ${size}")
assert size > 0, "Should be positive EV"
assert size <= 25, "Should be capped at quarter-Kelly ($25 max on $100)"

# Negative EV — should return 0
size_neg = kelly_size(0.30, 0.65, 100)
print(f"  30% prob, $0.65 price → ${size_neg} (should be $0)")
assert size_neg == 0, "Negative EV should return 0"

# Edge case: price at boundary
size_edge = kelly_size(0.50, 0.99, 100)
print(f"  50% prob, $0.99 price → ${size_edge} (should be $0)")
assert size_edge == 0, "Should be negative EV at 0.99"

# Contract conversion
contracts = dollars_to_contracts(25, 0.65)
print(f"  $25 at $0.65/contract → {contracts} contracts")
assert contracts == 38, f"Expected 38, got {contracts}"

print("  PASSED\n")

# ── 2. Market Parsing ─────────────────────────────────────────────────────────

from scanner import parse_market, prescreen_market

print("=" * 60)
print("TEST 2: Market Parsing + Pre-screen")
print("=" * 60)

raw_market = {
    "ticker": "KXBTC-26APR18-B100000",
    "event_ticker": "KXBTC",
    "title": "Will Bitcoin be above $100,000 on April 18?",
    "subtitle": "",
    "yes_price": 65,  # cents
    "no_price": 35,
    "yes_bid": 64,
    "yes_ask": 66,
    "volume": 50000,
    "volume_24h": 80000,
    "open_interest": 12000,
    "status": "open",
    "close_time": None,
    "category": "crypto",
}

market = parse_market(raw_market)
assert market is not None, "Should parse successfully"
print(f"  Parsed: {market.ticker} — YES=${market.yes_price}")
assert market.yes_price == 0.65, f"Price should be $0.65, got {market.yes_price}"
assert market.no_price == 0.35, f"NO price should be $0.35, got {market.no_price}"

# Sports market — should be filtered
sports_market = {**raw_market, "ticker": "SPORT-1", "category": "sports"}
sm = parse_market(sports_market)
if sm:
    result = prescreen_market(sm)
    print(f"  Sports market pre-screen: {result} (should be False)")
    assert not result, "Sports should be filtered"

# Closed market — should return None
closed = {**raw_market, "status": "closed"}
cm = parse_market(closed)
print(f"  Closed market parse: {cm} (should be None)")
assert cm is None, "Closed markets should be skipped"

print("  PASSED\n")

# ── 3. Consensus Engine ───────────────────────────────────────────────────────

from executor import (
    ArbitrageAgent, ConvergenceAgent, VolumeProfileAgent,
    compute_consensus, execute_consensus,
)
from models import Action, Side, AgentVote, AgentType
import asyncio

print("=" * 60)
print("TEST 3: Consensus Engine")
print("=" * 60)

thesis_strong = {
    "ticker": "KXBTC-26APR18-B100000",
    "title": "BTC above $100K",
    "yes_price": 0.55,
    "probability": 0.75,
    "confidence": 0.85,
    "action": "buy",
    "side": "yes",
    "position_size_dollars": 100,
    "contract_count": 180,
    "price_cents": 55,
    "reasoning": "Strong base rate divergence",
    "volume_signal": True,
}

# All 3 agents should agree
agents = [ArbitrageAgent(), ConvergenceAgent(), VolumeProfileAgent()]
votes = [a.evaluate(thesis_strong, {}) for a in agents]

for v in votes:
    print(f"  {v.agent.value}: {v.action.value} {v.side.value} (conf={v.confidence:.0%}) — {v.reasoning}")

action, side, mult = compute_consensus(votes, thesis_strong)
print(f"  Consensus: {action.value if action else 'NONE'} {side.value if side else '-'} x{mult}")
assert action == Action.BUY, "Should be BUY"
assert mult == 1.0, "All 3 agree → full position"

# Weak thesis — agents should disagree
thesis_weak = {
    **thesis_strong,
    "probability": 0.59,  # barely above market price
    "confidence": 0.60,   # low confidence
    "volume_signal": False,
}
votes_weak = [a.evaluate(thesis_weak, {}) for a in agents]
action_w, side_w, mult_w = compute_consensus(votes_weak, thesis_weak)
print(f"\n  Weak thesis consensus: {action_w.value if action_w else 'NONE'} x{mult_w}")

print("  PASSED\n")

# ── 4. Exit Triggers ──────────────────────────────────────────────────────────

from exit_monitor import (
    check_target_hit, check_stale_thesis, evaluate_exit,
    record_volume, check_volume_spike,
)
from models import Position, ExitReason

print("=" * 60)
print("TEST 4: Exit Triggers")
print("=" * 60)

pos = Position(
    ticker="KXBTC-26APR18-B100000",
    title="BTC above $100K",
    action=Action.BUY,
    side=Side.YES,
    entry_price=0.55,
    count=100,
    expected_gap=0.20,
    entry_time=time.time() - 3600,  # 1 hour ago
)

# Target hit: entry=0.55, gap=0.20, target=0.55+(0.20*0.85)=0.72
hit = check_target_hit(pos, 0.73)
print(f"  Target hit at $0.73 (target=$0.72): {hit} (should be True)")
assert hit, "Should trigger target hit"

not_hit = check_target_hit(pos, 0.60)
print(f"  Target hit at $0.60: {not_hit} (should be False)")
assert not not_hit, "Should not trigger"

# Stale thesis: needs 24h+ and <2% move
stale_pos = Position(
    ticker="STALE-1", title="Stale", action=Action.BUY, side=Side.YES,
    entry_price=0.50, count=50, expected_gap=0.15,
    entry_time=time.time() - 90000,  # 25 hours ago
)
stale = check_stale_thesis(stale_pos, 0.51)  # <2% move
print(f"  Stale after 25h, 1% move: {stale} (should be True)")
assert stale, "Should be stale"

not_stale = check_stale_thesis(stale_pos, 0.60)  # 10% move
print(f"  Stale after 25h, 10% move: {not_stale} (should be False)")
assert not not_stale, "Big move = not stale"

# Volume spike
for i in range(20):
    record_volume("VOL-TEST", 100)  # normal volume
import time as _t
_t.sleep(0.01)
# Simulate spike
for i in range(5):
    record_volume("VOL-TEST", 1000)

vol_spike_pos = Position(
    ticker="VOL-TEST", title="Vol", action=Action.BUY, side=Side.YES,
    entry_price=0.50, count=50, expected_gap=0.15,
)
spike = check_volume_spike(vol_spike_pos)
print(f"  Volume spike detected: {spike}")

# Full evaluate_exit
reason = evaluate_exit(pos, 0.73)
print(f"\n  Full exit eval at $0.73: {reason.value if reason else 'NONE'} (should be TARGET_HIT)")
assert reason == ExitReason.TARGET_HIT

print("  PASSED\n")

# ── 5. Kalshi Client (structure only — no network) ────────────────────────────

from kalshi_client import KalshiClient
import inspect

print("=" * 60)
print("TEST 5: Kalshi Client API surface")
print("=" * 60)

methods = [m for m in dir(KalshiClient) if not m.startswith("_")]
print(f"  Client methods: {', '.join(methods)}")

required = [
    "get_markets", "get_all_markets", "get_market",
    "get_orderbook", "get_balance", "get_positions",
    "place_order", "cancel_order", "get_midpoint",
    "get_book_depth_dollars",
]
for m in required:
    assert hasattr(KalshiClient, m), f"Missing method: {m}"
    print(f"  {m}: OK")

print("  PASSED\n")

# ── 6. Analysis Cache ─────────────────────────────────────────────────────────

from brain import _cache_valid, _cache_set, _cache_get, get_cache_stats

print("=" * 60)
print("TEST 6: Analysis Cache")
print("=" * 60)

# Empty cache
assert not _cache_valid("TEST-1", 0.50), "Should miss on empty cache"
print("  Empty cache miss: OK")

# Set and hit
_cache_set("TEST-1", 0.50, None)
assert _cache_valid("TEST-1", 0.50), "Should hit after set"
print("  Cache hit after set: OK")

# Small price change — still valid
assert _cache_valid("TEST-1", 0.505), "0.5% move should still be valid"
print("  Cache valid with 0.5% move: OK")

# Large price change — invalidated
assert not _cache_valid("TEST-1", 0.55), "5% move should invalidate"
print("  Cache invalidated with 5% move: OK")

# Stats
stats = get_cache_stats()
print(f"  Cache stats: {stats}")
assert stats["total_cached"] >= 1

print("  PASSED\n")

# ── 7. Two-Tier Config ────────────────────────────────────────────────────────

from config import CLAUDE_MODEL_FAST, CLAUDE_MODEL_DEEP, BANKROLL

print("=" * 60)
print("TEST 7: Two-Tier Config + Bankroll")
print("=" * 60)

print(f"  Tier 1 (fast): {CLAUDE_MODEL_FAST}")
assert "haiku" in CLAUDE_MODEL_FAST, "Fast model should be Haiku"

print(f"  Tier 2 (deep): {CLAUDE_MODEL_DEEP}")
assert "sonnet" in CLAUDE_MODEL_DEEP, "Deep model should be Sonnet"

print(f"  Bankroll: ${BANKROLL}")
assert BANKROLL == 100, f"Bankroll should be $100, got ${BANKROLL}"

# Kelly with $100 bankroll — max bet should be $25
max_bet = kelly_size(0.99, 0.50, 100, 0.25)
print(f"  Max possible bet (99% edge): ${max_bet}")
assert max_bet <= 25, f"Max bet should be ≤$25 on $100 bankroll"

print("  PASSED\n")

# ── Summary ────────────────────────────────────────────────────────────────────

print("=" * 60)
print("ALL TESTS PASSED")
print("=" * 60)
