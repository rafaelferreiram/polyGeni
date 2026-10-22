"""
Data snapshot for the morning / evening trading routine (.claude/skills/trading-routine).

Reads the running dashboard's read-only GET endpoints (so it sees exactly what the bots see) plus public
Hyperliquid candles for setup proximity, and prints one JSON document. It never places or changes anything.
Usage:  python -m src.solana.routine [--base http://127.0.0.1:1914]
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg
from src.solana.market_data import request_json

# Out-of-sample study_v2 (reports/study_v2_2026-10-21.md): mean_reversion_atr, 1h, 5 markets, 156 days.
STUDY = {"trades": 29, "days": 156, "avg_usd_per_100": 0.2085, "win_rate": 0.759, "source": "reports/study_v2_2026-10-21.md"}


def projection(position_usd: float, study: dict = STUDY) -> dict:
    """Expected value of the next 24h from the backtest, scaled to our position size. Honest, not a promise."""
    per_day = study["trades"] / study["days"]
    per_trade = study["avg_usd_per_100"] * position_usd / 100
    return {"expected_trades_24h": round(per_day, 2), "expected_usd_per_trade": round(per_trade, 4),
            "expected_usd_24h": round(per_day * per_trade, 4),
            "most_likely": "0 trades" if per_day < 0.5 else "1 trade",
            "note": f"backtest {study['source']}: {study['trades']} trades em {study['days']} dias, "
                    f"acerto {study['win_rate']:.0%}; resultados passados não garantem futuros"}


def recent(rows: list[dict], key: str, hours: float) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [r for r in rows if r.get(key) and datetime.fromisoformat(r[key]) >= since]


def summarize_account(acc: dict, daily: list[dict], hours: float) -> dict:
    trades = recent(acc.get("trades", []), "exit_ts", hours)
    decs = recent(acc.get("decisions", []), "ts", hours)
    return {
        "running": acc.get("running"), "network": acc.get("network"), "mode": acc.get("decision_mode"),
        "equity": acc.get("equity"), "cash": acc.get("cash"), "contributed": acc.get("contributed"),
        "pnl_total_usd": acc.get("pnl_usd"), "exchange_balance": acc.get("exchange_balance"),
        "exchange_error": acc.get("exchange_error"), "config_problems": (acc.get("config") or {}).get("problems"),
        "kill_switch": acc.get("kill_switch"), "heartbeat": acc.get("heartbeat"), "last_cycle": acc.get("last_cycle"),
        "sleep": acc.get("sleep"), "win_rate_all": acc.get("win_rate"), "profit": acc.get("profit"),
        "open_positions": acc.get("positions"),
        "trades_window": trades,
        "decision_kinds_window": dict(Counter(d.get("kind") for d in decs)),
        "top_reasons_window": Counter(str(d.get("reason") or d.get("detail") or "")[:90] for d in decs).most_common(6),
        "team": [{k: m.get(k) for k in ("name", "trades", "pnl_usd", "open", "last")} for m in acc.get("team", [])],
        "daily": daily[:8],
        "week": {k: round(sum(d.get(k) or 0 for d in daily[:7]), 4) for k in ("trades", "wins", "losses", "pnl_usd", "ai_cost_usd", "net_usd")},
    }


def setups(markets: list[str]) -> list[dict]:
    """How close each market is to the rules' entry (uptrend + dip). Public data only."""
    from src.solana import hyperliquid
    from src.solana.strategies import features
    out = []
    for m in markets:
        try:
            f = features(hyperliquid.candles(m)[:-1])        # closed candles only, like the engine
            out.append({"market": m, **{k: f.get(k) for k in ("price", "rsi14", "atr_pct", "uptrend", "below_lower_band",
                                                              "change_24h_pct", "rules_signal")}})
        except Exception as e:
            out.append({"market": m, "error": str(e)[:120]})
    return out


def snapshot(base: str, hours: float = 24) -> dict:
    get = lambda path: request_json("GET", f"{base}/api/paper{path}", retries=1, timeout=30)
    out = {"generated_at": datetime.now(cfg.TZ).isoformat(timespec="minutes"), "window_hours": hours}
    try:
        for kind in ("real", "sim"):
            out[kind] = summarize_account(get(f"/account?kind={kind}"), get(f"/report/daily?kind={kind}&days=8")["days"], hours)
        out["lab"] = get("/arena").get("leaderboard")
        out["server"] = "online"
    except Exception as e:
        out["server"] = f"OFFLINE ou erro: {str(e)[:160]} — os bots não estão rodando; abrir com `make run`"
    out["setups"] = setups(cfg.HL_MARKETS)        # broad set; the report says which ones the real account trades
    out["real_markets"] = cfg.HL_LIVE_MARKETS
    out["projection"] = projection(cfg.LIVE_MAX_POSITION_USD)
    out["limits"] = {"max_capital_usd": cfg.LIVE_MAX_CAPITAL_USD, "max_position_usd": cfg.LIVE_MAX_POSITION_USD,
                     "max_daily_loss_pct": cfg.LIVE_MAX_DAILY_LOSS_PCT, "max_entries_per_day": cfg.LIVE_MAX_ENTRIES_PER_DAY,
                     "ai_budget_month_usd": cfg.LLM_MONTHLY_BUDGET_USD}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:1914")
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    print(json.dumps(snapshot(a.base, a.hours), indent=1, default=str, ensure_ascii=False))
