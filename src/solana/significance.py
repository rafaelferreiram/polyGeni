"""
Rule-significance test before promotion (Jesse's "is it skill or luck?"): a bot's realized PnL per lab trade must beat
what random entries with the same exit logic on the same candles would have made. Each closed trade gives its coin,
its stop and target distances (from the entry fill), how long it was held and its fee rate; a random twin enters at
the close of a random closed bar of that coin's last 400 candles and exits the same way — stop, target, or the same
holding time. SAMPLES random books of the bot's trades are drawn; p = share of them whose mean PnL/trade is at least
the bot's. p < P_MAX means the record is unlikely to be luck.
shortcut: the random twins use the coin's latest 400 bars, not the exact bars of each trade, so the regime may differ;
upgrade to fetching each trade's own window if a bot's trades span many weeks.
"""
import math
import random
from datetime import datetime

from src.solana import hyperliquid as hl

SAMPLES = 200
P_MAX = 0.05


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


def p_value(ledger, interval: str = "1h", candles_fn=None, samples: int = SAMPLES, seed: int = 0) -> dict:
    step = hl.INTERVAL_S.get(interval, 3600)
    fn = candles_fn or (lambda coin: hl.candles(coin, interval))
    specs, candles = [], {}
    for t in ledger.closed_trades():
        f = ledger.get_fill(t.get("entry_order_id") or "")
        if not f or not f.get("stop_price") or not f.get("tp_price") or not t["mint"].startswith("hl:") or hl.is_short(t["mint"]):
            continue
        coin = hl.coin_of(t["mint"])
        if coin not in candles:
            candles[coin] = fn(coin)[:-1]                    # closed bars only
        held_s = (datetime.fromisoformat(t["exit_ts"]) - datetime.fromisoformat(t["entry_ts"])).total_seconds()
        specs.append((coin, 1 - f["stop_price"] / f["price"], f["tp_price"] / f["price"] - 1, max(1, math.ceil(held_s / step)),
                      t["fees_usd"] / t["cost_usd"] if t["cost_usd"] else 0.0, t["pnl_pct"]))
    if not specs:
        return {"p": None, "trades": 0, "samples": samples}
    real = sum(s[-1] for s in specs) / len(specs)
    rng, beats, means = random.Random(seed), 0, []
    for _ in range(samples):
        tot = 0.0
        for coin, stop, tp, hold, fee, _ in specs:
            bars = candles[coin]
            i = rng.randrange(len(bars) - hold) if len(bars) > hold else 0
            tot += _twin(bars[i:], stop, tp, hold, fee)
        means.append(tot / len(specs))
        beats += means[-1] >= real
    return {"p": round(beats / samples, 3), "trades": len(specs), "samples": samples, "real_pct": round(real * 100, 3),
            "random_pct": round(sum(means) / len(means) * 100, 3)}
