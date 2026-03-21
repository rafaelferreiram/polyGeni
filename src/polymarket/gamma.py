"""
Polymarket Gamma API — market discovery and metadata.
"""
import httpx
from src.config import GAMMA_HOST

# Tags used by Polymarket to classify markets
BITCOIN_TAGS = ["bitcoin", "crypto", "btc"]
SPORTS_TAGS = ["sports", "nfl", "nba", "soccer", "mls", "ufc", "tennis", "mlb", "nhl"]
EVENTS_TAGS = ["politics", "world", "economy", "finance", "elections", "geopolitics"]


def _get(path: str, params: dict | None = None) -> list | dict:
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{GAMMA_HOST}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def fetch_active_markets(tag: str | None = None, limit: int = 100) -> list[dict]:
    """Fetch active markets, optionally filtered by tag."""
    params: dict = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }
    if tag:
        params["tag"] = tag
    data = _get("/markets", params=params)
    if isinstance(data, dict):
        return data.get("data", [])
    return data or []


def fetch_bitcoin_markets(limit: int = 50) -> list[dict]:
    markets = []
    for tag in BITCOIN_TAGS:
        markets.extend(fetch_active_markets(tag=tag, limit=limit))
    seen = set()
    unique = []
    for m in markets:
        mid = m.get("conditionId") or m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)
    return unique


def fetch_sports_markets(limit: int = 50) -> list[dict]:
    markets = []
    for tag in SPORTS_TAGS:
        markets.extend(fetch_active_markets(tag=tag, limit=limit))
    seen = set()
    unique = []
    for m in markets:
        mid = m.get("conditionId") or m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)
    return unique


def fetch_events_markets(limit: int = 50) -> list[dict]:
    markets = []
    for tag in EVENTS_TAGS:
        markets.extend(fetch_active_markets(tag=tag, limit=limit))
    seen = set()
    unique = []
    for m in markets:
        mid = m.get("conditionId") or m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)
    return unique


def get_market_tokens(market: dict) -> tuple[str | None, str | None]:
    """Returns (yes_token_id, no_token_id) for a market."""
    tokens = market.get("tokens") or market.get("clobTokenIds") or []
    if len(tokens) >= 2:
        return tokens[0], tokens[1]
    elif len(tokens) == 1:
        return tokens[0], None
    return None, None


def parse_end_date(market: dict) -> str | None:
    return market.get("endDate") or market.get("end_date_iso")
