"""
Training center: a large, honest dataset for the bots to learn from, separate from the lab that proves them.

Every closed 1h candle of every coin watched becomes one sample: what the bots could see (indicators, trend, the
rules' setups, whale flow, top traders, news, market breadth) and, once the next 24h are known, what would have
happened to a long and to a short with the bots' own stop (2 ATR) and target (3 ATR), plus the 4h and 24h return.
No money, no position limits, no AI cost. From the resolved samples come "lessons" — the real hit rate of setups like
the current one — which the AI bot reads before deciding. (Claude cannot be retrained; this is its experience.)
"""
import json
import logging
import sqlite3
import threading
from statistics import mean

from src.solana import config as cfg

logger = logging.getLogger("solana.trainer")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
HORIZON = 24                       # hours a virtual trade may stay open


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(cfg.TRAINING_DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.execute("""CREATE TABLE IF NOT EXISTS samples (coin TEXT, t INTEGER, price REAL, atr_pct REAL, tags TEXT,
            features TEXT, long_result TEXT, short_result TEXT, ret_4h REAL, ret_24h REAL, resolved INTEGER DEFAULT 0,
            PRIMARY KEY (coin, t))""")
    return _conn


def tags_for(f: dict, candles: list[dict], ctx: dict) -> list[str]:
    """Plain-language labels of the situation (what lessons are grouped by)."""
    from src.solana.strategies import MeanReversionATR, Trend4h, TrendStrong
    t = ["alta" if f.get("uptrend") else "baixa"]
    if f.get("uptrend") and (f.get("below_lower_band") or (f.get("rsi14") or 100) < 40):
        t.append("queda numa alta")
    if MeanReversionATR().signal(candles):
        t.append("sinal São Marcos")
    if TrendStrong().signal(candles):
        t.append("rompimento 1h")
    if Trend4h().signal(candles):
        t.append("rompimento 4h")
    rsi = f.get("rsi14") or 50
    t.append("RSI alto" if rsi >= 70 else "RSI baixo" if rsi <= 30 else "RSI médio")
    wp = (ctx.get("whale_flow") or {}).get("pressure")
    if wp in ("comprando", "vendendo"):
        t.append(f"baleias {wp}")
    tt = ctx.get("top_traders") or {}
    if tt.get("long", 0) > tt.get("short", 0):
        t.append("top traders comprados")
    elif tt.get("short", 0) > tt.get("long", 0):
        t.append("top traders vendidos")
    if ctx.get("news"):
        t.append("com notícias")
    b = ctx.get("breadth") or {}
    if b.get("n"):
        t.append("mercado forte" if b["uptrend"] / b["n"] >= 0.6 else "mercado fraco" if b["uptrend"] / b["n"] <= 0.4 else "mercado misto")
    return t


def record(coin: str, candles: list[dict], ctx: dict) -> bool:
    """One sample per coin per closed candle (idempotent)."""
    from src.solana.strategies import atr, features
    if len(candles) < 210:
        return False
    f = features(candles)
    a = atr(candles) / candles[-1]["close"]
    row = (coin, candles[-1]["t"], candles[-1]["close"], a, json.dumps(tags_for(f, candles, ctx)),
           json.dumps({**{k: f.get(k) for k in ("rsi14", "atr_pct", "uptrend", "below_lower_band", "change_24h_pct")},
                       "whales": (ctx.get("whale_flow") or {}).get("1h"), "top": ctx.get("top_traders"),
                       "news": len(ctx.get("news") or [])}, default=str))
    with _lock:
        cur = db().execute("INSERT OR IGNORE INTO samples (coin,t,price,atr_pct,tags,features) VALUES (?,?,?,?,?,?)", row)
    return cur.rowcount == 1


def outcome(entry: float, a: float, future: list[dict], side: str) -> str:
    """'win' if the target (3 ATR) is touched before the stop (2 ATR) within HORIZON candles, 'loss' if the stop
    first (same candle touching both counts as a loss), else 'time'."""
    stop, tp = (entry * (1 - 2 * a), entry * (1 + 3 * a)) if side == "long" else (entry * (1 + 2 * a), entry * (1 - 3 * a))
    for c in future[:HORIZON]:
        hit_stop = c["low"] <= stop if side == "long" else c["high"] >= stop
        hit_tp = c["high"] >= tp if side == "long" else c["low"] <= tp
        if hit_stop:
            return "loss"
        if hit_tp:
            return "win"
    return "time"


def resolve(coin: str, candles: list[dict]) -> int:
    """Fill in outcomes for samples whose next 24h are now known."""
    with _lock:
        pending = db().execute("SELECT t, price, atr_pct FROM samples WHERE coin=? AND resolved=0", (coin,)).fetchall()
    by_t = {c["t"]: i for i, c in enumerate(candles)}
    done = 0
    for t, price, a in pending:
        i = by_t.get(t)
        if i is None or len(candles) - 1 - i < HORIZON:
            continue
        fut = candles[i + 1:i + 1 + HORIZON]
        with _lock:
            db().execute("UPDATE samples SET long_result=?, short_result=?, ret_4h=?, ret_24h=?, resolved=1 WHERE coin=? AND t=?",
                         (outcome(price, a, fut, "long"), outcome(price, a, fut, "short"),
                          fut[3]["close"] / price - 1, fut[-1]["close"] / price - 1, coin, t))
        done += 1
    return done


def run_once() -> dict:
    """Scheduler job: record the latest closed candle of every coin and resolve old samples (shared candle cache)."""
    from src.solana import hyperliquid as hl, news, toptraders, whales
    from src.solana.engine import _market_temp
    recorded = resolved = 0
    for coin in cfg.HL_LIVE_MARKETS:
        try:
            candles = hl.candles(coin)[:-1]
        except Exception as e:
            logger.warning(f"training {coin}: {e}")
            continue
        recorded += record(coin, candles, {"whale_flow": whales.summary(coin), "top_traders": toptraders.consensus(coin),
                                           "news": news.for_llm(coin), "breadth": _breadth_cache.get("b")})
        resolved += resolve(coin, candles)
    return {"recorded": recorded, "resolved": resolved}


_breadth_cache: dict = {}


def set_breadth(b: dict):
    _breadth_cache["b"] = b


def lessons(min_n: int = 20, limit: int = 40) -> list[dict]:
    """Hit rate of a long (and of a short) per situation label, over resolved samples."""
    with _lock:
        rows = db().execute("SELECT tags, long_result, short_result, ret_24h FROM samples WHERE resolved=1").fetchall()
    agg: dict[str, list] = {}
    for tags, lr, sr, r24 in rows:
        for tag in json.loads(tags):
            agg.setdefault(tag, []).append((lr, sr, r24))
    out = []
    for tag, v in agg.items():
        if len(v) < min_n:
            continue
        out.append({"tag": tag, "n": len(v), "long_win": round(sum(x[0] == "win" for x in v) / len(v), 3),
                    "long_loss": round(sum(x[0] == "loss" for x in v) / len(v), 3),
                    "short_win": round(sum(x[1] == "win" for x in v) / len(v), 3),
                    "avg_ret_24h": round(mean(x[2] for x in v) * 100, 2)})
    return sorted(out, key=lambda x: -x["n"])[:limit]


def lessons_for(tags: list[str], min_n: int = 20) -> list[str]:
    """What the AI reads: the real record of the situations it is in right now."""
    by = {x["tag"]: x for x in lessons(min_n, 200)}
    out = []
    for t in tags:
        x = by.get(t)
        if x:
            out.append(f"'{t}': compra bateu o alvo em {x['long_win']:.0%} e o stop em {x['long_loss']:.0%} de {x['n']} casos; "
                       f"retorno médio 24h {x['avg_ret_24h']:+.2f}%")
    return out


def backfill() -> int:
    """Seed the dataset from the candle history already in memory (no whale/top-trader/news context for the past)."""
    from src.solana import hyperliquid as hl
    n = 0
    for coin in cfg.HL_LIVE_MARKETS:
        try:
            candles = hl.candles(coin)[:-1]
        except Exception:
            continue
        for i in range(210, len(candles) - HORIZON):
            n += record(coin, candles[:i + 1], {})
        resolve(coin, candles)
    return n


def stats() -> dict:
    with _lock:
        n, res = db().execute("SELECT COUNT(*), SUM(resolved) FROM samples").fetchone()
        last = db().execute("SELECT MAX(t) FROM samples").fetchone()[0]
    return {"samples": n or 0, "resolved": res or 0, "last_t": last}
