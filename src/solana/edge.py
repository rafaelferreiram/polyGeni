"""
Margin of error before every buy: how likely is this trade to end in profit, with what margin, and is it worth it?
  p      = chance of a winning trade in this situation (same labels: trend, RSI band, setups, coin), from the bot's own
           realized trades and the whole team's — real exits included (quick wins, trailing stop, time limits). With
           no evidence p is a neutral prior. (The training center is not used here: it only counts "full target before
           stop within 24h", which our exits make far less important — it would understate the odds.)
  margin = ±1.96·sqrt(p(1−p)/n): little evidence, wide margin.
  EV     = p·average win − (1−p)·average loss, from the bot's own realized trades (the team's while it has few).
The bot skips the trade only when it is confident this situation is a loser: enough evidence (n ≥ EDGE_MIN_EVIDENCE),
even the optimistic end of the margin has a negative EV, and that optimistic chance is still below the bot's own
usual win rate (so it judges the situation, not the strategy — the 12h cut and the titular choice judge strategies).
Otherwise it buys and says its odds.
"""
from math import sqrt

from src.solana import config as cfg

PRIOR_P, PRIOR_N = 0.5, 4


def estimate(tags: list[str], own: dict, team: dict, avg_win: float, avg_loss: float, baseline: float | None = None) -> dict:
    """Only *specific* situations count (a label on at most half of a record's trades): a label on almost every trade
    ('alta', the bot's own setup) is the strategy itself — whether the strategy pays is the 12h cut's job, not this."""
    from src.solana.learning import _total
    rates, used = [], set()
    for src, rec in (("próprio", own), ("time", team)):
        total = _total(rec)
        for t in tags:
            r = rec.get(t)
            if t.startswith("janela") and (src == "time" or r and r.get("days", 0) < 2):      # a time window only counts from the bot's own multi-day record
                continue
            if r and r.get("n") and r["n"] <= total / 2:
                rates.append((r["wins"], r["n"]))
                used.add(src)
    evidence = max((k for _, k in rates), default=0)                  # the same trades carry several labels
    p_obs = sum(w for w, _ in rates) / sum(k for _, k in rates) if rates else PRIOR_P
    n = evidence + PRIOR_N
    p = (p_obs * evidence + PRIOR_P * PRIOR_N) / n
    margin = 1.96 * sqrt(p * (1 - p) / n)
    ev = lambda q: q * avg_win - (1 - q) * avg_loss
    best = min(1.0, p + margin)
    # skip only a situation clearly worse than this bot's usual (even its optimistic odds below its average) that loses
    worse = baseline is None or best < baseline
    return {"p": round(p, 3), "margin": round(margin, 3), "evidence": round(evidence), "ev_usd": round(ev(p), 4),
            "ev_best_usd": round(ev(best), 4), "sources": sorted(used), "baseline": baseline,
            "skip": evidence >= cfg.EDGE_MIN_EVIDENCE and ev(best) < 0 and worse}


def payoff(trades: list[dict], fallback: tuple[float, float] = (0.10, 0.10)) -> tuple[float, float]:
    """Average win and average loss ($) of realized trades; a neutral fallback when there are too few."""
    w = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    l = [-t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    if len(w) < 3 or len(l) < 3:
        return fallback
    return sum(w) / len(w), sum(l) / len(l)


def describe(e: dict) -> str:
    return f"chance {e['p']:.0%} (±{e['margin']:.0%}), valor esperado {'+' if e['ev_usd'] >= 0 else '−'}${abs(e['ev_usd']):.3f} por trade"
