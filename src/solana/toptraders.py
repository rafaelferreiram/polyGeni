"""
Top traders of Hyperliquid: who they are (public leaderboard), what they hold and what they just opened (public
clearinghouse state of each wallet, polled every couple of minutes). Used three ways:
  1. dashboard panel (traders, positions, recent moves),
  2. context for the AI bot (what the best traders are doing on the coin it is deciding),
  3. the copy bot "Veiga": buys a coin when ≥ 2 of the top 10 are long and one just entered — through the same
     committee (Coordinator, Debate, Risk) and lab evidence gate as every other bot.
Long side only (the bots never short). Read-only; nothing here places orders.
"""
import logging
import threading
import time
from collections import deque

import httpx

from src.solana import config as cfg

logger = logging.getLogger("solana.toptraders")
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

_traders: list[dict] = []                      # the chosen top 10 with their stats
_positions: dict[str, dict[str, dict]] = {}    # address -> coin -> position
_events: deque = deque(maxlen=500)             # (ts, address, coin, what, usd)
_lock = threading.Lock()
_status = {"leaderboard_at": None, "polled_at": None, "error": None}
_started = False


def _win(row: dict, key: str) -> dict:
    return {n: {k: float(v) for k, v in w.items()} for n, w in row["windowPerformances"]}[key]


def pick(rows: list[dict], n: int = 10) -> list[dict]:
    """Consistent, real traders: profitable this month, this week and all-time; ROI 5–300% (no deposit artefacts);
    account ≥ $100k; actually trading (monthly volume 1–300x the account, so not idle and not a market maker).
    Ranked by dollars made this month."""
    out = []
    for r in rows:
        av = float(r["accountValue"])
        if av < 100_000:
            continue
        m, wk, a = _win(r, "month"), _win(r, "week"), _win(r, "allTime")
        turnover = m["vlm"] / av
        if 0.05 <= m["roi"] <= 3 and wk["pnl"] > 0 and a["pnl"] > 0 and 1 <= turnover <= 300:
            out.append({"address": r["ethAddress"], "name": r.get("displayName"), "account_usd": round(av),
                        "pnl_30d": round(m["pnl"]), "roi_30d": round(m["roi"], 4), "roi_7d": round(wk["roi"], 4),
                        "pnl_all": round(a["pnl"])})
    return sorted(out, key=lambda t: -t["pnl_30d"])[:n]


def diff(addr: str, before: dict, after: dict, now: float) -> list[tuple]:
    """Position changes of one trader between two polls -> events (opened/increased/reduced/closed long or short)."""
    ev = []
    for coin in set(before) | set(after):
        b, a = before.get(coin, {}).get("szi", 0.0), after.get(coin, {}).get("szi", 0.0)
        usd = abs(after.get(coin, before.get(coin, {})).get("value", 0.0))
        if b <= 0 < a:
            ev.append((now, addr, coin, "abriu compra", usd))
        elif b >= 0 > a:
            ev.append((now, addr, coin, "abriu venda (short)", usd))
        elif b > 0 and a > b * 1.1:
            ev.append((now, addr, coin, "aumentou compra", usd))
        elif b > 0 and a == 0:
            ev.append((now, addr, coin, "fechou compra", usd))
        elif b < 0 and a == 0:
            ev.append((now, addr, coin, "fechou short", usd))
    return ev


def _positions_of(addr: str) -> dict[str, dict]:
    from src.solana import hyperliquid as hl
    st = hl.info({"type": "clearinghouseState", "user": addr})
    out = {}
    for p in st.get("assetPositions", []):
        q = p["position"]
        out[q["coin"]] = {"szi": float(q["szi"]), "entry": float(q.get("entryPx") or 0), "value": float(q.get("positionValue") or 0),
                          "lev": (q.get("leverage") or {}).get("value"), "roe": float(q.get("returnOnEquity") or 0),
                          "upnl": float(q.get("unrealizedPnl") or 0)}
    return out


def refresh_leaderboard():
    r = httpx.get(LEADERBOARD_URL, timeout=120)
    r.raise_for_status()
    top = pick(r.json()["leaderboardRows"])
    with _lock:
        _traders[:] = top
    _status["leaderboard_at"] = time.time()


def poll():
    now = time.time()
    _status["error"] = None
    for t in list(_traders):
        try:
            after = _positions_of(t["address"])
        except Exception as e:
            _status["error"] = f"{t['address'][:8]}: {str(e)[:80]}"
            continue
        with _lock:
            before = _positions.get(t["address"])
            if before is not None:                 # first poll is the baseline, not "new" trades
                _events.extend(diff(t["address"], before, after, now))
            _positions[t["address"]] = after
    _status["polled_at"] = now


def consensus(coin: str, now: float | None = None) -> dict:
    """What the top 10 do on one coin: how many are long/short, how many just went long, total long size."""
    now = now or time.time()
    with _lock:
        pos = [(a, p[coin]) for a, p in _positions.items() if coin in p]
        recent = [e for e in _events if e[2] == coin and e[0] >= now - 3600]
    longs = [p for _, p in pos if p["szi"] > 0]
    return {"long": len(longs), "short": sum(p["szi"] < 0 for _, p in pos),
            "new_longs_1h": sum(e[3] in ("abriu compra", "aumentou compra") for e in recent),
            "closed_longs_1h": sum(e[3] == "fechou compra" for e in recent),
            "long_usd": round(sum(p["value"] for p in longs)), "of": len(_traders)}


def board(coins: list[str]) -> dict:
    with _lock:
        traders = [{**t, "positions": sorted(({"coin": c, **p} for c, p in _positions.get(t["address"], {}).items()),
                                             key=lambda p: -p["value"])[:6]} for t in _traders]
        events = [{"ts": e[0], "address": e[1], "coin": e[2], "what": e[3], "usd": round(e[4])} for e in reversed(_events)][:30]
    return {"status": dict(_status), "traders": traders, "events": events,
            "consensus": {c: consensus(c) for c in coins}}


def start(poll_s: int = 120, leaderboard_s: int = 6 * 3600):
    global _started
    if _started or not cfg.TOP_TRADERS_ENABLED:
        return
    _started = True

    def loop():
        last_lb = 0.0
        while True:
            try:
                if time.time() - last_lb > leaderboard_s or not _traders:
                    refresh_leaderboard()
                    last_lb = time.time()
                poll()
            except Exception as e:
                _status["error"] = str(e)[:160]
                logger.warning(f"top traders: {e}")
            time.sleep(poll_s)
    threading.Thread(target=loop, daemon=True, name="toptraders").start()
