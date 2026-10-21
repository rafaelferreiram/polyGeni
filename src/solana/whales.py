"""
Whale tracking on Hyperliquid: one WebSocket subscription to the public `trades` feed of every coin the bots watch.
An aggressive order big enough to move that market (≥ WHALE_MIN_USD and ≥ 0.02% of the coin's daily volume) is a
"whale" trade. We keep 4 hours of them per coin: who is buying vs selling, how much, and which wallets repeat.

It costs nothing against the REST rate limit (WebSocket) and never trades. The flow is shown on the dashboard and
handed to the AI bot as context; it is not a buy/sell rule until the lab shows it helps.
"""
import json
import logging
import threading
import time
from collections import defaultdict, deque

from src.solana import config as cfg

logger = logging.getLogger("solana.whales")
WS_URL = "wss://api.hyperliquid.xyz/ws"
WINDOW_S = 4 * 3600

_trades: dict[str, deque] = defaultdict(deque)       # coin -> (ts, side 'B'/'A', usd, taker wallet)
_lock = threading.Lock()
_status = {"connected": False, "since": None, "error": None, "messages": 0}
_thresholds: dict[str, float] = {}
_watching: list[str] = []
_started = False


def _universe(ours) -> list[str]:
    """Our coins first, then every other liquid Hyperliquid perp (≥ $1M daily volume): whales anywhere in crypto."""
    from src.solana import hyperliquid as hl
    try:
        others = [c for c, a in hl.asset_ctxs().items()
                  if c not in ours and not a.get("isDelisted") and float(a.get("dayNtlVlm") or 0) >= 1e6]
    except Exception as e:
        logger.warning(f"whale universe: {e}")
        others = []
    return list(ours) + sorted(others)


def watching() -> list[str]:
    return list(_watching)


def threshold(coin: str) -> float:
    return _thresholds.get(coin, cfg.WHALE_MIN_USD)


def _refresh_thresholds(coins):
    from src.solana import hyperliquid as hl
    try:
        ctx = hl.asset_ctxs()
        for c in coins:
            vol = float((ctx.get(c) or {}).get("dayNtlVlm") or 0)
            _thresholds[c] = max(cfg.WHALE_MIN_USD, vol * 0.0002)
    except Exception as e:
        logger.warning(f"whale thresholds: {e}")


def ingest(fills: list[dict], now: float | None = None):
    """Group fills of one aggressive order (same tx hash) and keep the ones above the coin's whale threshold."""
    orders: dict[tuple, list] = {}
    for f in fills:
        key = (f["coin"], f.get("hash") or f.get("tid"), f["side"])
        o = orders.setdefault(key, [0.0, (f.get("users") or [None, None])[0 if f["side"] == "B" else 1], f["time"]])
        o[0] += float(f["px"]) * float(f["sz"])
    now = now or time.time()
    with _lock:
        for (coin, _, side), (usd, wallet, ts) in orders.items():
            if usd >= threshold(coin):
                _trades[coin].append((ts / 1000, side, usd, wallet))
        for q in _trades.values():
            while q and q[0][0] < now - WINDOW_S:
                q.popleft()


def summary(coin: str, now: float | None = None) -> dict:
    now = now or time.time()
    with _lock:
        rows = list(_trades.get(coin, ()))
    out = {"threshold_usd": round(threshold(coin))}
    for label, secs in (("1h", 3600), ("4h", WINDOW_S)):
        w = [r for r in rows if r[0] >= now - secs]
        buy, sell = sum(r[2] for r in w if r[1] == "B"), sum(r[2] for r in w if r[1] == "A")
        out[label] = {"buy_usd": round(buy), "sell_usd": round(sell), "net_usd": round(buy - sell), "n": len(w)}
    h = out["1h"]
    total = h["buy_usd"] + h["sell_usd"]
    ratio = (h["buy_usd"] - h["sell_usd"]) / total if total else 0
    out["pressure"] = ("sem baleias" if h["n"] < 2 else "comprando" if ratio >= 0.3 else "vendendo" if ratio <= -0.3 else "neutro")
    wallets = defaultdict(float)
    for r in rows:
        if r[3]:
            wallets[r[3]] += r[2] if r[1] == "B" else -r[2]
    out["top_wallets"] = [{"wallet": w, "net_usd": round(v)} for w, v in sorted(wallets.items(), key=lambda kv: -abs(kv[1]))[:3]]
    return out


def recent(limit: int = 15) -> list[dict]:
    with _lock:
        rows = [(coin, *r) for coin, q in _trades.items() for r in q]
    rows.sort(key=lambda r: -r[1])
    return [{"coin": c, "ts": ts, "side": "compra" if s == "B" else "venda", "usd": round(u), "wallet": w}
            for c, ts, s, u, w in rows[:limit]]


def status() -> dict:
    return dict(_status)


def _run(ours):
    import websocket
    backoff = 2
    while True:
        try:
            coins = _universe(ours)
            _watching[:] = coins
            _refresh_thresholds(coins)
            ws = websocket.create_connection(WS_URL, timeout=30)
            for c in coins:
                ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
            _status.update(connected=True, since=time.time(), error=None)
            backoff, last_refresh = 2, time.time()
            while True:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    ws.send(json.dumps({"method": "ping"}))        # Hyperliquid drops idle sockets after 60s
                    continue
                if msg.get("channel") == "trades":
                    _status["messages"] += 1
                    ingest(msg["data"])
                if time.time() - last_refresh > 600:
                    _refresh_thresholds(coins)
                    last_refresh = time.time()
        except Exception as e:
            _status.update(connected=False, error=str(e)[:160])
            logger.warning(f"whale feed disconnected: {e}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def start(coins=None):
    global _started
    if _started or not cfg.WHALES_ENABLED:
        return
    _started = True
    threading.Thread(target=_run, args=(list(coins or cfg.HL_LIVE_MARKETS),), daemon=True, name="whales").start()
