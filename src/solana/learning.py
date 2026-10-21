"""
Self-learning from a bot's own trades. Every entry is saved with the situation it was taken in (trend, RSI band,
dip/breakout setups, market breadth when known, the coin). Once trades close, each situation gets the bot's real
record over the last LEARN_DAYS. A situation the bot keeps losing in (≥ LEARN_MIN_TRADES trades, win rate below
LEARN_MAX_WIN_RATE and a net loss) is avoided from then on; when newer trades bring the record back up, it is allowed
again. Learning can only skip a trade — never add size, loosen a stop or create a trade — so it cannot add risk.
The real account learns from the same bot's lab record (more trades, same rules).
"""
import time
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg

CACHE_S = 120
_cache: dict = {}


def situation(candles: list[dict], symbol: str) -> list[str]:
    """Plain-language labels of the moment (the same ones the training center uses) plus the coin."""
    from src.solana.strategies import features
    from src.solana.trainer import tags_for
    try:
        tags = tags_for(features(candles), candles, {})
    except Exception:
        tags = []
    return tags + [f"moeda {symbol}"]


def record(ledger, strategy: str | None = None) -> dict[str, dict]:
    """Per situation: {'n', 'wins', 'pnl'} of this bot's closed trades in the last LEARN_DAYS (cached briefly)."""
    key = (ledger.path, strategy)
    hit = _cache.get(key)
    if hit and hit[0] > time.time() - CACHE_S:
        return hit[1]
    since = (datetime.now(timezone.utc) - timedelta(days=cfg.LEARN_DAYS)).isoformat()
    tags_of = {d["order_id"]: d["signals"].get("tags") for d in ledger.decisions(5000, kind="entry")
               if d.get("order_id") and d["signals"].get("tags")}
    out: dict[str, dict] = {}
    for t in ledger.closed_trades():
        if t["exit_ts"] < since or (strategy and t["strategy"] != strategy):
            continue
        for tag in tags_of.get((t.get("order_id") or "").removeprefix("exit-")) or []:
            r = out.setdefault(tag, {"n": 0, "wins": 0, "pnl": 0.0})
            r["n"] += 1
            r["wins"] += t["pnl_usd"] > 0
            r["pnl"] = round(r["pnl"] + t["pnl_usd"], 4)
    _cache[key] = (time.time(), out)
    return out


def _total(rec: dict[str, dict]) -> int:
    return sum(r["n"] for t, r in rec.items() if t.startswith("moeda ")) or 1


def bad(r: dict, total: int | None = None) -> bool:
    """A losing situation — and a specific one: a label on most of the bot's trades ('alta', its own setup) is the
    strategy itself, not a situation to avoid (avoiding it would mean never trading; evolution judges the strategy)."""
    return (r["n"] >= cfg.LEARN_MIN_TRADES and r["wins"] / r["n"] < cfg.LEARN_MAX_WIN_RATE and r["pnl"] < 0
            and (total is None or r["n"] <= total / 2))


def avoid(rec: dict[str, dict], tags: list[str], inherited=()) -> list[str]:
    """The situations in `tags` this bot has learned to stay out of — its own record first; lessons inherited from its
    predecessor count until it has its own LEARN_MIN_TRADES trades in that situation."""
    total = _total(rec)
    return [t for t in tags if (t in rec and bad(rec[t], total)) or (t in inherited and rec.get(t, {}).get("n", 0) < cfg.LEARN_MIN_TRADES)]


def summary(rec: dict[str, dict]) -> dict:
    """What a bot has learned: situations it avoids, where it does best, how many trade-situations it has seen."""
    good = [(t, r) for t, r in rec.items() if r["n"] >= cfg.LEARN_MIN_TRADES and r["wins"] / r["n"] >= 0.6 and r["pnl"] > 0]
    total = _total(rec)
    return {"avoid": [{"tag": t, **r} for t, r in sorted(rec.items(), key=lambda kv: kv[1]["pnl"]) if bad(r, total)][:5],
            "best": [{"tag": t, **r} for t, r in sorted(good, key=lambda kv: -kv[1]["pnl"])][:3],
            "seen": sum(r["n"] for t, r in rec.items() if t.startswith("moeda "))}
