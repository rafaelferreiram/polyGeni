# polyGeni

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets.
Covers **Bitcoin**, **Sports**, and **Global Events** markets with a real-time analysis engine, Kelly Criterion position sizing, and a live dashboard.

---

## How it works

### 1. Market Discovery
Every scan cycle (default: every 5 minutes) the bot fetches active markets from Polymarket's Gamma API across three categories:
- **Bitcoin** — price targets (e.g. "Will BTC hit $120k by Dec 2026?")
- **Sports** — match outcomes with bookmaker consensus cross-reference
- **Events** — political and world events with news sentiment analysis

### 2. Analysis Engine

#### Bitcoin
- Fetches 90 days of BTC/USDT daily candles from Binance (no API key needed)
- Computes RSI, MACD, and Bollinger Bands
- Uses a **log-normal probability model** to estimate the probability of BTC reaching a price target by the market's resolution date
- Compares model probability against Polymarket's implied probability (current YES/NO price)

#### Sports
- Fetches live H2H odds from **The Odds API** across 8 leagues (NFL, NBA, EPL, UCL, MLB, NHL, MMA, Tennis)
- Removes bookmaker margin (overround) to get **fair consensus probability**
- Skips season-long markets (championships, cups, finals) to avoid comparing game odds to futures
- Adds a small news sentiment nudge for relevant headlines

#### Events
- Queries **NewsAPI** for headlines matching each market question
- Scores sentiment with a lightweight keyword model
- Shifts the probability estimate from the market's baseline based on sentiment signal

### 3. Edge Detection
For each market, the bot computes:
- **Edge** = our estimated probability − Polymarket implied probability
- **Bet direction**: YES or NO, whichever side has the edge
- Only surfaces opportunities where edge ≥ 5% (configurable)

### 4. Position Sizing — Kelly Criterion
The bot sizes every bet using **fractional Kelly (¼ Kelly)** for safety:

```
f* = (b × p − q) / b       # full Kelly fraction
bet = bankroll × f* × 0.25  # fractional Kelly
bet = min(bet, bankroll × 30%)  # hard cap per position
```

Where `b = (1 − price) / price`, `p` = our probability, `q = 1 − p`.

### 5. Risk Management
Before every trade:
- Max **3 open positions** at once
- Min **$1.00 USDC** per bet
- Max **30%** of bankroll per single position
- Edge floor recheck before execution

### 6. Trade Execution
Orders are placed via the official `py-clob-client` SDK using L2 API credentials derived from your wallet private key. All order signing happens locally — Polymarket never has your private key.

---

## Dashboard

Open `http://localhost:8000` after starting the server.

| Section | What it shows |
|---|---|
| **Header** | Balance, bot status, Start/Stop/Scan buttons |
| **Portfolio row** | Balance, Total P&L, Realized P&L, Unrealized P&L, Win Rate, Open Positions |
| **Live Opportunities** | Current scan results ranked by edge, with reasoning |
| **Open Positions** | All active bets with unrealized P&L |
| **Trade History** | All past trades with status and P&L |

The dashboard auto-refreshes every 30 seconds.

---

## Setup

### Requirements
- Python 3.10+
- A funded Polymarket account (USDC on Polygon)

### 1. Clone and install
```bash
cd ~/Workspace/Personal
git clone git@github.com:rafaelferreiram/polyGeni.git
cd polyGeni
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Configure `.env`
```bash
cp .env.example .env
```

Fill in your credentials:

| Variable | Where to get it |
|---|---|
| `POLY_PRIVATE_KEY` | Polymarket → Cash → `···` → Export Private Key |
| `POLY_WALLET_ADDRESS` | Your `0x...` Polymarket wallet address |
| `NEWS_API_KEY` | [newsapi.org](https://newsapi.org) — free, 100 req/day |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com) — free, 500 req/month |

Bot settings (optional):
```
BOT_BUDGET_USDC=10.0         # starting bankroll reference
BOT_MAX_POSITION_PCT=0.30    # max % per trade (30%)
BOT_KELLY_FRACTION=0.25      # fractional Kelly multiplier
BOT_MIN_EDGE=0.05            # minimum edge threshold (5%)
BOT_SCAN_INTERVAL_SEC=300    # scan interval in seconds
```

### 3. Fund your Polymarket wallet
Deposit USDC to your Polymarket wallet. The bot reads your live balance at every scan.

### 4. Run locally
```bash
.venv/bin/uvicorn src.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

---

## Running the bot

### Monitor-only mode (recommended to start)
Click **Start (monitor)** in the dashboard — the bot scans every 5 minutes and surfaces opportunities, but places **no real orders**.

### Auto-trade mode
Click **Start (auto-trade)** — the bot automatically places the top opportunity each scan cycle if edge ≥ 5%.

### Manual scan
Click **Scan Now** to run a scan immediately at any time.

---

## Deploy to production (Docker)

### Build and run
```bash
docker-compose up -d
```

This starts the server on port `8000` and mounts the SQLite database as a volume so your trade history persists across restarts.

### Deploy on a VPS (Hetzner/DigitalOcean)
```bash
# On the server
git clone git@github.com:rafaelferreiram/polyGeni.git
cd polyGeni
cp .env.example .env   # fill in credentials
docker-compose up -d
```

Add a reverse proxy (nginx/Caddy) for HTTPS in production.

---

## Project structure

```
polyGeni/
├── src/
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Settings from .env
│   ├── database.py             # SQLite setup
│   ├── models.py               # Trade, Position, Opportunity models
│   ├── polymarket/
│   │   ├── client.py           # CLOB API wrapper (auth + orders)
│   │   └── gamma.py            # Market discovery (Gamma API)
│   ├── feeds/
│   │   ├── bitcoin.py          # Binance price feed + indicators
│   │   ├── news.py             # NewsAPI sentiment feed
│   │   └── sports.py           # The Odds API feed
│   ├── analysis/
│   │   ├── engine.py           # Scan orchestrator
│   │   ├── bitcoin.py          # Bitcoin probability model
│   │   ├── sports.py           # Sports edge detector
│   │   ├── events.py           # Events sentiment analyzer
│   │   └── kelly.py            # Kelly Criterion sizing
│   ├── bot/
│   │   ├── scanner.py          # Scheduled scan loop
│   │   ├── trader.py           # Order execution + DB recording
│   │   └── risk.py             # Pre-trade risk checks
│   └── api/
│       └── routes.py           # FastAPI REST endpoints
└── frontend/
    ├── index.html              # Dashboard UI
    ├── style.css               # Dark theme styles
    └── app.js                  # Dashboard logic
```

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Bot status + balance |
| POST | `/api/bot/start?auto_trade=false` | Start bot |
| POST | `/api/bot/stop` | Stop bot |
| POST | `/api/bot/scan` | Trigger immediate scan |
| GET | `/api/opportunities` | Saved opportunities from DB |
| GET | `/api/opportunities/live` | Latest in-memory scan results |
| GET | `/api/positions` | Open positions with live P&L |
| GET | `/api/trades` | Trade history |
| GET | `/api/portfolio` | Portfolio summary |
| POST | `/api/trade/manual` | Place a manual trade |

---

## Important notes

- **Never share your private key** or commit `.env` to version control (`.gitignore` already excludes it)
- Start in **monitor mode** first to validate opportunities before enabling auto-trade
- The $10 → $20 goal requires finding 1-2 high-confidence trades — quality over quantity
- Bitcoin markets tend to have the strongest signal from the log-normal model
- Sports opportunities require an active H2H game scheduled (not season-long futures)
