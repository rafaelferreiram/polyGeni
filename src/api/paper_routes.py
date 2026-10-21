"""
/api/paper — Solana PAPER trading state. Every response carries mode=PAPER.
Deposits/withdrawals here move SIMULATED money only; wallets are watch-only.
"""
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from src.solana import config as cfg
from src.solana import engine
from src.solana import market_data as md

router = APIRouter()
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# State-changing endpoints: loopback clients only, loopback Host header (anti DNS-rebinding),
# and a per-process token the dashboard fetches same-origin (cross-site pages cannot read it => CSRF-safe).
LOCAL_CLIENTS = set(os.getenv("PAPER_TRUSTED_CLIENTS", "127.0.0.1,::1").split(","))
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
API_TOKEN = os.getenv("PAPER_API_TOKEN") or secrets.token_urlsafe(24)


def _require_loopback(request: Request):
    client = request.client.host if request.client else ""
    if client not in LOCAL_CLIENTS or (request.url.hostname or "") not in LOCAL_HOSTNAMES:
        raise HTTPException(403, "paper controls are only available from this machine (127.0.0.1)")


def local_only(request: Request, x_paper_token: str | None = Header(default=None)):
    _require_loopback(request)
    if not hmac.compare_digest(x_paper_token or "", API_TOKEN):
        raise HTTPException(403, "missing or invalid X-Paper-Token")


LOCAL = [Depends(local_only)]


@router.get("/token")
def token(request: Request):
    _require_loopback(request)
    return {"token": API_TOKEN}


def _led():
    return engine.get_engine().ledger


def twr(rows: list[dict]) -> float:
    """Time-weighted return: chain sub-period returns, treating each deposit/withdrawal (change in
    `contributed`) as happening at the start of its interval, so cash flows never look like profit."""
    growth = 1.0
    for a, b in zip(rows, rows[1:]):
        base = a["equity"] + ((b["contributed"] or 0) - (a["contributed"] or 0))
        if base > 0:
            growth *= b["equity"] / base
    return growth - 1


def _ret(rows: list[dict], key: str) -> float | None:
    px = [r[key] for r in rows if r.get(key)]
    return px[-1] / px[0] - 1 if len(px) >= 2 else None


def performance(history: list[dict], now: datetime | None = None) -> dict:
    """Gain (USD, excl. cash flows), TWR and buy-and-hold SOL/BTC over day / week / month / all."""
    if not history:
        return {}
    now = now or datetime.now(timezone.utc)
    last = history[-1]
    out = {}
    for key, delta in (("day", timedelta(days=1)), ("week", timedelta(days=7)),
                       ("month", timedelta(days=30)), ("all", None)):
        start = 0 if delta is None else next(
            (i for i, h in enumerate(history) if datetime.fromisoformat(h["ts"]) >= now - delta), len(history) - 1)
        rows = history[max(0, start - 1) if delta else 0:]   # include the snapshot just before the window
        base = rows[0]
        gain = (last["equity"] - (last["contributed"] or 0)) - (base["equity"] - (base["contributed"] or 0))
        out[key] = {"gain_usd": round(gain, 4), "twr": round(twr(rows), 6),
                    "hold_sol": _ret(rows, "sol_price"), "hold_btc": _ret(rows, "btc_price"), "since": base["ts"]}
    return out


def _max_drawdown(history: list[dict]) -> float:
    peak, mdd, growth = None, 0.0, 1.0
    for a, b in zip(history, history[1:]):
        base = a["equity"] + ((b["contributed"] or 0) - (a["contributed"] or 0))
        growth *= b["equity"] / base if base > 0 else 1
        peak = max(peak or growth, growth)
        mdd = max(mdd, 1 - growth / peak)
    return mdd


@router.get("/status")
def status():
    eng = engine.get_engine()
    led = eng.ledger
    p = eng.portfolio()
    trades = led.closed_trades()
    wins = [t for t in trades if t["pnl_usd"] > 0]
    unrealized = sum((x["mark_value"] or x["cost_usd"]) - x["cost_usd"] for x in led.positions())
    return {
        "mode": "PAPER",
        "live_trading": False,
        "running": engine.is_running(),
        "heartbeat": led.get_state("heartbeat"),
        "heartbeat_ok": engine.heartbeat_ok(),
        "cycle": eng.cycle,
        "cycle_interval_sec": cfg.PAPER_CYCLE_SEC,
        "last_cycle": led.get_state("last_cycle"),
        "kill_switch": eng.kill_switch(),
        "equity": round(p.equity, 4),
        "cash": round(p.cash, 4),
        "exposure": round(p.exposure, 4),
        "contributed": round(led.contributed(), 4),
        "total_pnl": round(p.equity - led.contributed(), 4),
        "realized_pnl": round(led.realized_pnl(), 4),
        "unrealized_pnl": round(unrealized, 4),
        "closed_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "performance": performance(led.equity_history(100_000)),
        "venues": eng.executor.venues,
        "paused": eng.paused(),
        "debate_llm": {"enabled": eng.debate.enabled, "model": cfg.DEBATE_MODEL if eng.debate.enabled else None,
                       "mcp_servers": bool(cfg.DEBATE_MCP_SERVERS)},
        "limits": {"max_position_pct": eng.risk.max_pos_pct, "max_exposure_pct": eng.risk.max_exposure_pct,
                   "max_positions": eng.risk.max_positions, "max_daily_loss_pct": eng.risk.max_daily_loss_pct,
                   "min_pair_age_h": eng.safety.min_age_h},
    }


@router.get("/agents")
def agents():
    eng = engine.get_engine()
    state = eng.ledger.get_state("agents", {})
    trades = eng.ledger.closed_trades()
    roles = {
        "scout": "Finds trending/watchlist tokens with liquidity & volume",
        "safety": "Anti-rug: rejects by default (age, liquidity, authorities, holders)",
        **{s.name: f"Deterministic strategy, {s.capital_pct:.0%} of capital" for s in eng.strategies},
        "research": "Analyst desk: on-chain/flow metrics + LLM web/MCP research — can only VETO",
        "debate": "LLM bull vs bear with recent news — can only VETO",
        "risk": "Deterministic limits & sizing — final VETO, kill switch",
        "executor": "Only agent allowed to talk to the PaperBroker",
    }
    out = []
    for name, role in roles.items():
        s = state.get(name, {})
        row = {"name": name, "role": role, **s}
        strat = next((x for x in eng.strategies if x.name == name), None)
        if strat:
            row["capital_usd"] = round(eng.portfolio().equity * strat.capital_pct, 2)
        mine = [t for t in trades if t["strategy"] == name]
        if mine:
            row.update(trades=len(mine), pnl_usd=round(sum(t["pnl_usd"] for t in mine), 4),
                       win_rate=round(sum(t["pnl_usd"] > 0 for t in mine) / len(mine), 4))
        out.append(row)
    return {"mode": "PAPER", "agents": out}


@router.get("/positions")
def positions():
    rows = _led().positions()
    for r in rows:
        v = r["mark_value"] if r["mark_value"] is not None else r["cost_usd"]
        r["unrealized_pnl"] = round(v - r["cost_usd"], 4)
    return {"mode": "PAPER", "positions": rows}


@router.get("/trades")
def trades(limit: int = 100):
    return {"mode": "PAPER", "trades": _led().closed_trades()[:limit]}


@router.get("/fills")
def fills(limit: int = 200):
    return {"mode": "PAPER", "fills": _led().fills(limit)}


@router.get("/decisions")
def decisions(limit: int = 100, kind: str | None = None):
    return {"mode": "PAPER", "decisions": _led().decisions(limit, kind)}


@router.get("/equity")
def equity(limit: int = 2000):
    return {"mode": "PAPER", "history": _led().equity_history(limit)}


@router.post("/start", dependencies=LOCAL)
def start():
    return engine.start()


@router.post("/stop", dependencies=LOCAL)
def stop():
    return engine.stop()


@router.post("/cycle", dependencies=LOCAL)
def run_cycle():
    return engine.get_engine().run_cycle()


class KillSwitch(BaseModel):
    reason: str = "manual"


@router.post("/kill-switch", dependencies=LOCAL)
def trip(req: KillSwitch):
    engine.get_engine().trip_kill_switch(f"manual: {req.reason}")
    return engine.get_engine().kill_switch()


@router.post("/kill-switch/reset", dependencies=LOCAL)
def reset(req: KillSwitch):
    engine.get_engine().reset_kill_switch(req.reason)
    return engine.get_engine().kill_switch()


# ─── simulated funds ───

class CashMove(BaseModel):
    amount_usd: float = Field(gt=0, le=1_000_000)
    note: str = ""


@router.post("/funds/deposit", dependencies=LOCAL)
def deposit(req: CashMove):
    return {"mode": "PAPER", "movement": _led().move_cash("deposit", req.amount_usd, req.note), "cash": _led().cash()}


@router.post("/funds/withdraw", dependencies=LOCAL)
def withdraw(req: CashMove):
    try:
        mv = _led().move_cash("withdraw", req.amount_usd, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"mode": "PAPER", "movement": mv, "cash": _led().cash()}


@router.get("/funds")
def funds():
    return {"mode": "PAPER", "movements": _led().cash_movements(), "contributed": _led().contributed()}


# ─── watch-only wallets (public addresses, no keys) ───

class Wallet(BaseModel):
    address: str
    label: str = ""


@router.get("/wallets")
def wallets():
    out = []
    for w in _led().get_state("watch_wallets", []):
        try:
            out.append({**w, **md.wallet_balances(w["address"])})
        except Exception as e:
            out.append({**w, "error": str(e)})
    return {"mode": "WATCH-ONLY", "wallets": out}


@router.post("/wallets", dependencies=LOCAL)
def add_wallet(req: Wallet):
    if not BASE58.match(req.address):
        raise HTTPException(400, "not a Solana public address (never paste a private key or seed phrase)")
    ws = [w for w in _led().get_state("watch_wallets", []) if w["address"] != req.address]
    _led().set_state("watch_wallets", ws + [{"address": req.address, "label": req.label[:40]}])
    return {"ok": True}


@router.delete("/wallets/{address}", dependencies=LOCAL)
def remove_wallet(address: str):
    _led().set_state("watch_wallets", [w for w in _led().get_state("watch_wallets", []) if w["address"] != address])
    return {"ok": True}


# ─── research desk ───

class ResearchReq(BaseModel):
    mint: str
    symbol: str = ""


@router.get("/research")
def research(limit: int = 30):
    return {"mode": "PAPER", "briefs": _led().research(limit)}


@router.post("/research", dependencies=LOCAL)
def research_now(req: ResearchReq):
    if not BASE58.match(req.mint):
        raise HTTPException(400, "not a Solana mint address")
    eng = engine.get_engine()
    return {"mode": "PAPER", "brief": eng.research.brief(req.mint, req.symbol, force=True)}


# ─── markets (pause per market or per venue) ───

class Pause(BaseModel):
    market: str          # instrument id (mint / hl:<coin>) or a venue name
    paused: bool


@router.get("/markets")
def markets():
    eng = engine.get_engine()
    return {"mode": "PAPER", "markets": eng.ledger.get_state("markets", []), "paused": eng.paused(),
            "venues": eng.executor.venues}


@router.post("/markets/pause", dependencies=LOCAL)
def pause_market(req: Pause):
    engine.get_engine().set_paused(req.market, req.paused)
    return {"paused": engine.get_engine().paused()}


# ─── go-live readiness gate (informational: there is no live execution in this build) ───

@router.get("/readiness")
def readiness():
    led = _led()
    hist = led.equity_history(100_000)
    trades = led.closed_trades()
    days = 0.0
    if hist:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(hist[0]["ts"])).total_seconds() / 86400
    perf = performance(hist).get("all", {}) if hist else {}
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent = [d for d in led.decisions(5000) if d["ts"] >= week_ago]
    trips = [d for d in recent if d["kind"] == "kill_switch" and not d["reason"].startswith("reset")]
    issues = [d for d in recent if d["kind"] in ("error", "reconcile")]
    mdd = _max_drawdown(hist) if len(hist) > 1 else 0.0
    hold = perf.get("hold_sol")
    checks = [
        {"name": "Paper 24/7 há ≥ 28 dias", "ok": days >= 28, "value": f"{days:.1f} dias"},
        {"name": "≥ 30 trades fechados", "ok": len(trades) >= 30, "value": f"{len(trades)} trades"},
        {"name": "Retorno (TWR) > 0", "ok": (perf.get("twr") or 0) > 0, "value": f"{(perf.get('twr') or 0) * 100:.2f}%"},
        {"name": "Bate buy-and-hold SOL", "ok": hold is not None and (perf.get("twr") or 0) > hold,
         "value": "–" if hold is None else f"{(perf.get('twr') or 0) * 100:.2f}% vs {hold * 100:.2f}%"},
        {"name": "Drawdown máximo ≤ 15%", "ok": mdd <= 0.15, "value": f"{mdd * 100:.1f}%"},
        {"name": "Kill switch não disparou em 7 dias", "ok": not trips, "value": f"{len(trips)} disparos"},
        {"name": "Sem erros/reconciliações em 7 dias", "ok": len(issues) <= 5, "value": f"{len(issues)} eventos"},
    ]
    return {"mode": "PAPER", "ready": all(c["ok"] for c in checks), "checks": checks,
            "note": "Mesmo com tudo verde, a execução real (fase 3) ainda não existe neste build: exige "
                    "decisão explícita, hot wallet dedicada com limites e LIVE_TRADING=1."}
