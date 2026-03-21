"""
Polymarket Gamma API — market discovery and metadata.
"""
import json
import httpx
from src.config import GAMMA_HOST

EVENTS_URL = f"{GAMMA_HOST}/events"

# Confirmed working tag slugs on the Gamma API
BITCOIN_SLUGS = ["bitcoin", "crypto"]
SPORTS_SLUGS  = ["sports"]
EVENTS_SLUGS  = ["politics", "pop-culture", "world"]


def _get_events(tag_slug: str, limit: int = 20) -> list[dict]:
    with httpx.Client(timeout=15) as client:
        resp = client.get(EVENTS_URL, params={
            "active": "true",
            "closed": "false",
            "tag_slug": tag_slug,
            "limit": limit,
        })
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


def _flatten(events: list[dict]) -> list[dict]:
    """Pull individual markets from event objects, keep only active ones."""
    markets = []
    seen = set()
    for event in events:
        for m in event.get("markets", []):
            cid = m.get("conditionId")
            if cid and cid not in seen and m.get("active") and not m.get("closed"):
                m["_event_title"] = event.get("title", "")
                seen.add(cid)
                markets.append(m)
    return markets


def _fetch(slugs: list[str], per_slug: int = 20) -> list[dict]:
    markets = []
    seen = set()
    for slug in slugs:
        for m in _flatten(_get_events(slug, limit=per_slug)):
            cid = m.get("conditionId")
            if cid not in seen:
                seen.add(cid)
                markets.append(m)
    return markets


def fetch_bitcoin_markets(limit: int = 40) -> list[dict]:
    return _fetch(BITCOIN_SLUGS, per_slug=limit // len(BITCOIN_SLUGS))


def fetch_sports_markets(limit: int = 40) -> list[dict]:
    return _fetch(SPORTS_SLUGS, per_slug=limit)


def fetch_events_markets(limit: int = 40) -> list[dict]:
    return _fetch(EVENTS_SLUGS, per_slug=limit // len(EVENTS_SLUGS))


def _parse_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []


def get_market_tokens(market: dict) -> tuple[str | None, str | None]:
    """Returns (yes_token_id, no_token_id)."""
    ids = _parse_list(market.get("clobTokenIds") or market.get("tokens") or [])
    yes = ids[0] if len(ids) > 0 else None
    no  = ids[1] if len(ids) > 1 else None
    return yes, no


def get_market_price(market: dict) -> tuple[float | None, float | None]:
    """Returns (yes_price, no_price) in [0,1] from the market object."""
    prices = _parse_list(market.get("outcomePrices") or [])
    yes = float(prices[0]) if len(prices) > 0 else None
    no  = float(prices[1]) if len(prices) > 1 else None
    return yes, no


def parse_end_date(market: dict) -> str | None:
    return market.get("endDateIso") or market.get("endDate")
