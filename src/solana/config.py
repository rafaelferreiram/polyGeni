"""Paper-trading settings. Conservative defaults, all overridable by env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


PAPER_DB_PATH = os.getenv("PAPER_DB_PATH", "./paper.db")
PAPER_START_USDC = _f("PAPER_START_USDC", 100.0)
PAPER_CYCLE_SEC = _i("PAPER_CYCLE_SEC", 300)
PAPER_AUTOSTART = os.getenv("PAPER_AUTOSTART", "0") == "1"

# Simulated execution costs on top of the real Jupiter quote
PAPER_SLIPPAGE_BPS = _i("PAPER_SLIPPAGE_BPS", 30)        # extra adverse slippage (latency/MEV)
PAPER_QUOTE_SLIPPAGE_BPS = _i("PAPER_QUOTE_SLIPPAGE_BPS", 100)  # slippageBps sent to Jupiter
PAPER_BASE_FEE_LAMPORTS = _i("PAPER_BASE_FEE_LAMPORTS", 5000)
PAPER_PRIORITY_FEE_LAMPORTS = _i("PAPER_PRIORITY_FEE_LAMPORTS", 100_000)

# Endpoints (public, keyless; verified 2026-10)
JUPITER_QUOTE_URL = os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
JUPITER_TOKENS_URL = os.getenv("JUPITER_TOKENS_URL", "https://api.jup.ag/tokens/v2/search")
DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Scout
SCOUT_MIN_LIQUIDITY_USD = _f("SCOUT_MIN_LIQUIDITY_USD", 250_000)
SCOUT_MIN_VOLUME_24H_USD = _f("SCOUT_MIN_VOLUME_24H_USD", 500_000)
SCOUT_MAX_CANDIDATES = _i("SCOUT_MAX_CANDIDATES", 8)
# Established Solana tokens always considered (JUP, BONK, WIF, RAY, JTO, PYTH)
SCOUT_WATCHLIST = [m for m in os.getenv("SCOUT_WATCHLIST", ",".join([
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
])).split(",") if m]

# Safety (rejects by default)
SAFETY_MIN_PAIR_AGE_H = _f("SAFETY_MIN_PAIR_AGE_H", 24)
SAFETY_MIN_LIQUIDITY_USD = _f("SAFETY_MIN_LIQUIDITY_USD", 250_000)
SAFETY_MAX_TOP10_PCT = _f("SAFETY_MAX_TOP10_PCT", 0.60)

# Risk (deterministic, final veto)
RISK_MAX_POSITION_PCT = _f("RISK_MAX_POSITION_PCT", 0.10)
RISK_MAX_EXPOSURE_PCT = _f("RISK_MAX_EXPOSURE_PCT", 0.50)
RISK_MAX_POSITIONS = _i("RISK_MAX_POSITIONS", 4)
RISK_MAX_DAILY_LOSS_PCT = _f("RISK_MAX_DAILY_LOSS_PCT", 0.05)
RISK_MIN_TRADE_USD = _f("RISK_MIN_TRADE_USD", 5.0)
RISK_MAX_HOLD_H = _f("RISK_MAX_HOLD_H", 72)
RISK_MAX_ENTRIES_PER_DAY = _i("RISK_MAX_ENTRIES_PER_DAY", 6)
STALE_CANDLE_H = _f("STALE_CANDLE_H", 3)   # refuse signals computed on candles older than this
# study_v2 (reports/): ATR-sized mean reversion with an uptrend filter beat the old fixed-% version OOS
PAPER_STRATEGIES = [s for s in os.getenv("PAPER_STRATEGIES", "mean_reversion_atr").split(",") if s]

# Optional LLM debate (veto-only). Disabled without a key.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# veto-only reviewer of every rules signal: Haiku (≈ $0.001/call) — Opus with web search cost ≈ $0.10/call
DEBATE_MODEL = os.getenv("DEBATE_MODEL", "claude-haiku-5-5")
# JSON list of remote MCP servers the debate LLM may use for fresh context, e.g.
# [{"url": "https://mcp.example.com/mcp", "name": "market", "authorization_token": "..."}]
DEBATE_MCP_SERVERS = os.getenv("DEBATE_MCP_SERVERS", "")

# ─── Hyperliquid PAPER venue (public Info API only) ───
HYPERLIQUID_INFO_URL = os.getenv("HYPERLIQUID_INFO_URL", "https://api.hyperliquid.xyz/info")
HL_ENABLED = os.getenv("HL_ENABLED", "1") == "1"
# crypto first (more volatility); gold/silver are opt-in with HL_INCLUDE_METALS=1
HL_INCLUDE_METALS = os.getenv("HL_INCLUDE_METALS", "0") == "1"
_METALS = ["xyz:GOLD", "xyz:SILVER"] if HL_INCLUDE_METALS else []
# broad crypto set for simulation, lab and real account: CoinMarketCap top-50 by market cap plus Hyperliquid's most
# traded perps, each listed on Hyperliquid with ≥ 208 days of history and ≥ $1M daily volume (checked 2026-10-21).
# The bots scan all of it every cycle and take the best setup; risk limits are per position, not per coin.
HL_BROAD = ("BTC,ETH,SOL,HYPE,XRP,BNB,DOGE,ADA,TRX,AVAX,LINK,LTC,BCH,XLM,HBAR,DOT,SUI,NEAR,AAVE,UNI,ARB,TAO,ZEC,XMR,"
            "WLD,ENA,ZRO,ONDO,kPEPE")
HL_MARKETS = [m for m in os.getenv("HL_MARKETS", HL_BROAD).split(",") if m] + _METALS
HL_ALLOW_SHORT = False  # long-only in phase 1

# Per-venue risk profile: Solana tokens get a small slice, majors/metals a bigger one with wider stops.
# Override with JSON, e.g. MARKET_PROFILES='{"hyperliquid": {"max_pos_pct": 0.2, "stop_mult": 2}}'
import json as _json
MARKET_PROFILES = {
    "jupiter": {"max_pos_pct": 0.05, "stop_mult": 1.0},
    "hyperliquid": {"max_pos_pct": 0.15, "stop_mult": 1.0},   # stops are ATR-sized now; no extra widening
    **_json.loads(os.getenv("MARKET_PROFILES", "{}")),
}

# Watch-only EVM wallets (public reads only)
ARBITRUM_RPC_URL = os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
ARBITRUM_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"   # native USDC on Arbitrum One
HYPERLIQUID_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"

# ─── Hyperliquid LIVE (real orders). Testnet by default; mainnet needs LIVE_TRADING=1 as well. ───
HL_NETWORK = os.getenv("HL_NETWORK", "testnet")                 # testnet | mainnet
HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "")       # your MetaMask 0x address (public)
# HL_API_WALLET_KEY is read directly from the environment by hl_live and never stored in config/logs
LIVE_TRADING_ALLOWED = os.getenv("LIVE_TRADING", "0") == "1"
# one ledger per network, so test money never mixes with real money in P&L or reports
LIVE_DB_PATH = os.getenv("LIVE_DB_PATH", f"./live_{HL_NETWORK}.db")
LIVE_CYCLE_SEC = _i("LIVE_CYCLE_SEC", 300)
LIVE_MAX_CAPITAL_USD = _f("LIVE_MAX_CAPITAL_USD", 20.0)        # the bots never size off more than this (raise it as the wallet grows)
LIVE_MAX_POSITION_USD = _f("LIVE_MAX_POSITION_USD", 12.0)      # Hyperliquid minimum order is $10
# isolated leverage for the real account, its simulation and the lab (max 3): 2x lets $18 hold up to 3 positions of
# ~$12 at once. Every position keeps its own exchange stop and target; daily loss and kill switch are on equity.
LIVE_LEVERAGE = min(max(_f("LIVE_LEVERAGE", 2.0), 1.0), 3.0)
LIVE_MAX_POSITIONS = _i("LIVE_MAX_POSITIONS", 0)               # 0 = auto: capital // position size (max 6)
LIVE_MAX_DAILY_LOSS_PCT = _f("LIVE_MAX_DAILY_LOSS_PCT", 0.10)
LIVE_MAX_ENTRIES_PER_DAY = _i("LIVE_MAX_ENTRIES_PER_DAY", 12)   # trade whenever there is a valid signal; the real brakes are
#   max simultaneous positions, stops, and the 10% daily loss kill switch (coffee doubles this to 24)
HL_LIVE_MARKETS = [m for m in os.getenv("HL_LIVE_MARKETS", HL_BROAD).split(",") if m] + _METALS

# ─── Model arena: one paper bot per LLM, same markets and risk limits, separate portfolios ───
# "rules" = deterministic baseline bot (no LLM, no API cost) that every model must beat
# default: free rules bot + the cheap Haiku; add Sonnet/Opus/Fable when capital makes their cost worthwhile
ARENA_MODELS = [m for m in os.getenv("ARENA_MODELS", "rules,trend,trend4h,whales,copy,whales2,whales_15m,copy_15m").split(",") if m]
TOP_TRADERS_ENABLED = os.getenv("TOP_TRADERS_ENABLED", "1") == "1"   # follow the top 10 Hyperliquid traders (read-only)
WHALES_ENABLED = os.getenv("WHALES_ENABLED", "1") == "1"      # live whale-trade feed (WebSocket, read-only)
WHALE_MIN_USD = float(os.getenv("WHALE_MIN_USD", "25000"))    # whale = ≥ this and ≥ 0.02% of the coin's daily volume
# daily goal: once the account is up DAILY_GOAL_PCT on the day, the bots rest (no new entries) until tomorrow and keep
# managing open positions — a good day is banked instead of given back. 2% is a good day for day trading at 2x;
# an average day is lower. Goals are never a reason to force a trade.
DAILY_GOAL_PCT = float(os.getenv("DAILY_GOAL_PCT", "0.02"))
DAILY_GOAL_REST = os.getenv("DAILY_GOAL_REST", "1") == "1"
DAILY_GOAL_TRADES = int(os.getenv("DAILY_GOAL_TRADES", "4"))
SYNC_FAIL_LIMIT = int(os.getenv("SYNC_FAIL_LIMIT", "6"))   # consecutive unreachable-exchange cycles before the kill switch
# bots allowed to spend real money without first proving themselves in the lab (validated by backtest; "trend"
# enabled by the user on 2026-10-21). Any other bot needs LIVE_MIN_LAB_TRADES profitable lab trades.
REAL_TRUSTED_BOTS = [m for m in os.getenv("REAL_TRUSTED_BOTS", "rules,trend").split(",") if m]
LAB_SCORE_DAYS = float(os.getenv("LAB_SCORE_DAYS", "30"))   # Coordinator ranks bots on their last N days in the lab
ARENA_START_USDC = _f("ARENA_START_USDC", 20.0)     # each bot gets what the real account would have
# the lab proves bots faster with more paper capital and slots: same position size, stop and target as the real
# account (so each trade is valid evidence), but up to LAB_MAX_POSITIONS at once and no low daily entry cap
# the user's timezone (Lisbon): daily reports, goals, messages and the dashboard all use it
from zoneinfo import ZoneInfo
TZ = ZoneInfo(os.getenv("KAIROS_TZ", "Europe/Lisbon"))
TRAINING_DB_PATH = os.getenv("TRAINING_DB_PATH", "./training.db")   # training center samples (see trainer.py)
LAB_CAPITAL_USD = _f("LAB_CAPITAL_USD", 100.0)
LAB_MAX_POSITIONS = _i("LAB_MAX_POSITIONS", 8)
LAB_MAX_ENTRIES_PER_DAY = _i("LAB_MAX_ENTRIES_PER_DAY", 48)
# "treino intensivo": one slot per coin and more paper capital for 24h, so slots never limit how many lab trades happen
TRAIN_BOOST_MAX_POSITIONS = _i("TRAIN_BOOST_MAX_POSITIONS", 29)
TRAIN_BOOST_CAPITAL_USD = _f("TRAIN_BOOST_CAPITAL_USD", 400.0)   # fake money: 29 slots x ~$12 fit
# green light to real money (bots not in REAL_TRUSTED_BOTS): at least GREEN_LIGHT_TRADES closed lab trades with
# GREEN_LIGHT_WINS winners, net profit after AI cost, win rate and profit factor at least these (user's rule: 20 trades, 12 wins)
# self-learning: a bot stops entering situations its own last LEARN_DAYS of trades keep losing in (it only skips trades)
SELF_LEARNING = os.getenv("SELF_LEARNING", "1") == "1"
LEARN_DAYS = _i("LEARN_DAYS", 30)
LEARN_MIN_TRADES = _i("LEARN_MIN_TRADES", 6)
LEARN_MAX_WIN_RATE = _f("LEARN_MAX_WIN_RATE", 0.35)
MAX_TITULARES = _i("MAX_TITULARES", 6)
# daily quota: a titular behind DAILY_MIN_TRADES completed trades (Lisbon day, pro rata) loosens its entry a notch or two
# and aims at a closer target, so it completes buy+sell round trips; size, stops and the account limits do not change
DAILY_MIN_TRADES = _i("DAILY_MIN_TRADES", 5)
# evolution: every EVOLVE_PERIOD_H of a reserve's life, under EVOLVE_MIN_WIN_RATE (or no trade) -> replaced by a successor
EVOLVE_PERIOD_H = _f("EVOLVE_PERIOD_H", 20)
EVOLVE_MIN_WIN_RATE = _f("EVOLVE_MIN_WIN_RATE", 0.6)          # the real account fields at most this many bots: the best ones
GREEN_LIGHT_TRADES = _i("GREEN_LIGHT_TRADES", 20)
GREEN_LIGHT_WINS = _i("GREEN_LIGHT_WINS", 12)
GREEN_LIGHT_WIN_RATE = _f("GREEN_LIGHT_WIN_RATE", 0.6)
GREEN_LIGHT_PROFIT_FACTOR = _f("GREEN_LIGHT_PROFIT_FACTOR", 1.2)
# self-pause: a bot with nothing to do sleeps (up to BOT_MAX_SLEEP_MIN) and wakes early if price moves.
# Off by default: the bots watch the market every cycle 24/7 (the AI pre-filter already keeps idle cycles free).
PROTECT_PROFIT = os.getenv("PROTECT_PROFIT", "1") == "1"   # breakeven + trailing stop once a trade is halfway to target
# quick wins: once a trade is up QUICK_WIN_PCT, the stop locks QUICK_WIN_KEEP of the gain (never below entry + fees),
# so a small win is banked if the price turns, while the target can still be reached
QUICK_WIN_PCT = _f("QUICK_WIN_PCT", 0.006)
QUICK_WIN_KEEP = _f("QUICK_WIN_KEEP", 0.5)
BOT_SELF_PAUSE = os.getenv("BOT_SELF_PAUSE", "0") == "1"
BOT_MAX_SLEEP_MIN = _i("BOT_MAX_SLEEP_MIN", 120)
# an AI bot may act on the REAL wallet only after this many lab trades with positive expectancy
LIVE_MIN_LAB_TRADES = _i("LIVE_MIN_LAB_TRADES", 10)
BOT_WAKE_MOVE_PCT = _f("BOT_WAKE_MOVE_PCT", 0.015)
ARENA_CYCLE_SEC = _i("ARENA_CYCLE_SEC", 300)
TURBO_CYCLE_SEC = _i("TURBO_CYCLE_SEC", 60)          # a bot in "turbo do dia" analyses this often
# a bot trained with turbo closes positions sooner (lab and, once promoted, real account): more round trips per day
TURBO_MAX_HOLD_H = _f("TURBO_MAX_HOLD_H", 2.0)
TURBO_FAST_MAX_HOLD_H = _f("TURBO_FAST_MAX_HOLD_H", 1.0)   # 15-minute twins          # lab bots scan with the accounts (candles are shared, cheap)

# ─── Simulation of the real account: same limits/markets as live, real mainnet books, fake money ───
SHADOW_DB_PATH = os.getenv("SHADOW_DB_PATH", "./shadow.db")
SHADOW_START_USDC = _f("SHADOW_START_USDC", 20.0)

# ─── Profitability guards: AI must earn more than it costs ───
COSTS_DB_PATH = os.getenv("COSTS_DB_PATH", "./costs.db")
LLM_MONTHLY_BUDGET_USD = _f("LLM_MONTHLY_BUDGET_USD", 5.0)      # hard cap on AI spend per calendar month
# AI must pay for itself: this month it may spend AI_BASE_MONTHLY_USD (to keep learning) plus AI_PROFIT_SHARE of the
# real account's trading profit this month, never more than LLM_MONTHLY_BUDGET_USD. Over it, only rule bots trade.
AI_BASE_MONTHLY_USD = _f("AI_BASE_MONTHLY_USD", 1.5)
AI_PROFIT_SHARE = _f("AI_PROFIT_SHARE", 0.25)
# web search inflates every call (search results + $0.01/search); off unless it proves worth it
LLM_WEB_SEARCHES = _i("LLM_WEB_SEARCHES", 0)
LLM_PREFILTER = os.getenv("LLM_PREFILTER", "1") == "1"           # ask the AI only when there is a real setup
LLM_BOT_MAX_NET_LOSS_USD = _f("LLM_BOT_MAX_NET_LOSS_USD", 1.0)   # pause an AI bot whose cost exceeds its gains by this
VPS_MONTHLY_USD = _f("VPS_MONTHLY_USD", 0.0)                     # server cost, counted in net profit
