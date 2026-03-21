from dotenv import load_dotenv
import os

load_dotenv()

POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_WALLET_ADDRESS = os.getenv("POLY_WALLET_ADDRESS", "")

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

BOT_BUDGET_USDC = float(os.getenv("BOT_BUDGET_USDC", "10.0"))
BOT_MAX_POSITION_PCT = float(os.getenv("BOT_MAX_POSITION_PCT", "0.30"))
BOT_KELLY_FRACTION = float(os.getenv("BOT_KELLY_FRACTION", "0.25"))
BOT_MIN_EDGE = float(os.getenv("BOT_MIN_EDGE", "0.05"))
BOT_SCAN_INTERVAL_SEC = int(os.getenv("BOT_SCAN_INTERVAL_SEC", "300"))

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

BINANCE_BASE = "https://api.binance.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
NEWSAPI_BASE = "https://newsapi.org/v2"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

DATABASE_URL = "sqlite:///./polygeni.db"
