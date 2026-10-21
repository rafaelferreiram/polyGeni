"""
Walk-forward backtest of the deterministic strategies on public Binance 1h candles,
net of fees/slippage, always against SOL buy-and-hold over the same out-of-sample span.

    python -m src.solana.backtest --days 180                          # Solana tokens (Binance) -> reports/
    python -m src.solana.backtest --venue hyperliquid --interval 1h   # HL perps incl. xyz:GOLD/SILVER, with funding

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

from src.solana import config as cfg
from src.solana import hyperliquid as hl
from src.solana import market_data as md
from src.solana.strategies import MeanReversion, MomentumBreakout, RegimeFiltered

SYMBOLS = ["SOLUSDT", "JUPUSDT", "WIFUSDT", "RAYUSDT", "JTOUSDT", "PYTHUSDT"]  # BONK skipped: 8-dp price ticks
REGIMES = [None, 200]   # regime filter on/off is itself chosen in-sample


def grids(stop_mult: float = 1.0) -> dict:
    def wrap(s, n):
        s.stop_pct, s.tp_pct = s.stop_pct * stop_mult, s.tp_pct * stop_mult
        return RegimeFiltered(s, n) if n else s
    return {
        "momentum": [wrap(MomentumBreakout(lookback=lb, vol_mult=vm), n)
                     for lb, vm, n in itertools.product([24, 48], [1.5, 2.0], REGIMES)],
        "mean_reversion": [wrap(MeanReversion(k=k, rsi_max=r), n)
                           for k, r, n in itertools.product([2.0, 2.5], [25, 30], REGIMES)],
    }


GRIDS = grids()
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
             cost_bps: float | dict = COST_BPS, fee: float = FEE_USD, pos_pct: float = POS_PCT,
             max_hold: int = MAX_HOLD_BARS, funding: dict | None = None, bar_hours: float = 1.0) -> dict:
    """Trade bars [start, end) of aligned candle lists; bars before start are indicator warm-up.
    cost_bps may be per symbol; funding = {symbol: {hour_epoch_s: hourly_rate}} charged on open longs."""
    cash, positions, pending, equity, trades = capital, {}, {}, [], []
    costs = cost_bps if isinstance(cost_bps, dict) else {s: cost_bps for s in data}
    for i in range(start, end):
        # 1) fills for signals from the previous closed bar, at this bar's open
        for sym, sig in pending.items():
            bar = data[sym][i]
            eq_now = cash + sum(p["qty"] * data[s][i]["open"] for s, p in positions.items())
            size = min(eq_now * pos_pct, cash - fee)
            c = costs[sym] / 1e4
            if size > 1 and sym not in positions:
                px = bar["open"] * (1 + c)
                positions[sym] = {"qty": size / px, "cost": size + fee, "entry_i": i,
                                  "stop": bar["open"] * (1 - sig.stop_pct), "tp": bar["open"] * (1 + sig.tp_pct)}
                cash -= size + fee
        pending = {}
        # 2) exits on this bar
        for sym in list(positions):
            p, bar = positions[sym], data[sym][i]
            c = costs[sym] / 1e4
            if funding and sym in funding:
                fund = p["qty"] * bar["close"] * funding[sym].get(bar["t"] // 3600 * 3600, 0.0) * bar_hours
                cash -= fund
                p["cost"] += fund   # attribute funding to the trade's P&L
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
        equity.append(cash + sum(p["qty"] * data[s][i]["close"] * (1 - costs[s] / 1e4) for s, p in positions.items()))
        # 4) signals on the closed bar -> fill next bar
        if i < end - 1:
            for sym, candles in data.items():
                if sym not in positions:
                    sig = strategy.signal(candles[max(0, i - WINDOW):i + 1])
                    if sig:
                        pending[sym] = sig
    return {"equity": equity, "trades": trades}


def hold(candles: list[dict], start: int, end: int, capital: float = 100.0, cost_bps: float = COST_BPS) -> dict:
    c = cost_bps / 1e4
    qty = (capital - FEE_USD) / (candles[start]["open"] * (1 + c))
    return {"equity": [qty * candles[i]["close"] * (1 - c) - FEE_USD for i in range(start, end)], "trades": []}


def align(raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
    common = set.intersection(*(set(k["t"] for k in v) for v in raw.values()))
    return {s: [k for k in v if k["t"] in common] for s, v in raw.items()}


def walk_forward(data: dict[str, list[dict]], folds: int = 4, benchmarks=("SOLUSDT",), costs=COST_BPS,
                 funding=None, bar_hours: float = 1.0, stop_mult: float = 1.0) -> dict:
    n = len(next(iter(data.values())))
    edges = [round(n * k / folds) for k in range(folds + 1)]
    kw = dict(cost_bps=costs, funding=funding, bar_hours=bar_hours, max_hold=round(MAX_HOLD_BARS / bar_hours))
    out = {}
    for name, grid in grids(stop_mult).items():
        curve, trades, chosen, per_fold = [100.0], [], [], []
        for k in range(1, folds):
            ins = [(s, _metrics(r["equity"], r["trades"])) for s in grid
                   for r in [simulate(s, data, max(edges[k - 1], 50), edges[k], **kw)]]
            best = max(ins, key=lambda x: (x[1]["sharpe"], x[1]["return_pct"]))[0]
            res = simulate(best, data, edges[k], edges[k + 1], capital=curve[-1], **kw)
            curve += res["equity"]
            trades += res["trades"]
            inner = getattr(best, "inner", best)
            params = {f: getattr(inner, f) for f in ("lookback", "vol_mult", "k", "rsi_max") if hasattr(inner, f)}
            params["regime_sma"] = getattr(best, "n", None)
            chosen.append(params)
            per_fold.append({"fold": k, "params": params, **_metrics([curve[-len(res["equity"]) - 1]] + res["equity"], res["trades"])})
        out[name] = {"oos": _metrics(curve, trades), "folds": per_fold, "params": chosen}
    for b in benchmarks:
        cb = costs[b] if isinstance(costs, dict) else costs
        out[f"hold_{b}"] = {"oos": _metrics([100.0] + hold(data[b], edges[1], n, cost_bps=cb)["equity"], [])}
    first = next(iter(data.values()))
    out["_span"] = {"oos_start": datetime.fromtimestamp(first[edges[1]]["t"], timezone.utc).isoformat(),
                    "oos_end": datetime.fromtimestamp(first[-1]["t"], timezone.utc).isoformat(),
                    "bars": n - edges[1], "folds": folds}
    return out


HL_SPREAD_BPS = 2.0     # half-spread + book walk for small sizes on liquid HL books (taker fee added per market)


def report(res: dict, title: str, setup: list[str]) -> str:
    span = res["_span"]
    holds = [k for k in res if k.startswith("hold_")]
    lines = [f"# {title} ({datetime.now(timezone.utc).date()})", "",
             "> PAPER research only. Backtests overfit; a strategy must also beat hold in weeks of live paper trading.", "",
             *[f"- {x}" for x in setup],
             f"- Out-of-sample span: {span['oos_start']} → {span['oos_end']} ({span['bars']} bars, {span['folds'] - 1} OOS folds)",
             "- Walk-forward: each fold trades the parameter set (incl. regime filter SMA200 on/off) with the best "
             "in-sample Sharpe on the previous fold; next-bar-open fills; stop wins if stop and TP touch in one bar", "",
             "| Strategy / benchmark | Net return | Max DD | Sharpe | Trades | Win rate |",
             "|---|---:|---:|---:|---:|---:|"]
    for name in ("momentum", "mean_reversion", *holds):
        m = res[name]["oos"]
        wr = "–" if m["win_rate"] is None else f"{m['win_rate']}%"
        lines.append(f"| {name} | {m['return_pct']}% | {m['max_dd_pct']}% | {m['sharpe']} | {m['trades']} | {wr} |")
    lines += ["", "## Per fold (parameters chosen in-sample on the previous fold)", ""]
    for name in ("momentum", "mean_reversion"):
        for f in res[name]["folds"]:
            lines.append(f"- {name} fold {f['fold']}: params {f['params']} → {f['return_pct']}% "
                         f"(DD {f['max_dd_pct']}%, Sharpe {f['sharpe']}, {f['trades']} trades)")
    best = max(("momentum", "mean_reversion"), key=lambda s: res[s]["oos"]["return_pct"])
    beaten = [h for h in holds if res[best]["oos"]["return_pct"] > res[h]["oos"]["return_pct"]]
    lines += ["", "## Verdict", "",
              f"Best strategy OOS: **{best}** ({res[best]['oos']['return_pct']}%). Beats: "
              f"{', '.join(beaten) or 'no buy-and-hold benchmark'}; loses to: "
              f"{', '.join(h for h in holds if h not in beaten) or 'none'}.",
              "", "Do not go live on this basis alone: the phase-3 gate is 4–8 weeks of 24/7 paper trading with real "
              "quotes that beats buy-and-hold after costs, with drawdown inside limits."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=["solana", "hyperliquid"], default="solana")
    ap.add_argument("--interval", default="1h", help="hyperliquid only: 1h or 15m (max 5000 candles)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()
    end = int(time.time() * 1000)
    if args.venue == "solana":
        raw = {s: md.binance_candles(s, "1h", end - args.days * 86_400_000, end) for s in SYMBOLS}
        res = walk_forward(align(raw), args.folds, benchmarks=("SOLUSDT",))
        stem = f"backtest_{datetime.now(timezone.utc).date()}"
        text = report(res, "Backtest walk-forward — Solana tokens vs hold SOL", [
            f"Data: Binance 1h klines, last {args.days} days, {', '.join(SYMBOLS)}",
            f"Costs per side: {COST_BPS} bps (slippage + spread/impact) + ${FEE_USD} network fee; sizing "
            f"{POS_PCT:.0%} of strategy capital per position; max hold {MAX_HOLD_BARS}h"])
    else:
        bar_h = {"1h": 1.0, "15m": 0.25}[args.interval]
        days = min(args.days, int(5000 * bar_h / 24))
        start = end - days * 86_400_000
        raw = {m: hl.candles(m, args.interval, start, end) for m in cfg.HL_MARKETS}
        costs = {m: hl.taker_fee_bps(hl.asset_ctx(m)) + HL_SPREAD_BPS for m in cfg.HL_MARKETS}
        funding = {m: hl.funding_history(m, start, end) for m in cfg.HL_MARKETS}
        stop_mult = cfg.MARKET_PROFILES.get("hyperliquid", {}).get("stop_mult", 1.0)
        res = walk_forward(align(raw), args.folds, benchmarks=tuple(cfg.HL_MARKETS), costs=costs,
                           funding=funding, bar_hours=bar_h, stop_mult=stop_mult)
        stem = f"backtest_hyperliquid_{args.interval}_{datetime.now(timezone.utc).date()}"
        text = report(res, f"Backtest walk-forward — Hyperliquid perps {args.interval} (1x long) vs buy-and-hold", [
            f"Data: Hyperliquid candleSnapshot {args.interval}, last {days} days, {', '.join(cfg.HL_MARKETS)}",
            "Costs per side: taker fee per market (" + ", ".join(f"{m} {c - HL_SPREAD_BPS:.2f}" for m, c in costs.items())
            + f" bps) + {HL_SPREAD_BPS} bps spread/slippage; hourly funding from fundingHistory charged on open longs",
            f"Stops/TPs x{stop_mult} (market profile); sizing {POS_PCT:.0%} of strategy capital; "
            f"max hold {MAX_HOLD_BARS}h; benchmarks = buy-and-hold of each asset (perp, no funding in hold)"])
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, stem)
    with open(stem + ".md", "w") as f:
        f.write(text)
    with open(stem + ".json", "w") as f:
        json.dump(res, f, indent=1)
    print(text)


if __name__ == "__main__":
    main()
