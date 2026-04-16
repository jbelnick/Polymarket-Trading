# Polymarket Trading Bot

A Claude-powered prediction market trading bot that runs 24/7 on a $5 VPS. Four async agents scan, analyze, trade, and exit — fully automated.

**Total cost: ~$25/month** (Claude API + VPS)

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Scanner   │────▶│    Brain    │────▶│  Executor   │────▶│ Exit Monitor│
│             │     │             │     │             │     │             │
│ polymarket- │     │ Claude API  │     │ 3 strategy  │     │ 3 triggers: │
│ cli pulls   │     │ runs 4      │     │ agents vote │     │ target hit, │
│ 500 markets │     │ checks per  │     │ on each     │     │ volume      │
│ filters 93% │     │ market      │     │ thesis      │     │ spike,      │
│             │     │ Kelly sizes │     │             │     │ stale thesis│
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
   scanner.py          brain.py           executor.py       exit_monitor.py
```

## How It Works

### Step 0 — Find Who's Winning
`data_analyzer.py` crunches historical trade data from [poly_data](https://github.com/warproxxx/poly_data) (86M+ trades). Finds every wallet with 100+ trades and >70% win rate, ranks by profit, exports top 50 as target wallets.

```bash
python main.py --analyze ~/poly_data/processed/trades.csv
```

### Step 1 — Scan for Opportunities
`scanner.py` pulls 500 active markets via [polymarket-cli](https://github.com/Polymarket/polymarket-cli) and kills 93% with hard filters:

| Filter | Threshold | Why |
|--------|-----------|-----|
| Edge gap | < 7% mispricing | Not worth the risk |
| Book depth | < $500 both sides | Can't fill without slippage |
| Time to resolution | < 4h or > 7 days | Too late or too slow |
| 24h volume | < $50K | Slippage eats the edge |
| Category | Sports | 52% win rate — not profitable |

### Step 2 — Claude Decides
`brain.py` sends each surviving market to Claude with 4 checks:

1. **Base rate** — historical stats and prior probabilities
2. **News** — anything material in the last 6 hours?
3. **Whale check** — are target wallets active in this market?
4. **Disposition** — is the crowd making a cognitive error?

**3 of 4 must agree.** Confidence must exceed 75%. Position sizing uses the Kelly criterion capped at quarter-Kelly.

```python
def kelly_size(p_win, market_price, bankroll, max_fraction=0.25):
    b = (1 / market_price) - 1
    q = 1 - p_win
    f_star = (p_win * b - q) / b
    if f_star <= 0:
        return 0  # negative EV — kill trade
    return round(bankroll * min(f_star, max_fraction), 2)
```

### Step 3 — Consensus Execution
`executor.py` runs 3 independent strategy agents on each thesis:

| Agent | Strategy |
|-------|----------|
| **Arbitrage** | Catches price gaps between related markets |
| **Convergence** | Enters when price moves toward Claude's estimate |
| **Whale Copy** | Mirrors target wallets with 60s delay |

**Consensus rules:**
- 2+ agents agree → full position
- 1 agent → half position
- 0 agents → no trade

This filter alone kills 40% of losing trades.

### Step 4 — Know When to Leave
`exit_monitor.py` runs 3 exit triggers:

| Trigger | Condition | Insight |
|---------|-----------|---------|
| **Target hit** | 85% of expected move captured | Top wallets capture ~73% avg, don't hold to settlement |
| **Volume spike** | 3× normal 10-min volume | Smart money is leaving |
| **Stale thesis** | 24h held, < 2% price move | Edge is gone |

**Key finding from data:** 91% of top-wallet exits happen *before* resolution. They buy at 40¢, sell at 65¢, and move on.

## Quick Start

### Prerequisites
- Python 3.11+
- [polymarket-cli](https://github.com/Polymarket/polymarket-cli) installed
- Anthropic API key
- Polymarket wallet (private key + API credentials)

### Install

```bash
git clone https://github.com/jbelnick/Polymarket-Trading.git
cd Polymarket-Trading
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
```

### Run

```bash
# Full bot — all 4 agents
python main.py

# Scanner only (read-only, no wallet needed)
python main.py --scan-only

# Analyze wallet data first
python main.py --analyze ~/poly_data/processed/trades.csv
```

### VPS Deployment (cron)

```bash
# Run daily at 06:00 UTC
crontab -e
0 6 * * * /path/to/Polymarket-Trading/start.sh >> /path/to/cron.log 2>&1
```

Or keep it running in a screen session:
```bash
screen -S polybot
./start.sh
# Ctrl+A, D to detach
```

## Configuration

All parameters are tunable via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key (required) |
| `POLYMARKET_PRIVATE_KEY` | — | Wallet private key (required for trading) |
| `BANKROLL` | `800` | Total capital in USD |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model for analysis |
| `MIN_EDGE_GAP` | `0.07` | Minimum mispricing (7%) |
| `MIN_BOOK_DEPTH` | `500` | Minimum order book depth ($) |
| `MIN_MARKET_VOLUME` | `50000` | Minimum 24h volume ($) |
| `MAX_KELLY` | `0.25` | Kelly fraction cap (quarter-Kelly) |
| `DISABLED_CATEGORIES` | `sports` | Comma-separated categories to skip |
| `SCAN_INTERVAL` | `300` | Seconds between market scans |
| `STALE_HOURS` | `24` | Hours before a thesis goes stale |

## File Structure

```
├── main.py              # Orchestrator — launches all 4 agents
├── config.py            # All tunable parameters
├── models.py            # Shared dataclasses (Market, Position, Thesis, etc.)
├── data_analyzer.py     # Step 0: Wallet profiling from poly_data
├── scanner.py           # Step 1: Market scanning and filtering
├── brain.py             # Step 2: Claude analysis + Kelly sizing
├── executor.py          # Step 3: Consensus multi-agent execution
├── exit_monitor.py      # Step 4: Exit triggers
├── start.sh             # Startup script for VPS deployment
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── data/                # Runtime data (gitignored)
    ├── targets.json     # Top wallets from poly_data analysis
    ├── markets.json     # Latest market scan
    ├── queue.json       # Prescreened markets for brain
    ├── thesis.json      # Actionable theses from Claude
    └── trades.json      # Trade log
```

## Built On

| Repo | What it provides |
|------|-----------------|
| [warproxxx/poly_data](https://github.com/warproxxx/poly_data) | 86M+ historical trades, every wallet |
| [Polymarket/polymarket-cli](https://github.com/Polymarket/polymarket-cli) | Market scanning, order book data, trade execution |
| [Polymarket/agents](https://github.com/Polymarket/agents) | Agent framework, LLM integration |
| [dylanpersonguy/Polymarket-Trading-Bot](https://github.com/dylanpersonguy/Polymarket-Trading-Bot) | 7 strategies, execution engine patterns |

## Cost

| Component | Monthly Cost |
|-----------|-------------|
| Claude API | ~$20 |
| VPS (Hetzner) | $5 |
| Everything else | Free |
| **Total** | **~$25** |

## ⚠️ Disclaimer

This bot is for educational and research purposes. Trading on prediction markets involves real financial risk. The authors are not responsible for any losses incurred. Past performance of analyzed wallets does not guarantee future results. Always trade with money you can afford to lose.

## License

MIT
