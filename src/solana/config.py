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
            "WLD,ENA,ZRO,ONDO,kPEPE,PUMP")
HL_MARKETS = [m for m in os.getenv("HL_MARKETS", HL_BROAD).split(",") if m] + _METALS

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
ARENA_MODELS = [m for m in os.getenv("ARENA_MODELS", "rules,trend,trend4h,whales,copy,whales2,whales_15m,copy_15m,short,short_break,short_break_15m,rebound_15m").split(",") if m]
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
# fast-lane protection (§6/§12.3): open positions on the real account and its simulation are re-checked (marks,
# protected stops, stop/target/time exits) every EXIT_WATCHDOG_SEC, independently of the 5-minute entry cycle —
# a slow analysis or a delayed cycle never leaves an expired position unmanaged. Deterministic only: the watchdog
# never asks an LLM and never opens anything. 0 = off.
EXIT_WATCHDOG_SEC = int(os.getenv("EXIT_WATCHDOG_SEC", "60"))
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
TRAINING_DB_PATH = os.getenv("TRAINING_DB_PATH", "./training.db")
TEAM_CHAT_DB_PATH = os.getenv("TEAM_CHAT_DB_PATH", "./team_chat.db")    # the bots' group chat (teamchat.py)   # training center samples (see trainer.py)
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
# the meme-coin trench (meme.py): lab only, paper money, real Solana prices
MEME_ENABLED = os.getenv("MEME_ENABLED", "1") == "1"
MEME_CYCLE_SEC = _i("MEME_CYCLE_SEC", 60)
# "no traction" exit: a meme that has not shown +MEME_TRACTION_PCT of peak gain after MEME_TRACTION_MIN minutes is cut
# (data: every winner was +3% inside 10 min; the six stop-outs lived 7–120 min and ended −12…−56%)
MEME_TRACTION_MIN = _f("MEME_TRACTION_MIN", 15)
MEME_TRACTION_PCT = _f("MEME_TRACTION_PCT", 0.03)
# bad-tick guard: a price that jumps more than this (fraction) from the last mark in one tick is only believed once the
# next tick shows it again (a single wrong DexScreener quote halved WBTC and booked a fake −50% stop on 2026-10-22)
MEME_JUMP_GUARD = _f("MEME_JUMP_GUARD", 0.30)
MEME_WATCH_SEC = _i("MEME_WATCH_SEC", 15)             # open meme positions are re-priced this often (memes gap in minutes)
MEME_SAFE_ROUNDS = _i("MEME_SAFE_ROUNDS", 2)          # a token must pass the safety checks on this many consecutive rounds
# stop-loss guard (Freqtrade's most-used protection): a bot stopped out STOPLOSS_GUARD_N times within
# STOPLOSS_GUARD_WINDOW_H pauses its entries for STOPLOSS_GUARD_PAUSE_H — the market is not in its regime right now
STOPLOSS_GUARD_N = _i("STOPLOSS_GUARD_N", 3)
STOPLOSS_GUARD_WINDOW_H = _f("STOPLOSS_GUARD_WINDOW_H", 4)
STOPLOSS_GUARD_PAUSE_H = _f("STOPLOSS_GUARD_PAUSE_H", 6)
MEME_CAPITAL_USD = _f("MEME_CAPITAL_USD", 100.0)
MEME_TRADE_USD = _f("MEME_TRADE_USD", 10.0)
MEME_MAX_POSITIONS = _i("MEME_MAX_POSITIONS", 4)
MEME_STOP_PCT = _f("MEME_STOP_PCT", 0.12)            # was 18%: the 6 stops averaged −26% (gaps); winners popped within 10 min
MEME_TP_PCT = _f("MEME_TP_PCT", 0.40)
MEME_MAX_HOLD_H = _f("MEME_MAX_HOLD_H", 2)
# every trade, crypto or meme, is short: at most MAX_HOLD_CAP_H (the owner's rule — trades can last 30s or 2h, never a
# buy-and-hold); the quick-win locks bank the fast moves before that. FOCUS_COINS are scanned first each cycle.
MAX_HOLD_CAP_H = _f("MAX_HOLD_CAP_H", 2)
# §5.5 holding policy, lab side: slower families (trend 24h, trend4h 48h, copy 12h) run their OWN horizon in the
# lab up to this cap, building the evidence longer holds need; the 15m family keeps the scalp clock. Real money
# stays under MAX_HOLD_CAP_H until that evidence exists.
LAB_HOLD_CAP_H = _f("LAB_HOLD_CAP_H", 48.0)
# crypto "no traction" exit: 149 time exits lost $9.15 (27% wins); trades held past 2h won 24%, under 1h 57%. After
# STALE_EXIT_H without clearing +STALE_EXIT_PCT over entry the position is closed instead of waiting for the cap.
STALE_EXIT_H = _f("STALE_EXIT_H", 1.0)
STALE_EXIT_PCT = _f("STALE_EXIT_PCT", 0.002)
FOCUS_COINS = tuple(c.strip().upper() for c in os.getenv("FOCUS_COINS", "ZEC,PUMP").split(",") if c.strip())
# universe filter (passivbot forager / Freqtrade VolumePairList): each hour the rule bots scan only the UNIVERSE_SIZE
# coins with the most 24h volume × 1h log-range volatility (plus FOCUS_COINS and any coin already held). 0 = all 30.
UNIVERSE_SIZE = _i("UNIVERSE_SIZE", 12)
# stop distance = ATR_STOP_MULT × ATR(14) of the signal's candles, bounded to [0.5×, 1.5×] the strategy's own stop
# (strategies.atr_stop). 0 = the strategy's stop as is.
ATR_STOP_MULT = _f("ATR_STOP_MULT", 1.5)
# meme quick wins: memes spike and dump in minutes, so any sudden gain is banked — the stop locks a share of the peak gain
# as soon as it clears the round trip's cost (0.5% fee per side + impact): +4% locks 50%, +8% locks 65%, +15% locks 75%,
# +30% locks 80%. The lock never sits below entry + fees, so a locked exit is always a net gain.
MEME_QUICK_WINS = [(0.04, 0.5), (0.08, 0.65), (0.15, 0.75), (0.30, 0.8)]
# market regime for memes (memes are high-beta SOL): no meme buys while SOL or BTC fell more than MEME_REGIME_1H_PCT
# in the last closed hour; half the trade size while either is down more than MEME_REGIME_4H_PCT in 4h
MEME_REGIME_1H_PCT = _f("MEME_REGIME_1H_PCT", -0.02)
MEME_REGIME_4H_PCT = _f("MEME_REGIME_4H_PCT", -0.04)
MEME_WHALE_TOKENS = _i("MEME_WHALE_TOKENS", 12)       # tokens whose big trades are watched (GeckoTerminal)
MEME_WHALE_PER_ROUND = _i("MEME_WHALE_PER_ROUND", 4)  # of those, how many are re-read each round (rate limit)
# reserves (lab, fake money) train aggressively to learn faster: crypto reserves use the loosest entry level (the one a
# titular only gets when behind its daily quota); meme reserves get wider signal windows and more open positions.
RESERVE_RELAX = _i("RESERVE_RELAX", 2)
MEME_RESERVE_MAX_POSITIONS = _i("MEME_RESERVE_MAX_POSITIONS", 6)
MEME_LINEAGE_PATH =os.getenv("MEME_LINEAGE_PATH", "meme_lineage.json")   # the meme team's roster and its retired bots
# real meme account (meme_wallet.py): the bot wallet on Solana, separate from Hyperliquid. "ensaio" = dry run (quotes and
# transactions simulated, nothing sent); "real" sends — only for the meme titulars, and only with the owner's Phantom set.
MEME_REAL_ENABLED = os.getenv("MEME_REAL_ENABLED", "1") == "1"
MEME_REAL_MODE = os.getenv("MEME_REAL_MODE", "ensaio")
# bot wallet key, mode 0600, outside the repo: a worktree cleanup must never take the key (and the money) with it
MEME_WALLET_PATH = os.getenv("MEME_WALLET_PATH", os.path.expanduser("~/.kairos/meme_wallet.json"))
MEME_REAL_DB_PATH = os.getenv("MEME_REAL_DB_PATH", "meme_real.db")
MEME_REAL_DRY_CAPITAL_USD = _f("MEME_REAL_DRY_CAPITAL_USD", 25.0)
MEME_REAL_TRADE_USD = _f("MEME_REAL_TRADE_USD", 3.0)
MEME_REAL_MAX_POSITIONS = _i("MEME_REAL_MAX_POSITIONS", 3)       # shared by the meme and Solana crypto teams: ≤ $15 exposed
MEME_REAL_MAX_BUYS_DAY = _i("MEME_REAL_MAX_BUYS_DAY", 12)
MEME_REAL_DAILY_LOSS_USD = _f("MEME_REAL_DAILY_LOSS_USD", 5.0)
MEME_REAL_MAX_IMPACT_PCT = _f("MEME_REAL_MAX_IMPACT_PCT", 2.0)
MEME_REAL_SLIPPAGE_BPS = _i("MEME_REAL_SLIPPAGE_BPS", 300)
MEME_REAL_MAX_ENTRY_AGE_S = _i("MEME_REAL_MAX_ENTRY_AGE_S", 180)    # the real account only follows lab entries this fresh
# real-account shorts (Hyperliquid): OFF. Phase 1 is only the machinery; it stays unreachable until the owner sets
# LIVE_ALLOW_SHORT=1 (and a short bot has earned the green light in the lab). Small limits for when it is on:
# X.com posts (xfeed.py): pay-per-use, $0.005 per post returned. HARD daily cap in dollars; one read of X_MAX_RESULTS posts
# at most every X_POLL_MIN minutes, so the default (≈ $0.25/day) is about 5 reads of 10 posts a day.
X_DAILY_BUDGET_USD = _f("X_DAILY_BUDGET_USD", 0.25)
X_POLL_MIN = _i("X_POLL_MIN", 120)
X_MAX_RESULTS = _i("X_MAX_RESULTS", 10)
X_STATE_PATH = os.getenv("X_STATE_PATH", "x_feed.json")
# small-profit exit ("see a bit of profit and sell"): a position up SCALP_TP_PCT over its cost basis (fees included) is
# closed at once. 0 disables. Reserves in the lab also skip the BTC-below-average buy filter: they must trade to learn
# (titulars, and everything on the real account, keep the filter).
SCALP_TP_PCT = _f("SCALP_TP_PCT", 0.01)
# lab-only scalp mode (2026-10-22 data: win 22-40%, avg win +0.7% vs avg loss -1.1%, 2h cap killed trades): lab bots cap the stop
# and target at these distances and exit after LAB_SCALP_HOLD_H. The real account (no lab_model) is untouched. 0 disables each.
LAB_SCALP_STOP_PCT = _f("LAB_SCALP_STOP_PCT", 0.006)
LAB_SCALP_TP_PCT = _f("LAB_SCALP_TP_PCT", 0.008)
LAB_SCALP_HOLD_H = _f("LAB_SCALP_HOLD_H", 0.5)
# the 15-minute bots may trade the REAL account even while BTC is below its 50h average (owner's decision 2026-10-22):
# long only, LIVE_FAST_USD per trade, at most LIVE_FAST_MAX_OPEN open at once. 0 turns it off.
LIVE_FAST_ALLOW = os.getenv("LIVE_FAST_ALLOW", "1") == "1"
LIVE_FAST_BOTS = {m.strip() for m in os.getenv("LIVE_FAST_BOTS", "whales_15m,copy_15m,trend4h_15m").split(",") if m.strip()}   # explicit list: a new fast bot never gets real money by default
LIVE_FAST_USD = _f("LIVE_FAST_USD", 10.0)
LIVE_FAST_MAX_OPEN = _i("LIVE_FAST_MAX_OPEN", 1)
LIVE_ALLOW_SHORT = os.getenv("LIVE_ALLOW_SHORT", "0") == "1"
LIVE_SHORT_MAX_OPEN = _i("LIVE_SHORT_MAX_OPEN", 1)
LIVE_SHORT_MAX_PER_DAY = _i("LIVE_SHORT_MAX_PER_DAY", 3)
LIVE_SHORT_DAILY_LOSS_USD = _f("LIVE_SHORT_DAILY_LOSS_USD", 1.0)
LIVE_SHORT_MAX_STOP_PCT = _f("LIVE_SHORT_MAX_STOP_PCT", 0.03)
MEME_REAL_KILL_PCT = _f("MEME_REAL_KILL_PCT", 0.6)                 # equity below 60% of deposits: sell all and halt
MEME_REAL_MAX_PRIORITY_LAMPORTS = _i("MEME_REAL_MAX_PRIORITY_LAMPORTS", 60_000)   # ≤ 0.00006 SOL (~$0.006) priority fee: 0.2% of a $3 trade
# margin of error before a buy (edge.py): skip only when confident (this much evidence) that even the optimistic odds lose
EDGE_CHECK = os.getenv("EDGE_CHECK", "1") == "1"
EDGE_MIN_EVIDENCE = _i("EDGE_MIN_EVIDENCE", 30)
# daily quota: a titular behind DAILY_MIN_TRADES completed trades (Lisbon day, pro rata). Observational by default
# (logged and shown, never changes entries): forcing trades to hit a count is an incentive against profitability.
# QUOTA_RELAX_ENTRIES=1 restores the old behaviour (looser entry + closer target) as a rollback switch.
DAILY_MIN_TRADES = _i("DAILY_MIN_TRADES", 5)
QUOTA_RELAX_ENTRIES = os.getenv("QUOTA_RELAX_ENTRIES", "0") == "1"
# manual promotion from the bot's modal (the user's rule): this many lab wins and at least this win rate
PROMOTE_MIN_WINS = _i("PROMOTE_MIN_WINS", 12)
PROMOTE_MIN_WIN_RATE = _f("PROMOTE_MIN_WIN_RATE", 0.35)
# evolution: every EVOLVE_PERIOD_H of a reserve's life, under EVOLVE_MIN_WIN_RATE -> replaced by a successor.
# An EMPTY window is NOT a fail (§8.5: waiting for its regime is healthy — punishing it breeds overtrading): a
# reserve is only retired as "blind" after EVOLVE_IDLE_PERIODS consecutive empty windows while the rest of the
# team closed ≥ EVOLVE_IDLE_TEAM_TRADES trades in that stretch (opportunities existed; this bot saw none).
EVOLVE_PERIOD_H = _f("EVOLVE_PERIOD_H", 12)
EVOLVE_IDLE_PERIODS = _i("EVOLVE_IDLE_PERIODS", 6)
EVOLVE_IDLE_TEAM_TRADES = _i("EVOLVE_IDLE_TEAM_TRADES", 10)
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
# second tier, what worked in the user's manual sells (+0.5% to +1%): from +1.2% the stop locks 80% of the gain, so the
# trade is sold on the first pullback (a half sale is impossible: Hyperliquid's minimum order is $10, positions are $10)
QUICK_WIN2_PCT = _f("QUICK_WIN2_PCT", 0.012)
QUICK_WIN2_KEEP = _f("QUICK_WIN2_KEEP", 0.8)
# no buys while BTC is below its 50-hour average (when BTC falls almost every coin falls with it)
BTC_FILTER = os.getenv("BTC_FILTER", "1") == "1"
# ─── P1 Market Intelligence ───
# regime classifier (regime.py): OBSERVATIONAL — logged, shown and tagged on every entry so its efficacy can be
# compared against the BTC-50h filter on real trades; it gates nothing yet
REGIME_ENABLED = os.getenv("REGIME_ENABLED", "1") == "1"
# correlated-exposure guard: a new entry whose last CORR_WINDOW_H 1h returns correlate ≥ CORR_MAX_ENTRY with an
# already-open same-direction position is economically redundant (no real diversification) and is skipped. 0 = off.
CORR_MAX_ENTRY = _f("CORR_MAX_ENTRY", 0.92)
CORR_WINDOW_H = _i("CORR_WINDOW_H", 48)
# on the real/simulation accounts, one titular may hold at most this many positions (room for the others)
MAX_POSITIONS_PER_BOT = _i("MAX_POSITIONS_PER_BOT", 3)
# no burst of buys into one move: at most this many new buys per BURST_WINDOW_MIN on an account (2026-10-21 19h Lisbon:
# 35 lab buys in one hour, 0 wins, −$3.63 — everyone bought the same top)
BURST_MAX_ENTRIES = _i("BURST_MAX_ENTRIES", 2)
BURST_WINDOW_MIN = _i("BURST_WINDOW_MIN", 30)
BOT_SELF_PAUSE = os.getenv("BOT_SELF_PAUSE", "0") == "1"
BOT_MAX_SLEEP_MIN = _i("BOT_MAX_SLEEP_MIN", 120)
# an AI bot may act on the REAL wallet only after this many lab trades with positive expectancy
LIVE_MIN_LAB_TRADES = _i("LIVE_MIN_LAB_TRADES", 10)
BOT_WAKE_MOVE_PCT = _f("BOT_WAKE_MOVE_PCT", 0.015)
ARENA_CYCLE_SEC = _i("ARENA_CYCLE_SEC", 300)
TURBO_CYCLE_SEC = _i("TURBO_CYCLE_SEC", 120)        # a bot in "turbo do dia" analyses this often (1 min caused 429s)
# a bot trained with turbo closes positions sooner (lab and, once promoted, real account): more round trips per day
TURBO_FAST_EXITS = os.getenv("TURBO_FAST_EXITS", "0") == "1"   # off: forced 2h exits closed trades before their target
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
# daily brake on top of the monthly allowance: on 2026-10-21 one day burnt $1.77 (889 calls), 79% of the month.
# 0 = off. The rules bots keep trading for free when AI is paused.
AI_DAILY_BUDGET_USD = _f("AI_DAILY_BUDGET_USD", 0.25)
# web search inflates every call (search results + $0.01/search); off unless it proves worth it
LLM_WEB_SEARCHES = _i("LLM_WEB_SEARCHES", 0)
LLM_PREFILTER = os.getenv("LLM_PREFILTER", "1") == "1"           # ask the AI only when there is a real setup
LLM_BOT_MAX_NET_LOSS_USD = _f("LLM_BOT_MAX_NET_LOSS_USD", 1.0)   # pause an AI bot whose cost exceeds its gains by this
VPS_MONTHLY_USD = _f("VPS_MONTHLY_USD", 0.0)                     # server cost, counted in net profit
