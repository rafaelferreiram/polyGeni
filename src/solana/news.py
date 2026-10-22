"""
Crypto news for the bots: free RSS feeds of the main crypto outlets, fetched every few minutes and tagged with the
coins they mention. Shown on the dashboard and handed to the AI bot (headlines of the last hours for the coin it is
deciding on). X.com posts come from xfeed.py (pay-per-use, hard daily cap) and join these headlines when configured.
"""
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger("solana.news")
FEEDS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/", "Cointelegraph": "https://cointelegraph.com/rss",
         "Decrypt": "https://decrypt.co/feed", "The Block": "https://www.theblock.co/rss.xml"}
NAMES = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana", "sol"], "XRP": ["xrp", "ripple"],
         "HYPE": ["hyperliquid", "hype"], "BNB": ["bnb", "binance coin"], "DOGE": ["dogecoin", "doge"], "ADA": ["cardano", "ada"],
         "TRX": ["tron", "trx"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink", "link token"], "LTC": ["litecoin", "ltc"],
         "BCH": ["bitcoin cash", "bch"], "XLM": ["stellar", "xlm"], "HBAR": ["hedera", "hbar"], "DOT": ["polkadot", "dot token"],
         "SUI": ["sui"], "NEAR": ["near protocol"], "AAVE": ["aave"], "UNI": ["uniswap", "uni token"],
         "ARB": ["arbitrum", "arb"], "TAO": ["bittensor", "tao"], "ZEC": ["zcash", "zec"], "XMR": ["monero", "xmr"],
         "WLD": ["worldcoin", "world network", "wld"], "ENA": ["ethena", "ena"], "ZRO": ["layerzero", "zro"],
         "ONDO": ["ondo"], "kPEPE": ["pepe"], "PUMP": ["pump.fun", "pumpfun", "pump token", "PUMP token"]}
_PATTERNS = {c: re.compile(r"\b(" + "|".join(re.escape(n) for n in ns) + r")\b", re.I) for c, ns in NAMES.items()}

_items: list[dict] = []
_lock = threading.Lock()
_status = {"ok": None, "updated": None, "error": None, "sources": {}}


def tag(text: str) -> list[str]:
    return [c for c, p in _PATTERNS.items() if p.search(text or "")]


def _parse(source: str, xml: str) -> list[dict]:
    out = []
    for it in ET.fromstring(xml).iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")[:300].strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = time.time()
        out.append({"source": source, "title": title, "summary": desc, "url": (it.findtext("link") or "").strip(),
                    "ts": ts, "coins": tag(f"{title} {desc}")})
    return out


def refresh():
    items, sources = [], {}
    for name, url in FEEDS.items():
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Kairos"})
            r.raise_for_status()
            got = _parse(name, r.text)
            items += got
            sources[name] = len(got)
        except Exception as e:
            sources[name] = f"erro: {str(e)[:60]}"
    seen, uniq = set(), []
    for i in sorted(items, key=lambda x: -x["ts"]):
        if i["title"] not in seen:
            seen.add(i["title"])
            uniq.append(i)
    with _lock:
        _items[:] = uniq[:300]
    _status.update(ok=any(isinstance(v, int) for v in sources.values()), updated=time.time(), sources=sources)


def latest(coin: str | None = None, hours: float = 24, limit: int = 30) -> list[dict]:
    since = time.time() - hours * 3600
    with _lock:
        rows = [i for i in _items if i["ts"] >= since and (coin is None or coin in i["coins"])]
    return rows[:limit]


def trending(hours: float = 12, top: int = 5) -> dict:
    """What the crypto press is talking about: coins ranked by recency-weighted mentions, and the top headlines
    (newest first among those that mention the hottest coins)."""
    now, since = time.time(), time.time() - hours * 3600
    with _lock:
        rows = [i for i in _items if i["ts"] >= since]
    score, count = {}, {}
    for i in rows:
        w = 1.0 + max(0.0, 1 - (now - i["ts"]) / (hours * 3600))          # newer stories weigh up to 2x
        for c in i["coins"]:
            score[c] = score.get(c, 0) + w
            count[c] = count.get(c, 0) + 1
    hot = sorted(score, key=lambda c: -score[c])[:top]
    picked, seen = [], set()
    for c in hot:                                                       # best headline per hot coin
        h = next((i for i in rows if c in i["coins"] and i["title"] not in seen), None)
        if h:
            seen.add(h["title"])
            picked.append(h)
    return {"coins": [{"coin": c, "mentions": count[c]} for c in hot], "headlines": picked[:top],
            "mentions": count, "total": len(rows)}


def for_llm(coin: str) -> list[str]:
    """Headlines of the last 12h that mention the coin (what the AI bot reads before deciding)."""
    out = [f"{i['source']}: {i['title']}" for i in latest(coin, hours=12, limit=5)]
    try:                                                   # X posts, when the owner configured a token (budgeted, read-only)
        from src.solana import xfeed
        out += xfeed.for_llm(coin)
    except Exception:
        pass
    return out


def status() -> dict:
    return dict(_status)


def start(every_s: int = 300):
    def loop():
        while True:
            try:
                refresh()
            except Exception as e:
                _status.update(ok=False, error=str(e)[:160])
                logger.warning(f"news refresh: {e}")
            time.sleep(every_s)
    threading.Thread(target=loop, daemon=True, name="news").start()
