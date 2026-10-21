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
PAPER_STRATEGIES = [s for s in os.getenv("PAPER_STRATEGIES", "mean_reversion").split(",") if s]

# Optional LLM debate (veto-only). Disabled without a key.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEBATE_MODEL = os.getenv("DEBATE_MODEL", "claude-opus-5-5")
# JSON list of remote MCP servers the debate LLM may use for fresh context, e.g.
# [{"url": "https://mcp.example.com/mcp", "name": "market", "authorization_token": "..."}]
DEBATE_MCP_SERVERS = os.getenv("DEBATE_MCP_SERVERS", "")

# ─── Hyperliquid PAPER venue (public Info API only) ───
HYPERLIQUID_INFO_URL = os.getenv("HYPERLIQUID_INFO_URL", "https://api.hyperliquid.xyz/info")
HL_ENABLED = os.getenv("HL_ENABLED", "1") == "1"
HL_MARKETS = [m for m in os.getenv("HL_MARKETS", "BTC,ETH,SOL,xyz:GOLD,xyz:SILVER").split(",") if m]
HL_ALLOW_SHORT = False  # long-only in phase 1

# Per-venue risk profile: Solana tokens get a small slice, majors/metals a bigger one with wider stops.
# Override with JSON, e.g. MARKET_PROFILES='{"hyperliquid": {"max_pos_pct": 0.2, "stop_mult": 2}}'
import json as _json
MARKET_PROFILES = {
    "jupiter": {"max_pos_pct": 0.05, "stop_mult": 1.0},
    "hyperliquid": {"max_pos_pct": 0.15, "stop_mult": 1.5},
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
LIVE_DB_PATH = os.getenv("LIVE_DB_PATH", "./live.db")
LIVE_CYCLE_SEC = _i("LIVE_CYCLE_SEC", 300)
LIVE_MAX_CAPITAL_USD = _f("LIVE_MAX_CAPITAL_USD", 20.0)        # the bot never sizes off more than this
LIVE_MAX_POSITION_USD = _f("LIVE_MAX_POSITION_USD", 12.0)      # Hyperliquid minimum order is $10
LIVE_MAX_POSITIONS = _i("LIVE_MAX_POSITIONS", 1)
LIVE_MAX_DAILY_LOSS_PCT = _f("LIVE_MAX_DAILY_LOSS_PCT", 0.10)
LIVE_MAX_ENTRIES_PER_DAY = _i("LIVE_MAX_ENTRIES_PER_DAY", 3)
HL_LIVE_MARKETS = [m for m in os.getenv("HL_LIVE_MARKETS", "BTC,ETH,SOL,xyz:GOLD,xyz:SILVER").split(",") if m]

# ─── Model arena: one paper bot per LLM, same markets and risk limits, separate portfolios ───
ARENA_MODELS = [m for m in os.getenv("ARENA_MODELS", "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5").split(",") if m]
ARENA_START_USDC = _f("ARENA_START_USDC", 100.0)
ARENA_CYCLE_SEC = _i("ARENA_CYCLE_SEC", 900)

# ─── Simulation of the real account: same limits/markets as live, real mainnet books, fake money ───
SHADOW_DB_PATH = os.getenv("SHADOW_DB_PATH", "./shadow.db")
SHADOW_START_USDC = _f("SHADOW_START_USDC", 20.0)
