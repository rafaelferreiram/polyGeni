# Kairos

*Kairos* is Greek for "the right moment": a team of trading bots that watches the whole crypto market 24/7 and acts
only when the moment is right. (Repository and package name: `polyGeni`.)

> ⚠️ **No real trading by default.** The Polymarket bot only places real orders with `POLY_LIVE_TRADING=1`
> explicitly set; otherwise every order is a dry run. The Solana bot is **paper trading only**. The Hyperliquid
> bot trades real money only with `LIVE_TRADING=1`, an API wallet key for that network **and** a click on Start in
> the dashboard's 💰 Real account, inside hard USD caps (default $20 capital, $12 per position, 2x isolated, long-only).

## Multi-agent crypto + gold/silver bot — paper, simulation and small live account

Simulated money, **real market data**: Solana spot via real Jupiter quotes, and Hyperliquid perps
(BTC, ETH, SOL, `xyz:GOLD`, `xyz:SILVER`) via the real L2 order book, taker fees and hourly funding.
Dashboard: [http://127.0.0.1:8000](http://127.0.0.1:8000) (the old `/paper` link opens the same page).

### Quick start
```bash
make install     # Python 3.11 venv via uv
make demo        # dummy account: real quotes, forced trades on both venues, ledger self-check, UI on :8001
make run         # 24/7 loop + dashboard on http://127.0.0.1:8000
make test        # offline pytest suite
make service     # run 24/7 on this Mac as a launchd agent (see "Running 24/7" below)
make backtest    # walk-forward reports in reports/
```

### Architecture

```
            ┌──────────── every PAPER_CYCLE_SEC (APScheduler, heartbeat) ────────────┐
 ledger ⇄ reconcile ⇄ positions → mark-to-market (Jupiter sell quote) → stop / TP / max-hold exits
                                   → daily-loss check → persistent KILL SWITCH
 ScoutAgent ─► SafetyAgent ─► StrategyAgents ─► ResearchAgent ─► DebateAgent ─► RiskAgent ─► ExecutorAgent ─► PaperBroker
 (DexScreener  (anti-rug,     (momentum,        (analyst desk:    (LLM bull vs   (rules: size,  (only one that   (real Jupiter
  trending +    rejects by     mean-reversion;   Jupiter metrics   bear, VETO     exposure, max   talks to the     quote + slippage
  watchlist)    default)       own sub-capital)  + LLM web/MCP)    only)          positions; VETO) broker)          + fees → paper.db)
```

| Agent | Decides? | What it does |
|---|---|---|
| `ScoutAgent` | candidates only | DexScreener boosted/latest Solana tokens + a watchlist of established tokens; best USDC/SOL pair; min liquidity & 24h volume |
| `SafetyAgent` | can only reject | **No launch sniping**: pair age ≥ `SAFETY_MIN_PAIR_AGE_H` (24h). Min liquidity, mint & freeze authority must be revoked (on-chain RPC), no risky Token-2022 extensions, top-holder concentration (Jupiter audit, RPC fallback). Anything unverifiable ⇒ reject. Context: on Pump.fun ~98% of tokens were rugs/manipulation |
| `MeanReversion` (on), `MomentumBreakout` (off) | signal | Deterministic, pure functions of closed 1h candles (GeckoTerminal / Hyperliquid live; Binance / Hyperliquid in backtest). Enabled by evidence: momentum lost in every walk-forward, so `PAPER_STRATEGIES=mean_reversion` by default |
| `ResearchAgent` | can only veto | Per-token brief: Jupiter organic score, buy/sell flow, holder and liquidity change, verification, concentration → red flags + risk level; with `ANTHROPIC_API_KEY`, Claude with live web search (+ optional MCP servers) adds thesis, risks, catalysts, sentiment |
| `DebateAgent` | can only veto | Optional LLM bull-vs-bear review. Off without `ANTHROPIC_API_KEY`. Cannot open, resize or redirect a trade; errors/unparsable output ⇒ veto |
| `RiskAgent` | **final veto + sizing** | No LLM. Per-market profile (`MARKET_PROFILES`: Solana 5%/position, Hyperliquid 15% with 1.5× wider stops), max total exposure, max positions, strategy sub-capital, max entries/day, stale-price veto, mandatory stop/TP, daily loss limit ⇒ persistent kill switch. Hyperliquid is **1x, long-only**: notional ≤ cash |
| `ExecutorAgent` | — | The only component holding brokers; routes by venue and refuses any order whose SHA-256 fingerprint differs from what Risk approved |

Lesson applied from Alpha Arena (LLMs trading alone lost 30–60%): **rules decide, the LLM only analyses and vetoes.**

### Real trading on Hyperliquid (testnet first) — `src/solana/hl_live.py`
Same engine and agents; only the broker is real (`HyperliquidLiveBroker`, official `hyperliquid-python-sdk`).
- **Two keys:** an API ("agent") wallet per network in `.env` (`HL_ACCOUNT_ADDRESS`, `HL_API_WALLET_KEY_TESTNET` /
  `HL_API_WALLET_KEY_MAINNET` — they can trade but cannot withdraw), `HL_NETWORK=testnet|mainnet`, **and** Start on
  the dashboard's 💰 Real account. Mainnet also needs `LIVE_TRADING=1` (Polymarket uses its own `POLY_LIVE_TRADING`).
- **Unified accounts** are supported: the balance is read from the spot clearinghouse (no Spot→Perps transfer needed).
- **Hard limits:** 1x isolated, long-only, `LIVE_MAX_CAPITAL_USD` / `LIVE_MAX_POSITION_USD` caps, one position,
  daily loss → kill switch, Hyperliquid's $10 minimum respected.
- **Protection on the exchange:** every entry gets reduce-only stop-loss and take-profit trigger orders; if they
  cannot be placed the position is closed immediately.
- **Idempotent:** client order ids derived from our order id; a crash after submit is never resubmitted.
- **Exchange is the truth:** native stop/TP exits are recorded from fills; untracked positions or equity drift trip
  the kill switch. One ledger per network (`live_testnet.db`, `live_mainnet.db`) so test money never mixes with real money. "Fechar tudo" flattens and disarms; testnet has a "test order" button.

### One wallet, a team of bots
- **Team:** `ARENA_MODELS` (default `rules`, Fable 5.1, Opus 5.5, Opus 5, Sonnet 5.5, Haiku 5.5). Each bot runs in the
  **lab** with its own $20 paper wallet and the real account's exact limits — that is how each model is measured.
- **Shared wallet:** in "Equipa IA" mode every bot proposes for the same wallet (simulation or real); the
  **Coordinator** executes the proposal of the bot with the best lab expectancy on that market. One wallet, no
  double-spending, every trade attributed to the bot that made it. LLM bots reuse their lab decision for the same
  candle (one call feeds both). Positions scale with capital (`LIVE_MAX_POSITIONS=0` = capital ÷ position size).
- **Self-pause:** a bot with nothing to do sleeps (LLM suggests `next_check_min`, or idle-streak back-off up to
  `BOT_MAX_SLEEP_MIN`) and wakes early when price moves `BOT_WAKE_MOVE_PCT` or a position needs watching.
- **Leverage** `LIVE_LEVERAGE` (default 2x isolated, max 3x): with $18 the team can hold up to 3 positions of ~$12 at
  once instead of one. Each keeps its own stop and target on the exchange; daily loss (10%) and the kill switch work on
  equity, so the worst day is still capped.
- **Thermometer** on the dashboard: market temperature from the bots' own scan (how many of the 29 coins are in an
  uptrend, average RSI and 24h move, coins close to a buy) and our return vs simply holding BTC.
- **Connections** panel (`GET /api/paper/health`, cached 60s): Hyperliquid data and real account (API wallet
  authorized, balance), Anthropic (key + models), Arbitrum and Solana RPC, Jupiter, DexScreener, CoinAPI (optional, no
  credits). CoinMarketCap and TradingView are MCP connectors used by Claude in the morning/evening routine.
- **Lucro real** has a toggle to show profit with or without the AI cost.
- **☕ Coffee** (button next to the bots, `POST /api/paper/account/coffee`): keeps both wallets (simulation and real) and the whole team awake
  (no self-pause, a scan every cycle) for 4 hours and doubles the daily entry cap (12 → 24) for day trading. Entry rules and stops do **not** loosen: in the backtest, accepting shallower
  dips (Bollinger k 1.0–1.5, RSI < 40–45, shorter targets) tripled the trades and turned +2.5% into −4% to −19%.
- Markets: the whole liquid crypto market, not one coin — 29 perps (`HL_BROAD`): CoinMarketCap top-50 by market cap
  plus Hyperliquid's most traded, each with ≥ 208 days of history and ≥ $1M daily volume on Hyperliquid (BTC, ETH, SOL,
  HYPE, XRP, BNB, DOGE, ADA, TRX, AVAX, LINK, LTC, BCH, XLM, HBAR, DOT, SUI, NEAR, AAVE, UNI, ARB, TAO, ZEC, XMR, WLD, ENA,
  ZRO, ONDO, kPEPE) for simulation, lab and real account. Every cycle all of them are scanned and ranked (uptrend first, then the most
  oversold), so the single position slot goes to the best setup of the moment. Override with `HL_MARKETS` /
  `HL_LIVE_MARKETS`; gold/silver with `HL_INCLUDE_METALS=1`. Per-coin backtest (208 days, fixed rules): the original 5
  coins +$2.43 over 54 trades, the 19 new ones −$3.69 over 159 — the wider set is being proven live, watch the lab.

### Decision mode and model arena
- **Regras | LLM** switch: in LLM mode an LLM (`llm_trader.py`) decides BUY/HOLD per market and SELL/HOLD for open
  positions, once per closed 1h candle. Risk still sets size and limits; LLM stops are clamped to 1–10%.
- **Arena** (`arena.py`): one paper bot per model (`ARENA_MODELS`, default Opus 5.5 / Sonnet 5.5 / Haiku 5.5),
  same markets and limits, separate ledgers, leaderboard in the dashboard. The real account uses one chosen model.
- Cost: roughly 216 calls/day per model at 9 markets × 24 candles (Opus ~$9/day, Sonnet ~$4, Haiku ~$0.2).

### Venues (pluggable `Broker` interface: buy / sell / mark / accrue)
- `PaperBroker` (Jupiter): real quote incl. route & price impact, + `PAPER_SLIPPAGE_BPS` + base/priority fee.
- `HyperliquidPaperBroker`: public Info API only (`l2Book`, `metaAndAssetCtxs` with `dex:"xyz"` for HIP-3,
  `candleSnapshot`, `fundingHistory`). Fills walk the real book; taker fee 4.5 bps (HIP-3 `xyz:GOLD` 9 bps,
  `xyz:SILVER` 0.9 bps in growth mode); hourly funding accrued to an append-only `funding` table;
  leverage ≠ 1 or notional > cash is rejected. No Exchange API, no signing.

### Ledger & decision log (`paper.db`, separate from `polygeni.db`)
- `fills`, `decisions`, `cash_movements`: **append-only** (SQLite triggers block UPDATE/DELETE); fills idempotent by `order_id`.
- `positions` is a materialised view rebuilt from fills every cycle (reconciliation logs any mismatch).
- Every entry, veto, exit, kill-switch and error is logged with signals, each agent's vote and the reason.

### Run it (paper)
```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
PAPER_AUTOSTART=1 .venv/bin/uvicorn src.main:app --port 8000      # 24/7 loop + dashboard at /paper
.venv/bin/python -m src.solana.engine --cycles 3                   # or a few cycles from the shell
.venv/bin/python -m src.solana.demo                                # dummy account demo → demo.db (forced entries, proves plumbing)
.venv/bin/python -m src.solana.backtest --days 180                 # walk-forward report → reports/
.venv/bin/python -m pytest -q                                      # offline tests (no network)
```

Main settings (env, conservative defaults): `PAPER_START_USDC=100`, `PAPER_CYCLE_SEC=300`,
`RISK_MAX_POSITION_PCT=0.10`, `RISK_MAX_EXPOSURE_PCT=0.50`, `RISK_MAX_POSITIONS=4`, `RISK_MAX_DAILY_LOSS_PCT=0.05`,
`SAFETY_MIN_PAIR_AGE_H=24`, `SAFETY_MIN_LIQUIDITY_USD=250000`, `PAPER_SLIPPAGE_BPS=30`,
`PAPER_PRIORITY_FEE_LAMPORTS=100000`, `SOLANA_RPC_URL` (public RPC by default), `ANTHROPIC_API_KEY`,
`DEBATE_MODEL`, `DEBATE_MCP_SERVERS` (JSON list of remote MCP servers for fresh market context).

### Dashboard (`/paper`)
Equity, cash, P&L (realised/unrealised), gains for day / 7d / 30d / all-time as USD and **time-weighted return**
(deposits/withdrawals never count as profit) next to buy-and-hold SOL and BTC, equity chart with day/week/month
selector, trades filterable by venue/asset/strategy (entry→exit, fees, funding, slippage, P&L, reason),
pause per market or per venue, a **"ready for real money?"** checklist (≥28 days of paper, ≥30 trades, TWR > 0
and > hold SOL, max DD ≤ 15%, no kill-switch trips or errors in 7 days), each agent's activity and veto counts, research desk, open positions, closed trades,
decision log with per-agent votes, kill switch, **paper** deposit/withdraw, and **watch-only** wallets.
A plain-language summary at the top says what the bot is doing; the agents are shown as a left-to-right
decision pipeline (green = approved last time, yellow = blocked).
Wallets: **Connect Phantom / Solflare** buttons call the extension's `connect()`, which only shares the public
key (no signature, no fund access), or paste a public address; SOL/SPL balances are read via RPC.
Gold/silver are not traded yet (later phase).

### Security of the local API
State-changing endpoints (start/stop, cycle, kill switch, deposit/withdraw, wallets, research, pause) only
accept loopback clients with a loopback `Host` header (anti DNS-rebinding) **and** an `X-Paper-Token` that the
dashboard fetches same-origin (cross-site pages can't read it ⇒ CSRF-safe). Uvicorn binds 127.0.0.1;
docker-compose publishes `127.0.0.1:8000` only (inside Docker set `PAPER_TRUSTED_CLIENTS` to the bridge IP).

### Funds and wallets — what is and isn't possible now
- Paper deposits/withdrawals are simulated ledger entries.
- Phase 3 (not built): a dedicated bot hot wallet with a small balance; funding = sending USDC/SOL from your
  wallet to its public address; withdrawals only to an allow-listed address with daily limits; real swaps via
  Jupiter only with `LIVE_TRADING=1` **after** 4–8 weeks of 24/7 paper trading that beats hold SOL after costs.

### Running 24/7 on a Mac (`scripts/kairos_service.sh`)
`make service` installs a launchd user agent (`~/Library/LaunchAgents/com.kairos.bots.plist`): it starts at login,
restarts the server within ~15 s if it crashes, and wraps it in `caffeinate -i -s` so the Mac does not idle-sleep.
Logs go to `~/Library/Logs/kairos.log`; `scripts/kairos_service.sh status|restart|logs|uninstall`.
Limits: closing the lid or shutting down still stops the bots (open positions stay protected by their exchange
stop/target orders); for true 24/7 use a small cloud server. The agent runs from this checkout's folder — re-run
`make service` if the project moves.

### Daily report and morning/evening routine
- **Daily report:** dashboard table "Relatório diário" (`GET /api/paper/report/daily?kind=real|sim`) — trades, wins,
  losses, P&L after fees and funding, AI cost, net; a markdown copy is written nightly at 23:55 to `reports/daily/`.
- **Routine skill** (`.claude/skills/trading-routine/`): a read-only review in Portuguese of what traded, what went well
  or badly, gains and losses net of AI, market setups and an honest projection for the day; improvements are only
  proposed, never applied. Data: `.venv/bin/python -m src.solana.routine` (JSON snapshot from the running dashboard +
  public candles). Reports go to `reports/routines/YYYY-MM-DD-manha|noite.md`; scheduled in the Claude app daily at
  11:00 and 22:00 (runs while the app is open). Ask "faz a rotina da manhã" to run it on demand.

### Profitability guards — the AI must earn more than it costs
- **Real cost per call** (`costs.py`, `costs.db`): priced from the usage each API response returns (tokens,
  cache, web searches at $10/1000) with the published per-model rates.
- **Monthly budget** `LLM_MONTHLY_BUDGET_USD` (default $5): once spent, no AI call is made; the rules bot keeps going.
- **Pre-filter** `LLM_PREFILTER=1`: the AI is only asked when there is a dip inside an uptrend; otherwise HOLD for $0.
- **Auto-pause**: an AI bot whose cost exceeds its lab gains by `LLM_BOT_MAX_NET_LOSS_USD` stops; AI bots reach the
  real wallet only when profitable *after* their AI cost.
- **Dashboard "Lucro real"**: trades (fees+funding included) − AI − server (`VPS_MONTHLY_USD`) = net, 30 days and all-time.
- Default team: `rules` (free) + Haiku. With small capital, expensive models cost more than they can earn.

### Strategy v2 (`reports/study_v2_*.md`) — what changed and why
Out-of-sample May→Oct 2026, Hyperliquid BTC/ETH/SOL/HYPE/XRP 1h, taker fees + spread + hourly funding:

| | Return | Max DD | Sharpe | Trades | Win rate |
|---|---:|---:|---:|---:|---:|
| Old mean reversion (fixed % stops, what ran before) | −4.7% | 20.7% | −0.19 | 154 | 49% |
| **Mean reversion ATR + uptrend filter** (walk-forward) | +6.1% | 1.9% | 2.42 | 29 | 76% |
| Same, fixed default parameters (no selection) | +2.5% | 3.0% | 0.82 | 54 | 54% |
| Same, without the uptrend filter | −16.1% | 18.6% | −2.03 | 304 | 47% |
| Trend following | +0.9% | 11.5% | 0.21 | 174 | 40% |
| Buy-and-hold basket of the 5 coins | +24.9% | 22.4% | 1.23 | – | – |

Adopted: `MeanReversionATR` (fixed defaults) for the rules bot and paper; ATR-sized stops (no extra widening);
LLM bots now receive the same indicators (RSI, ATR%, EMA50/200, band, funding, the rules signal, suggested
ATR stops), the round-trip cost and the lesson "no dip-buying in downtrends"; AI bots trade the real wallet only
after `LIVE_MIN_LAB_TRADES` lab trades with positive expectancy. Still below buy-and-hold in absolute return over
a bull window — better risk-adjusted, not yet "beats the market".

### Which strategy, and when (29 coins, 208 days, 1h, fees + funding, per-coin, 2026-10-21)
| Strategy | 1st half | 2nd half | Total |
|---|---:|---:|---:|
| Mean reversion (buy dips in uptrend) | +$2.61 / 101 trades | −$4.70 / 157 | −$2.08 |
| Trend following (breakout) | −$9.77 / 387 | +$12.40 / 721 | +$2.63 |
| Strong trend only (EMA50 +1%/day) | −$8.36 / 244 | +$24.34 / 435 | +$15.98 |
| Both combined | −$1.80 / 473 | +$9.90 / 853 | +$8.10 |

No strategy wins in both halves: mean reversion works in calm markets, trend following in strong trends. So both run
as free lab bots (`rules` = dips, `trend` = strong trend) and the Coordinator gives each market to the bot with the best
**last 30 days** in the lab (`LAB_SCORE_DAYS`); both rule bots trade real money (`REAL_TRUSTED_BOTS=rules,trend`, trend enabled by the user on 2026-10-21); AI bots reach real money only after ≥ 10 profitable lab trades.
Arbitrage is not used: cross-exchange gaps on liquid coins are smaller than two taker fees, and funding/basis trades
need shorts and capital on two venues — not worth it on $20. CoinAPI (`COINAPI_KEY`) is configured but the account has
no credits yet; CoinMarketCap context is used by the morning/evening routine.

### Backtest (walk-forward, out-of-sample Jun→Oct 2026, net of costs)
Params (incl. a SMA200 regime filter on/off) are chosen in-sample on the previous fold and traded on the next.

| Run | Momentum | Mean-reversion | Buy-and-hold |
|---|---:|---:|---|
| Solana tokens, 1h, 60 bps/side | −32.6% | **+4.1%** (11 trades) | SOL +58.9% |
| Hyperliquid perps, 1h, fees+2 bps+funding | −3.7% | **+11.2%** (DD 5.9%, Sharpe 2.1) | BTC +30.0%, ETH +43.9%, SOL +59.3%, GOLD −5.6%, SILVER −16.0% |
| Hyperliquid perps, 15m (52 days) | −7.1% | −6.6% | BTC +6.9%, SOL +3.9%, GOLD −5.8% |

Reading: costs decide everything. Short-term trading (15m) loses; Hyperliquid's ~0.1% round trip makes 1h
mean-reversion positive, Solana's ~1.2% round trip almost kills it. **No strategy beats simply holding SOL/BTC
in this bull window**; mean-reversion beats holding gold/silver. Reports: `reports/backtest_*.md`.

### Known limitations
- Public endpoints are rate limited (Jupiter keyless 429s, public RPC blocks `getTokenLargestAccounts`); the loop
  logs and continues, Safety rejects what it cannot verify. A free RPC/Jupiter key improves coverage.
- Holder concentration comes from Jupiter's audit (third-party) and includes pool vaults.
- Live candles come from GeckoTerminal (~30 req/min) / Hyperliquid; Solana backtest uses Binance and excludes memecoins.
- Hyperliquid paper is 1x long-only; short selling and leverage are deliberately off.
- Live trading exists only on Hyperliquid (1x long-only, small caps); Solana real swaps and withdrawals are not built.
- Sub-hour timeframes were tested and rejected: on 1–15 min candles the typical move is smaller than the round-trip cost.
- Paper fills assume the quoted route would fill; no MEV/landing failures beyond the extra slippage bps.

---

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

There is one dashboard, Kairos, at `http://localhost:8000` (see the dashboard section at the top). The legacy
Polymarket bot has no UI of its own any more; it is controlled through its REST API (`/api/...`, below).

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
Click **Start (auto-trade)** — the bot picks the top opportunity each scan cycle if edge ≥ 5%.
Orders stay dry-run unless the server runs with `POLY_LIVE_TRADING=1` (see the audit before enabling it).

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
    ├── paper.html              # Kairos dashboard (served at /)
    ├── paper.js                # Dashboard logic
    └── style.css               # Base styles
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
