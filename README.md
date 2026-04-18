# Kalshi Trading Bot

A Claude-powered prediction market trading bot for [Kalshi](https://kalshi.com) — the CFTC-regulated US prediction market exchange. Four async agents scan, analyze, trade, and exit — fully automated.

**Total cost: ~$25/month** (Claude API + VPS)

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Scanner   │────▶│    Brain    │────▶│  Executor   │────▶│ Exit Monitor│
│             │     │             │     │             │     │             │
│ Kalshi API  │     │ Claude API  │     │ 3 strategy  │     │ 3 triggers: │
│ pulls open  │     │ runs 4      │     │ agents vote │     │ target hit, │
│ markets,    │     │ checks per  │     │ on each     │     │ volume      │
│ filters 93% │     │ market      │     │ thesis      │     │ spike,      │
│             │     │ Kelly sizes │     │             │     │ stale thesis│
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
   scanner.py          brain.py           executor.py       exit_monitor.py
```

## How It Works

### Step 1 — Scan for Opportunities
`scanner.py` pulls open markets via the Kalshi REST API and kills 93% with hard filters:

| Filter | Threshold | Why |
|--------|-----------|-----|
| Edge gap | < 7% mispricing | Not worth the risk |
| Book depth | < $500 both sides | Can't fill without slippage |
| Time to resolution | < 4h or > 7 days | Too late or too slow |
| 24h volume | < 50K contracts | Slippage eats the edge |
| Price range | < $0.03 or > $0.97 | Too close to settlement |
| Category | Sports | 52% win rate — not profitable |

### Step 2 — Claude Decides
`brain.py` sends each surviving market to Claude with 4 checks:

1. **Base rate** — historical stats and prior probabilities
2. **News** — anything material in the last 6 hours?
3. **Volume / OI** — is smart money accumulating?
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
| **Arbitrage** | Catches price gaps vs Claude's estimate |
| **Convergence** | Enters when price moves toward estimate |
| **Volume Profile** | Confirms via volume/OI accumulation patterns |

**Consensus rules:**
- 2+ agents agree → full position
- 1 agent → half position
- 0 agents → no trade

Orders are placed **live** on Kalshi via their REST API with RSA-PSS authentication.

### Step 4 — Know When to Leave
`exit_monitor.py` runs 3 exit triggers:

| Trigger | Condition | Insight |
|---------|-----------|---------|
| **Target hit** | 85% of expected move captured | Don't hold to settlement |
| **Volume spike** | 3x normal 10-min volume | Smart money is leaving |
| **Stale thesis** | 24h held, < 2% price move | Edge is gone |

## Quick Start

### Prerequisites
- Python 3.11+
- Kalshi account with API access
- RSA key pair (generate at [kalshi.com/account/api](https://kalshi.com/account/api))
- Anthropic API key

### Install

```bash
git clone https://github.com/jbelnick/Polymarket-Trading.git
cd Polymarket-Trading
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys and private key path
```

### Generate Kalshi API Keys

1. Go to [kalshi.com/account/api](https://kalshi.com/account/api)
2. Create an API key — you'll get a **Key ID** and download a **private key PEM file**
3. Save the PEM file somewhere safe (e.g. `~/.kalshi/private_key.pem`)
4. Add both to your `.env` file

### Run

```bash
# Demo mode first (paper trading, no real money)
python main.py --demo

# Scanner only (read-only, see what the bot would do)
python main.py --scan-only

# Full bot — all 4 agents, live trading
python main.py
```

### VPS Deployment

```bash
# cron — daily at 06:00 UTC
crontab -e
0 6 * * * /path/to/start.sh >> /path/to/cron.log 2>&1
```

Or keep it running in a screen session:
```bash
screen -S kalshi-bot
./start.sh
# Ctrl+A, D to detach
```

## Configuration

All parameters are tunable via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | — | Kalshi API key ID (required) |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA private key PEM (required) |
| `KALSHI_ENV` | `demo` | `demo` for paper trading, `prod` for real money |
| `ANTHROPIC_API_KEY` | — | Claude API key (required) |
| `BANKROLL` | `800` | Total capital in USD |
| `CLAUDE_MODEL_FAST` | `claude-haiku-4-5-20251001` | Haiku for pre-screening |
| `CLAUDE_MODEL_DEEP` | `claude-sonnet-4-20250514` | Sonnet for full analysis |
| `MIN_EDGE_GAP` | `0.07` | Minimum mispricing (7%) |
| `MIN_BOOK_DEPTH` | `500` | Minimum order book depth ($) |
| `MIN_MARKET_VOLUME` | `50000` | Minimum 24h volume (contracts) |
| `MAX_KELLY` | `0.25` | Kelly fraction cap (quarter-Kelly) |
| `DISABLED_CATEGORIES` | `sports` | Comma-separated categories to skip |
| `SCAN_INTERVAL` | `1800` | Seconds between market scans (30 min) |
| `BRAIN_INTERVAL` | `7200` | Seconds between Claude analyses (2 hours — cost control) |
| `EXIT_INTERVAL` | `30` | Seconds between exit trigger checks |
| `CACHE_TTL` | `3600` | Seconds before re-analyzing a market |
| `STALE_HOURS` | `24` | Hours before a thesis goes stale |

## File Structure

```
├── main.py              # Orchestrator — launches all 4 agents
├── config.py            # All tunable parameters
├── models.py            # Shared dataclasses (Market, Position, Thesis, etc.)
├── kalshi_client.py     # Kalshi API client with RSA-PSS auth
├── scanner.py           # Step 1: Market scanning and filtering
├── brain.py             # Step 2: Claude analysis + Kelly sizing
├── executor.py          # Step 3: Consensus multi-agent execution (live orders)
├── exit_monitor.py      # Step 4: Exit triggers (live closes)
├── data_analyzer.py     # Optional: analyze your fill history
├── start.sh             # Startup script for VPS deployment
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── data/                # Runtime data (gitignored)
    ├── markets.json     # Latest market scan
    ├── queue.json       # Prescreened markets for brain
    ├── thesis.json      # Actionable theses from Claude
    ├── trades.json      # Trade log
    └── fills.json       # Fill history from Kalshi
```

## Kalshi vs Polymarket

| | Kalshi | Polymarket |
|---|---|---|
| **Regulation** | CFTC-regulated (legal in US) | Offshore |
| **Auth** | RSA-PSS signed headers | Wallet-based / CLOB API key |
| **Pricing** | Cents (1-99) per contract | Fractional (0.0-1.0) per share |
| **Settlement** | $1.00 per winning contract | $1.00 per winning share |
| **Infrastructure** | Centralized exchange | On-chain (Polygon) |

## Cost

With default settings (2h brain interval, $100 bankroll, Haiku pre-screen + Sonnet deep):

| Component | Monthly Cost |
|-----------|-------------|
| Claude API (Haiku + Sonnet) | ~$20 |
| VPS (Hetzner) | $5 |
| Everything else | Free |
| **Total** | **~$25** |

Swap in cheaper models to lower further — Gemini Pro + Flash-Lite drops this to ~$8/mo. See `.env.example` for model overrides.

## Disclaimer

This bot is for educational and research purposes. Trading on prediction markets involves real financial risk. Use demo mode first. The authors are not responsible for any losses incurred. Always start with the Kalshi demo environment before trading with real money.

## License

MIT
