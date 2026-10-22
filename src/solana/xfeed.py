"""
X.com posts about crypto and meme coins for the bots and the dashboard — read-only, with a HARD daily dollar cap.
The X API is pay-per-use: $0.005 per post returned (a User expansion would cost double, so none is asked for). Every
call therefore reserves its worst case against X_DAILY_BUDGET_USD before it is made, the real cost (posts returned) is
booked after, and the day rolls over at Lisbon midnight. Posts are tagged with the coins they mention (cashtags like
$ZEC and the same names the news feed knows) and handed to the AI bots through news.for_llm.
Nothing here posts, likes or follows: the bearer token only reads. Without X_BEARER_TOKEN the module stays silent.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from src.solana import config as cfg

logger = logging.getLogger("solana.xfeed")
URL = "https://api.x.com/2/tweets/search/recent"
COST_PER_POST = 0.005
QUERY = "($ZEC OR $PUMP OR $SOL OR $BTC OR $ETH OR $HYPE OR pump.fun OR memecoin) -is:retweet lang:en"
QUERY_PLAIN = "(zcash OR pump.fun OR solana OR bitcoin OR hyperliquid OR memecoin) -is:retweet lang:en"     # if cashtags are not allowed
_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
_lock = threading.Lock()


def _token() -> str:
    return os.getenv("X_BEARER_TOKEN", "")


def _load() -> dict:
    try:
        with open(cfg.X_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(st: dict):
    tmp = cfg.X_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, cfg.X_STATE_PATH)


def _today() -> str:
    return datetime.now(cfg.TZ).date().isoformat()


def tag(text: str) -> list[str]:
    from src.solana import news
    return sorted({*news.tag(text), *(c.upper() for c in _CASHTAG.findall(text))})


def poll(now: float | None = None) -> dict:
    """One budgeted read. Returns {'ok', 'skipped' (why) or 'posts', 'cost'}."""
    if not _token():
        return {"ok": False, "skipped": "sem X_BEARER_TOKEN"}
    now = now or time.time()
    with _lock:
        st = _load()
        if st.get("day") != _today():
            st.update(day=_today(), spent=0.0, calls=0)
        if now < st.get("blocked_until", 0):
            return {"ok": False, "skipped": "pausado depois de um erro de pagamento/limite"}
        worst = cfg.X_MAX_RESULTS * COST_PER_POST                       # reserve the worst case before calling
        if st.get("spent", 0.0) + worst > cfg.X_DAILY_BUDGET_USD + 1e-9:
            return {"ok": False, "skipped": f"teto do dia (${cfg.X_DAILY_BUDGET_USD:.2f}) atingido"}
        if now - st.get("last_call", 0) < cfg.X_POLL_MIN * 60:
            return {"ok": False, "skipped": "ainda não é hora da próxima leitura"}
        st["last_call"] = now                                           # even a failed call waits its turn: no hammering
        params = {"query": QUERY_PLAIN if st.get("plain") else QUERY, "max_results": max(10, cfg.X_MAX_RESULTS),
                  "tweet.fields": "created_at,public_metrics"}
        if st.get("since_id"):
            params["since_id"] = st["since_id"]                         # never pay twice for a post already read
        try:
            r = httpx.get(URL, params=params, headers={"Authorization": f"Bearer {_token()}"}, timeout=20)
        except Exception as e:
            st["error"] = f"rede: {str(e)[:120]}"
            _save(st)
            return {"ok": False, "error": st["error"]}
        if r.status_code == 400 and not st.get("plain"):               # cashtag operators not available on this plan: use words
            st.update(plain=True, error=None)
            _save(st)
            return {"ok": False, "skipped": "consulta trocada para palavras; tenta na próxima"}
        if r.status_code != 200:
            st["error"] = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code in (402, 403, 429):                        # no credits: look again soon (the owner may be topping up)
                st["blocked_until"] = now + (1800 if r.status_code == 402 else 6 * 3600)
            _save(st)
            return {"ok": False, "error": st["error"]}
        data = r.json().get("data") or []
        cost = len(data) * COST_PER_POST
        st.update(spent=round(st.get("spent", 0.0) + cost, 4), calls=st.get("calls", 0) + 1, error=None)
        posts = st.get("posts", [])
        for d in data:
            m = d.get("public_metrics") or {}
            posts.append({"id": d["id"], "text": d["text"], "ts": d.get("created_at"), "likes": m.get("like_count", 0),
                          "reposts": m.get("retweet_count", 0), "coins": tag(d["text"])})
        st["posts"] = sorted(posts, key=lambda p: int(p["id"]), reverse=True)[:150]
        if data:
            st["since_id"] = max(data, key=lambda d: int(d["id"]))["id"]
        _save(st)
        return {"ok": True, "posts": len(data), "cost": round(cost, 4)}


def recent(limit: int = 20, coin: str | None = None, hours: float = 24) -> list[dict]:
    cut = datetime.now(timezone.utc).timestamp() - hours * 3600
    out = []
    for p in _load().get("posts", []):
        try:
            if p.get("ts") and datetime.fromisoformat(p["ts"].replace("Z", "+00:00")).timestamp() < cut:
                continue
        except ValueError:
            pass
        if coin is None or coin.upper() in p.get("coins", []):
            out.append(p)
    return out[:limit]


def for_llm(coin: str, hours: float = 6, limit: int = 3) -> list[str]:
    """Posts of the last hours that mention the coin, with their engagement (what the AI bot reads)."""
    return [f"X: {p['text'][:140].replace(chr(10), ' ')} (♥{p['likes']} ↻{p['reposts']})" for p in recent(limit, coin, hours)]


def status() -> dict:
    st = _load()
    spent = st.get("spent", 0.0) if st.get("day") == _today() else 0.0
    return {"enabled": bool(_token()), "budget_usd": cfg.X_DAILY_BUDGET_USD, "spent_usd": round(spent, 4),
            "remaining_usd": round(max(cfg.X_DAILY_BUDGET_USD - spent, 0.0), 4), "calls_today": st.get("calls", 0) if st.get("day") == _today() else 0,
            "poll_min": cfg.X_POLL_MIN, "error": st.get("error"), "posts": len(st.get("posts", [])),
            "last_call": datetime.fromtimestamp(st["last_call"], timezone.utc).isoformat(timespec="seconds") if st.get("last_call") else None,
            "query": "palavras" if st.get("plain") else "cashtags"}


def start(every_s: int = 300):
    def loop():
        while True:
            try:
                poll()
            except Exception as e:
                logger.warning(f"x feed: {e}")
            time.sleep(every_s)
    if _token():
        threading.Thread(target=loop, daemon=True, name="xfeed").start()
