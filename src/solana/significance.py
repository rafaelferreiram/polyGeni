"""
Rule-significance test before promotion (Jesse's "is it skill or luck?"): a bot's realized PnL per lab trade must beat
what random entries with the same exit logic on the same candles would have made. Each closed trade gives its coin,
its stop and target distances (from the entry fill), how long it was held and its fee rate; a random twin enters at
the close of a random closed bar of that coin's last 400 candles and exits the same way — stop, target, or the same
holding time. SAMPLES random books of the bot's trades are drawn; p = share of them whose mean PnL/trade is at least
the bot's. p < P_MAX means the record is unlikely to be luck.

POINT-IN-TIME (§8.3): each trade's twins enter inside that trade's OWN window of candles (±ERA bars around its
entry, one fetch per coin per day of trading), so a trade made in October is judged against October's regime, not
against whatever the latest 400 bars look like (look-back drift). When a window cannot be fetched the latest bars
are used and the result says how many trades were approximated (`approx_windows`) — never silently.
"""
import math
import random
from datetime import datetime

from src.solana import hyperliquid as hl

SAMPLES = 200
P_MAX = 0.05
VERSION = 2
ERA_BEFORE, ERA_AFTER = 350, 50          # twin entries draw from [entry − 350 bars, entry + 50 bars]


def _twin(bars: list[dict], stop_pct: float, tp_pct: float, hold: int, fee_pct: float) -> float:
    """PnL fraction of one random entry at bars[0]'s close, exits as the real trade's rules."""
    entry = bars[0]["close"]
    stop, tp = entry * (1 - stop_pct), entry * (1 + tp_pct)
    for k in bars[1:hold + 1]:
        if k["low"] <= stop:
            return -stop_pct - fee_pct
        if k["high"] >= tp:
            return tp_pct - fee_pct
    return bars[min(hold, len(bars) - 1)]["close"] / entry - 1 - fee_pct


def p_value(ledger, interval: str = "1h", candles_fn=None, samples: int = SAMPLES, seed: int = 0,
            window_fn=None) -> dict:
    step = hl.INTERVAL_S.get(interval, 3600)
    fn = candles_fn or (lambda coin: hl.candles(coin, interval))
    if window_fn is None and candles_fn is None:   # live: fetch each trade's own era (point in time)
        window_fn = lambda coin, s_ms, e_ms: hl.candles(coin, interval, start_ms=s_ms, end_ms=e_ms)
    specs, latest, windows, approx = [], {}, {}, 0
    for t in ledger.closed_trades():
        f = ledger.get_fill(t.get("entry_order_id") or "")
        if not f or not f.get("stop_price") or not f.get("tp_price") or not t["mint"].startswith("hl:") or hl.is_short(t["mint"]):
            continue
        coin = hl.coin_of(t["mint"])
        entry_ep = datetime.fromisoformat(t["entry_ts"]).timestamp()
        bars = None
        if window_fn:
            key = (coin, int(entry_ep) // 86400)             # one fetch per coin per trading day
            if key not in windows:
                try:
                    windows[key] = window_fn(coin, int((entry_ep - ERA_BEFORE * step) * 1000),
                                             int((entry_ep + ERA_AFTER * step) * 1000))
                except Exception:
                    windows[key] = None
            bars = windows[key]
        if not bars or len(bars) < 60:                        # window unavailable: latest bars, counted as approximate
            approx += 1 if window_fn else 0
            if coin not in latest:
                latest[coin] = fn(coin)[:-1]                  # closed bars only
            bars = latest[coin]
        held_s = (datetime.fromisoformat(t["exit_ts"]) - datetime.fromisoformat(t["entry_ts"])).total_seconds()
        specs.append((bars, 1 - f["stop_price"] / f["price"], f["tp_price"] / f["price"] - 1, max(1, math.ceil(held_s / step)),
                      t["fees_usd"] / t["cost_usd"] if t["cost_usd"] else 0.0, t["pnl_pct"]))
    if not specs:
        return {"p": None, "trades": 0, "samples": samples}
    real = sum(s[-1] for s in specs) / len(specs)
    rng, beats, means = random.Random(seed), 0, []
    for _ in range(samples):
        tot = 0.0
        for bars, stop, tp, hold, fee, _ in specs:
            i = rng.randrange(len(bars) - hold) if len(bars) > hold else 0
            tot += _twin(bars[i:], stop, tp, hold, fee)
        means.append(tot / len(specs))
        beats += means[-1] >= real
    return {"p": round(beats / samples, 3), "trades": len(specs), "samples": samples, "real_pct": round(real * 100, 3),
            "random_pct": round(sum(means) / len(means) * 100, 3), "version": VERSION, "interval": interval,
            "point_in_time": bool(window_fn), "approx_windows": approx,
            "methodology": "twins enter at random bars of each trade's own era, exit by the trade's stop/target/hold"}
