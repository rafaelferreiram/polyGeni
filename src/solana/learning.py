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


def window_tag(at=None) -> str:
    """The 4-hour UTC window of an entry ('janela 16-20h UTC'): the data showed whole windows losing together (35 entries
    at 18h UTC, none won), so the bots learn which windows they lose in — like any other situation."""
    d = at if isinstance(at, datetime) else datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    h = d.astimezone(timezone.utc).hour // 4 * 4
    return f"janela {h:02d}-{h + 4:02d}h UTC"


def situation(candles: list[dict], symbol: str, at=None) -> list[str]:
    """Plain-language labels of the moment (the same ones the training center uses), the coin and the time window."""
    from src.solana.strategies import features
    from src.solana.trainer import tags_for
    try:
        tags = tags_for(features(candles), candles, {})
    except Exception:
        tags = []
    return tags + [f"moeda {symbol}", window_tag(at)]


def record(ledger, strategy: str | None = None) -> dict[str, dict]:
    """Per situation: {'n', 'wins', 'pnl'} of this bot's closed trades in the last LEARN_DAYS (cached briefly)."""
    key = (ledger.path, strategy)
    hit = _cache.get(key)
    if hit and hit[0] > time.time() - CACHE_S:
        return hit[1]
    since = (datetime.now(timezone.utc) - timedelta(days=cfg.LEARN_DAYS)).isoformat()
    tags_of = {**(ledger.get_state("tags_backfill") or {}),
               **{d["order_id"]: d["signals"].get("tags") for d in ledger.decisions(5000, kind="entry")
                  if d.get("order_id") and d["signals"].get("tags")}}
    out: dict[str, dict] = {}
    days: dict[str, set] = {}
    for t in ledger.closed_trades():
        if t["exit_ts"] < since or (strategy and t["strategy"] != strategy):
            continue
        saved = tags_of.get((t.get("order_id") or "").removeprefix("exit-")) or []
        for tag in dict.fromkeys(saved + [window_tag(t["entry_ts"])]):          # older trades get their window from entry_ts
            r = out.setdefault(tag, {"n": 0, "wins": 0, "pnl": 0.0})
            r["n"] += 1
            r["wins"] += t["pnl_usd"] > 0
            r["pnl"] = round(r["pnl"] + t["pnl_usd"], 4)
            if tag.startswith("janela"):
                days.setdefault(tag, set()).add(t["entry_ts"][:10])
    for tag, ds in days.items():                                               # a time window is a weak lesson: count its days
        out[tag]["window"], out[tag]["days"] = True, len(ds)
    _cache[key] = (time.time(), out)
    return out


def _total(rec: dict[str, dict]) -> int:
    return sum(r["n"] for t, r in rec.items() if t.startswith("moeda ")) or 1


def bad(r: dict, total: int | None = None) -> bool:
    """A losing situation — and a specific one: a label on most of the bot's trades ('alta', its own setup) is the
    strategy itself, not a situation to avoid (avoiding it would mean never trading; evolution judges the strategy)."""
    if r.get("window") and r.get("days", 0) < 2:        # one bad day is not a pattern (35 entries of one afternoon blocked the whole team)
        return False
    return (r["n"] >= cfg.LEARN_MIN_TRADES and r["wins"] / r["n"] < cfg.LEARN_MAX_WIN_RATE and r["pnl"] < 0
            and (total is None or r["n"] <= total / 2))


def avoid(rec: dict[str, dict], tags: list[str], inherited=()) -> list[str]:
    """The situations in `tags` this bot has learned to stay out of — its own record first; lessons inherited from its
    predecessor count until it has its own LEARN_MIN_TRADES trades in that situation."""
    total = _total(rec)
    return [t for t in tags if (t in rec and bad(rec[t], total))
            or (t in inherited and not t.startswith("janela") and rec.get(t, {}).get("n", 0) < cfg.LEARN_MIN_TRADES)]


def backfill(ledger, interval: str = "1h") -> int:
    """Trades made before situations were recorded: rebuild each one's situation from the candles that had closed when
    it was bought, so the bot (and the team network) learn from its whole history at once."""
    from datetime import datetime
    from src.solana import hyperliquid as hl
    known = {**(ledger.get_state("tags_backfill") or {}),
             **{d["order_id"]: 1 for d in ledger.decisions(5000, kind="entry") if d.get("order_id") and d["signals"].get("tags")}}
    step = hl.INTERVAL_S.get(interval, 3600)
    out, cache = dict(ledger.get_state("tags_backfill") or {}), {}
    for t in ledger.closed_trades():
        oid = (t.get("order_id") or "").removeprefix("exit-")
        if not oid or oid in known or t["symbol"].endswith("~short"):
            continue
        try:
            candles = cache.get(t["symbol"]) or cache.setdefault(t["symbol"], hl.candles(t["symbol"], interval))
        except Exception:
            continue
        at = datetime.fromisoformat(t["entry_ts"]).timestamp()
        past = [c for c in candles if c["t"] + step <= at]
        if len(past) >= 210:
            out[oid] = situation(past, t["symbol"], datetime.fromisoformat(t["entry_ts"]))
    if len(out) != len(ledger.get_state("tags_backfill") or {}):
        ledger.set_state("tags_backfill", out)
        _cache.clear()
    return len(out)


# ─── the team's shared network: every bot's closed trades feed one common record ───
def team_record() -> dict[str, dict]:
    """Per situation, the whole team's record (all lab bots' closed trades, cached briefly)."""
    from src.solana import arena
    hit = _cache.get("team")
    if hit and hit[0] > time.time() - CACHE_S:
        return hit[1]
    out: dict[str, dict] = {}
    for eng in list((arena._bots or {}).values()):
        for tag, r in record(eng.ledger).items():
            t = out.setdefault(tag, {"n": 0, "wins": 0, "pnl": 0.0})
            t["n"] += r["n"]
            t["wins"] += r["wins"]
            t["pnl"] = round(t["pnl"] + r["pnl"], 4)
    _cache["team"] = (time.time(), out)
    from src.solana import teamchat
    total = _total(out)
    teamchat.team_lessons([{"tag": t, **r} for t, r in out.items() if r["n"] >= 2 * cfg.LEARN_MIN_TRADES and bad(r, total)])
    return out


def team_avoid(own: dict[str, dict], team: dict[str, dict], tags: list[str]) -> list[str]:
    """Situations the whole team keeps losing in (twice the evidence a single bot needs), unless this bot itself does
    well there: a lesson learned by one teammate protects the others."""
    total, own_total = _total(team), _total(own)
    out = []
    for t in tags:
        r, mine = team.get(t), own.get(t)
        if t.startswith("janela") or not r or r["n"] < 2 * cfg.LEARN_MIN_TRADES or not bad(r, total):     # time windows: own record only
            continue
        if mine and mine["n"] >= cfg.LEARN_MIN_TRADES and mine["wins"] / mine["n"] >= 0.5:
            continue
        if mine and mine["n"] > own_total / 2:    # it is this bot's own core move: the evolution cut judges that, not the team
            continue
        out.append(t)
    return out


def better_teammate(model: str, tags: list[str], margin: float = 0.02) -> tuple[str, str] | None:
    """The teammate whose own record in this situation is clearly better than `model`'s (by average result per trade
    over the tags where it has evidence). Returns (teammate, why) or None."""
    from src.solana import arena
    def score(m: str) -> tuple[float, list[str]]:
        eng = (arena._bots or {}).get(m)
        rec = record(eng.ledger) if eng else {}
        total = _total(rec)
        hits = [(t, rec[t]) for t in tags if t in rec and rec[t]["n"] >= cfg.LEARN_MIN_TRADES and rec[t]["n"] <= total * 0.8]
        if not hits:
            return 0.0, []
        return sum(r["pnl"] / r["n"] for _, r in hits) / len(hits), [f"'{t}' {r['wins']}/{r['n']}" for t, r in hits]
    mine, _ = score(model)
    best = None
    for other in (arena._bots or {}):
        if other == model:
            continue
        s, why = score(other)
        if why and s > 0 and s > max(mine, 0) + margin and (best is None or s > best[0]):
            best = (s, other, why)
    if not best:
        return None
    return best[1], f"vai melhor nessa situação ({', '.join(best[2][:3])}; média ${best[0]:+.3f}/trade vs ${mine:+.3f})"


def summary(rec: dict[str, dict]) -> dict:
    """What a bot has learned: situations it avoids, where it does best, how many trade-situations it has seen."""
    good = [(t, r) for t, r in rec.items() if r["n"] >= cfg.LEARN_MIN_TRADES and r["wins"] / r["n"] >= 0.6 and r["pnl"] > 0]
    total = _total(rec)
    return {"avoid": [{"tag": t, **r} for t, r in sorted(rec.items(), key=lambda kv: kv[1]["pnl"]) if bad(r, total)][:5],
            "best": [{"tag": t, **r} for t, r in sorted(good, key=lambda kv: -kv[1]["pnl"])][:3],
            "seen": sum(r["n"] for t, r in rec.items() if t.startswith("moeda "))}
