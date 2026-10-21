"""
Walk-forward backtest of the deterministic strategies on public Binance 1h candles,
net of fees/slippage, always against SOL buy-and-hold over the same out-of-sample span.

    python -m src.solana.backtest --days 180      # writes reports/backtest_<date>.md (+ .json)

Walk-forward: history is cut into N folds. For each fold k>=1, every parameter set in the grid is run
on fold k-1 (in-sample) and the best Sharpe is then traded on fold k (out-of-sample). Only OOS results
are reported; fold equity curves are chained.
Execution model: signal on a closed bar -> fill at next bar open; stop/TP checked on bar high/low
(if both touch in one bar, stop wins); max holding time; per-side cost = bps + fixed network fee.
"""
import argparse
import itertools
import json
import math
import os
import time
from datetime import datetime, timezone

from src.solana import market_data as md
from src.solana.strategies import MeanReversion, MomentumBreakout

SYMBOLS = ["SOLUSDT", "JUPUSDT", "WIFUSDT", "RAYUSDT", "JTOUSDT", "PYTHUSDT"]  # BONK skipped: 8-dp price ticks
GRIDS = {
    "momentum": [MomentumBreakout(lookback=lb, vol_mult=vm) for lb, vm in itertools.product([24, 48], [1.5, 2.0])],
    "mean_reversion": [MeanReversion(k=k, rsi_max=r) for k, r in itertools.product([2.0, 2.5], [25, 30])],
}
COST_BPS = 60          # per side: 30 sim slippage + ~30 spread/price impact on mid-size Solana pools
FEE_USD = 0.02         # per side: base + priority fee (~0.0001 SOL) with margin
POS_PCT = 0.20         # of strategy capital per position (== 10% of total with 50/50 split)
MAX_HOLD_BARS = 72
WINDOW = 300           # bars of history handed to the strategy


def _metrics(equity: list[float], trades: list[float]) -> dict:
    rets = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    mu = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets)) if rets else 0
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return {"return_pct": round((equity[-1] / equity[0] - 1) * 100, 2), "max_dd_pct": round(mdd * 100, 2),
            "sharpe": round(mu / sd * math.sqrt(24 * 365), 2) if sd else 0.0,
            "trades": len(trades), "win_rate": round(sum(t > 0 for t in trades) / len(trades) * 100, 1) if trades else None}


def simulate(strategy, data: dict[str, list[dict]], start: int, end: int, capital: float = 100.0,
             cost_bps: float = COST_BPS, fee: float = FEE_USD, pos_pct: float = POS_PCT,
             max_hold: int = MAX_HOLD_BARS) -> dict:
    """Trade bars [start, end) of aligned candle lists; bars before start are indicator warm-up."""
    cash, positions, pending, equity, trades = capital, {}, {}, [], []
    c = cost_bps / 1e4
    for i in range(start, end):
        # 1) fills for signals from the previous closed bar, at this bar's open
        for sym, sig in pending.items():
            bar = data[sym][i]
            eq_now = cash + sum(p["qty"] * data[s][i]["open"] for s, p in positions.items())
            size = min(eq_now * pos_pct, cash - fee)
            if size > 1 and sym not in positions:
                px = bar["open"] * (1 + c)
                positions[sym] = {"qty": size / px, "cost": size + fee, "entry_i": i,
                                  "stop": bar["open"] * (1 - sig.stop_pct), "tp": bar["open"] * (1 + sig.tp_pct)}
                cash -= size + fee
        pending = {}
        # 2) exits on this bar
        for sym in list(positions):
            p, bar = positions[sym], data[sym][i]
            exit_px = None
            if bar["low"] <= p["stop"]:
                exit_px = min(p["stop"], bar["open"])     # gap below stop fills at the open
            elif bar["high"] >= p["tp"]:
                exit_px = max(p["tp"], bar["open"])
            elif i - p["entry_i"] >= max_hold:
                exit_px = bar["close"]
            if exit_px is not None:
                proceeds = p["qty"] * exit_px * (1 - c) - fee
                trades.append(proceeds - p["cost"])
                cash += proceeds
                del positions[sym]
        # 3) mark to market at close
        equity.append(cash + sum(p["qty"] * data[s][i]["close"] * (1 - c) for s, p in positions.items()))
        # 4) signals on the closed bar -> fill next bar
        if i < end - 1:
            for sym, candles in data.items():
                if sym not in positions:
                    sig = strategy.signal(candles[max(0, i - WINDOW):i + 1])
                    if sig:
                        pending[sym] = sig
    return {"equity": equity, "trades": trades}


def hold(candles: list[dict], start: int, end: int, capital: float = 100.0) -> dict:
    c = COST_BPS / 1e4
    qty = (capital - FEE_USD) / (candles[start]["open"] * (1 + c))
    return {"equity": [qty * candles[i]["close"] * (1 - c) - FEE_USD for i in range(start, end)], "trades": []}


def align(raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
    common = set.intersection(*(set(k["t"] for k in v) for v in raw.values()))
    return {s: [k for k in v if k["t"] in common] for s, v in raw.items()}


def walk_forward(data: dict[str, list[dict]], folds: int = 4) -> dict:
    n = len(next(iter(data.values())))
    edges = [round(n * k / folds) for k in range(folds + 1)]
    out = {}
    for name, grid in GRIDS.items():
        curve, trades, chosen, per_fold = [100.0], [], [], []
        for k in range(1, folds):
            ins = [(s, _metrics(r["equity"], r["trades"])) for s in grid
                   for r in [simulate(s, data, max(edges[k - 1], 50), edges[k])]]
            best = max(ins, key=lambda x: (x[1]["sharpe"], x[1]["return_pct"]))[0]
            res = simulate(best, data, edges[k], edges[k + 1], capital=curve[-1])
            curve += res["equity"]
            trades += res["trades"]
            params = {f: getattr(best, f) for f in ("lookback", "vol_mult", "k", "rsi_max") if hasattr(best, f)}
            chosen.append(params)
            per_fold.append({"fold": k, "params": params, **_metrics([curve[-len(res["equity"]) - 1]] + res["equity"], res["trades"])})
        out[name] = {"oos": _metrics(curve, trades), "folds": per_fold, "params": chosen}
    sol = data["SOLUSDT"]
    out["hold_SOL"] = {"oos": _metrics([100.0] + hold(sol, edges[1], n)["equity"], [])}
    out["_span"] = {"oos_start": datetime.fromtimestamp(sol[edges[1]]["t"], timezone.utc).isoformat(),
                    "oos_end": datetime.fromtimestamp(sol[-1]["t"], timezone.utc).isoformat(),
                    "bars": n - edges[1], "folds": folds}
    return out


def report(res: dict, days: int) -> str:
    span = res["_span"]
    lines = [
        f"# Backtest walk-forward — Solana strategies vs hold SOL ({datetime.now(timezone.utc).date()})",
        "",
        "> PAPER research only. Past performance on 6 liquid Solana tokens (Binance 1h) does not transfer to memecoins.",
        "",
        f"- Data: Binance 1h klines, last {days} days, {', '.join(SYMBOLS)}",
        f"- Out-of-sample span: {span['oos_start']} → {span['oos_end']} ({span['bars']} bars, {span['folds'] - 1} OOS folds)",
        f"- Costs per side: {COST_BPS} bps (slippage + spread/impact) + ${FEE_USD} network fee; "
        f"sizing {POS_PCT:.0%} of strategy capital per position; max hold {MAX_HOLD_BARS}h; next-bar-open fills; "
        "stop wins if stop and TP touch in the same bar",
        "",
        "| Strategy | Net return | Max DD | Sharpe | Trades | Win rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("momentum", "mean_reversion", "hold_SOL"):
        m = res[name]["oos"]
        wr = "–" if m["win_rate"] is None else f"{m['win_rate']}%"
        lines.append(f"| {name} | {m['return_pct']}% | {m['max_dd_pct']}% | {m['sharpe']} | {m['trades']} | {wr} |")
    lines += ["", "## Per fold (parameters chosen in-sample on the previous fold)", ""]
    for name in ("momentum", "mean_reversion"):
        for f in res[name]["folds"]:
            lines.append(f"- {name} fold {f['fold']}: params {f['params']} → {f['return_pct']}% "
                         f"(DD {f['max_dd_pct']}%, Sharpe {f['sharpe']}, {f['trades']} trades)")
    best = max(("momentum", "mean_reversion"), key=lambda s: res[s]["oos"]["return_pct"])
    beat = res[best]["oos"]["return_pct"] > res["hold_SOL"]["oos"]["return_pct"]
    lines += ["", "## Verdict", "",
              f"Best strategy OOS: **{best}** ({res[best]['oos']['return_pct']}%) vs hold SOL "
              f"({res['hold_SOL']['oos']['return_pct']}%) → **{'beats' if beat else 'does NOT beat'} buy-and-hold SOL**.",
              "", "Do not go live on this basis alone: the gate for phase 3 is 4–8 weeks of 24/7 paper trading "
              "with real quotes that beats hold SOL after costs, with drawdown inside limits."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()
    end = int(time.time() * 1000)
    raw = {s: md.binance_candles(s, "1h", end - args.days * 86_400_000, end) for s in SYMBOLS}
    res = walk_forward(align(raw), args.folds)
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, f"backtest_{datetime.now(timezone.utc).date()}")
    with open(stem + ".md", "w") as f:
        f.write(report(res, args.days))
    with open(stem + ".json", "w") as f:
        json.dump(res, f, indent=1)
    print(open(stem + ".md").read())


if __name__ == "__main__":
    main()
