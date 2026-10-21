"""
/api/paper — Solana PAPER trading state. Every response carries mode=PAPER.
Deposits/withdrawals here move SIMULATED money only; wallets are watch-only.
"""
import hmac
import os
import threading
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
EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")

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
        "live": eng.ledger.get_state("live", {}),
        "decision_mode": eng.decision_mode(),
        "llm_available": bool(eng.llm and eng.llm.enabled),
        "scanning": eng.scanning(),
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
    """Start a scan in the background; the UI follows progress through /status (scanning, live)."""
    eng = engine.get_engine()
    if eng.scanning():
        return {"started": False, "reason": "a scan is already running"}
    threading.Thread(target=eng.run_cycle, daemon=True).start()
    return {"started": True}


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
            bal = md.evm_wallet_balances(w["address"]) if EVM.match(w["address"]) else md.wallet_balances(w["address"])
            out.append({**w, **bal})
        except Exception as e:
            out.append({**w, "error": str(e)})
    return {"mode": "WATCH-ONLY", "wallets": out}


@router.post("/wallets", dependencies=LOCAL)
def add_wallet(req: Wallet):
    if not (BASE58.match(req.address) or EVM.match(req.address)):
        raise HTTPException(400, "not a public Solana or EVM (0x) address — never paste a private key or seed phrase")
    if len(req.address) > 44 and not EVM.match(req.address):
        raise HTTPException(400, "that looks like a private key, not an address")
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


# ─── real trading on Hyperliquid (testnet first) ───

def _live_call(fn, *a):
    from src.solana import hl_live
    try:
        return getattr(hl_live, fn)(*a)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Hyperliquid: {e}")


@router.get("/live/status")
def live_status():
    return _live_call("status")


@router.post("/live/arm", dependencies=LOCAL)
def live_arm():
    return _live_call("arm")


@router.post("/live/disarm", dependencies=LOCAL)
def live_disarm():
    return _live_call("disarm", "dashboard")


@router.post("/live/flatten", dependencies=LOCAL)
def live_flatten():
    return _live_call("flatten", "manual 'close everything' from dashboard")


@router.post("/live/test-order", dependencies=LOCAL)
def live_test_order():
    return _live_call("test_order")


@router.post("/live/kill-switch/reset", dependencies=LOCAL)
def live_kill_reset():
    from src.solana import hl_live
    hl_live.get_live_engine().reset_kill_switch("dashboard")
    return hl_live.get_live_engine().kill_switch()


class Mode(BaseModel):
    mode: str                 # rules | llm
    target: str = "paper"     # paper | live


@router.post("/decision-mode", dependencies=LOCAL)
def set_mode(req: Mode):
    if req.target == "live":
        from src.solana import hl_live
        eng = hl_live.get_live_engine()
    else:
        eng = engine.get_engine()
    try:
        eng.set_decision_mode(req.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"target": req.target, "mode": eng.decision_mode()}


# ─── model arena (paper) and the model that runs the real account ───

@router.get("/arena")
def arena_board():
    from src.solana import arena, hl_live
    from src.solana import evolution
    return {"mode": "PAPER", "models": evolution.active_models(), "running": any(r["running"] for r in arena.leaderboard()),
            "leaderboard": arena.leaderboard(), "live_model": "coordinator"}


class Toggle(BaseModel):
    on: bool


@router.post("/arena", dependencies=LOCAL)
def arena_toggle(req: Toggle):
    from src.solana import arena
    try:
        return arena.set_running(req.on)
    except ValueError as e:
        raise HTTPException(400, str(e))


class LiveModel(BaseModel):
    model: str


class Boost(BaseModel):
    bot: str = "claude-haiku-5-5"
    model: str
    cycles: int = 3


class TrainBoost(BaseModel):
    bot: str
    hours: float = 24


@router.post("/arena/train-boost", dependencies=LOCAL)
def arena_train_boost(req: TrainBoost):
    from src.solana import arena
    try:
        return arena.boost_training(req.bot, req.hours)
    except ValueError as e:
        raise HTTPException(400, str(e))


class Turbo(BaseModel):
    bot: str


@router.post("/arena/turbo", dependencies=LOCAL)
def arena_turbo(req: Turbo):
    from src.solana import arena
    try:
        return arena.turbo(req.bot)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/arena/ai-full-scan", dependencies=LOCAL)
def arena_ai_full_scan(req: TrainBoost):
    from src.solana import arena
    try:
        return arena.ai_full_scan(req.bot, req.hours)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/arena/boost", dependencies=LOCAL)
def arena_boost(req: Boost):
    """Give an AI bot a stronger model (Sonnet, Opus, Fable…) for N cycles; it then returns to its default."""
    from src.solana import arena
    try:
        return arena.boost(req.bot, req.model, req.cycles)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/live/model", dependencies=LOCAL)
def live_model(req: LiveModel):
    from src.solana import hl_live
    if req.model not in cfg.ARENA_MODELS:
        raise HTTPException(400, f"model must be one of {cfg.ARENA_MODELS}")
    eng = hl_live.get_live_engine()
    eng.llm.model = req.model
    eng.ledger.set_state("live_model", req.model)
    eng.ledger.log_decision("mode", reason=f"real account decisions by {req.model}")
    return {"live_model": req.model}


# ─── simulation of the real account (fake money, real mainnet prices, same limits as live) ───

@router.get("/shadow")
def shadow():
    from src.solana import hl_live
    return hl_live.shadow_status()


@router.post("/shadow", dependencies=LOCAL)
def shadow_toggle(req: Toggle):
    from src.solana import hl_live
    hl_live.get_shadow_engine().ledger.set_state("running", req.on)
    return {"running": req.on}


@router.post("/shadow/scan", dependencies=LOCAL)
def shadow_scan():
    from src.solana import hl_live
    eng = hl_live.get_shadow_engine()
    if eng.scanning():
        return {"started": False}
    threading.Thread(target=hl_live.shadow_cycle, daemon=True).start()
    return {"started": True}


# ─── one account, two modes: "sim" (fake money) and "real" — identical engine, limits and view ───

def _account_engine(kind: str):
    from src.solana import hl_live
    if kind == "sim":
        return hl_live.get_shadow_engine()
    if kind == "real":
        return hl_live.get_live_engine()
    raise HTTPException(400, "kind must be 'sim' or 'real'")


def profit(eng) -> dict:
    """Net profit = trading P&L (fees and funding already inside) − AI cost − server cost."""
    from src.solana import costs
    led = eng.ledger
    hist = led.equity_history(100_000)
    trade_all = eng.portfolio().equity - led.contributed()
    trade_30 = (performance(hist).get("month") or {}).get("gain_usd", trade_all) if hist else trade_all
    days = ((datetime.now(timezone.utc) - datetime.fromisoformat(hist[0]["ts"])).total_seconds() / 86400) if hist else 0
    b = costs.book()
    ai_30, ai_all = b.total(days=30), b.total()
    vps_30, vps_all = cfg.VPS_MONTHLY_USD * min(days, 30) / 30, cfg.VPS_MONTHLY_USD * days / 30
    net_30, net_all = trade_30 - ai_30 - vps_30, trade_all - ai_all - vps_all
    return {"trading_30d": round(trade_30, 4), "ai_30d": round(ai_30, 4), "server_30d": round(vps_30, 4),
            "net_30d": round(net_30, 4), "trading_all": round(trade_all, 4), "ai_all": round(ai_all, 4),
            "server_all": round(vps_all, 4), "net_all": round(net_all, 4),
            "ai_month": round(b.month_spent(), 4), "ai_budget": round(costs.allowance(), 4), "ai_hard_cap": cfg.LLM_MONTHLY_BUDGET_USD,
            "ai_base": cfg.AI_BASE_MONTHLY_USD, "ai_share": cfg.AI_PROFIT_SHARE, "ai_calls_30d": b.calls(30),
            "prefilter": cfg.LLM_PREFILTER,
            "verdict": "sem dados ainda" if not (ai_all or trade_all) else ("dando lucro" if net_all > 0 else "dando prejuízo")}


def goals(eng) -> dict:
    """Today's goals vs what the bots actually did (local calendar day)."""
    from src.solana import hl_live
    today = (hl_live.daily_report(eng, 1) or [{}])[0]
    start = today.get("equity_start") or eng.portfolio().equity
    target_usd = round(start * cfg.DAILY_GOAL_PCT, 4)
    now = datetime.now(cfg.TZ)
    cycles_target = int((now.hour * 3600 + now.minute * 60 + now.second) // cfg.LIVE_CYCLE_SEC) or 1
    pnl = today.get("pnl_usd", 0.0) or 0.0
    return {"profit": {"target_usd": target_usd, "target_pct": cfg.DAILY_GOAL_PCT, "done_usd": round(pnl, 4)},
            "trades": {"target": cfg.DAILY_GOAL_TRADES, "done": today.get("entries", 0) or 0,
                       "closed": today.get("trades", 0) or 0, "wins": today.get("wins", 0) or 0},
            "analyses": {"target_so_far": cycles_target, "done": today.get("cycles", 0) or 0},
            "loss_limit_usd": round(-start * cfg.LIVE_MAX_DAILY_LOSS_PCT, 2),
            "hit": eng._goal_hit(eng.portfolio()), "rest": cfg.DAILY_GOAL_REST,
            "day_gain_usd": round((p := eng.portfolio()).equity - p.day_start_equity, 4)}   # deposits are not gains


def _market_temp() -> dict:
    """Market data is the same for both accounts (Hyperliquid mainnet): show the freshest scan of either one."""
    temps = [_account_engine(k).ledger.get_state("market_temp") or {} for k in ("sim", "real")]
    return max(temps, key=lambda t: t.get("ts") or "")


@router.get("/account")
def account(kind: str = "sim"):
    from src.solana import hl_live
    eng = _account_engine(kind)
    led = eng.ledger
    p = eng.portfolio()
    trades = led.closed_trades()
    out = {
        "kind": kind, "money": "fake" if kind == "sim" else "real", "network": "mainnet" if kind == "sim" else hl_live.network(),
        "running": led.get_state("running", False) if kind == "sim" else hl_live.is_armed(),
        "scanning": eng.scanning(), "decision_mode": eng.decision_mode(), "model": "equipe" if len(eng.team) > 1 else (eng.llm.model if eng.llm else None),
        "llm_available": bool(cfg.ANTHROPIC_API_KEY),
        "team": [{"name": m.name, "model": m.model, "enabled": m.enabled,
                  "last": next((dict(v, market=k) for k, v in sorted(m.last.items(), key=lambda kv: -(kv[1].get("t") or 0))), None),
                  "trades": len([t for t in trades if t["strategy"] == m.name]),
                  "pnl_usd": round(sum(t["pnl_usd"] for t in trades if t["strategy"] == m.name), 4),
                  "open": [x["symbol"] for x in led.positions() if x["strategy"] == m.name]} for m in eng.team],
        "goals": goals(eng),
        "sleep": (led.get_state("sleep") or {}) if cfg.BOT_SELF_PAUSE else {}, "coffee_until": eng.coffee(), "market_temp": _market_temp(),
        "profit": profit(eng),
        "equity": round(p.equity, 4), "cash": round(p.cash, 4), "exposure": round(p.exposure, 4),
        # what is still free: equity minus the margin the open positions hold (notional / leverage)
        "available": round(p.equity - p.exposure / cfg.LIVE_LEVERAGE, 4),
        "margin_used": round(p.exposure / cfg.LIVE_LEVERAGE, 4),
        "buying_power": round(max(0.0, min(p.equity, cfg.LIVE_MAX_CAPITAL_USD) * cfg.LIVE_LEVERAGE * 0.97 - p.exposure), 4),
        "pnl_usd": round(p.equity - led.contributed(), 4), "contributed": led.contributed(),
        "performance": performance(led.equity_history(100_000)),
        "positions": led.positions(), "trades": trades[:100],
        "win_rate": round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 3) if trades else None,
        "decisions": led.decisions(25), "last_cycle": led.get_state("last_cycle"), "cycle": eng.cycle,
        "heartbeat": led.get_state("heartbeat"), "cycle_interval_sec": cfg.LIVE_CYCLE_SEC,
        "kill_switch": eng.kill_switch(), "live": led.get_state("live", {}), "agents": led.get_state("agents", {}),
        "limits": {"max_capital_usd": cfg.LIVE_MAX_CAPITAL_USD, "max_position_usd": cfg.LIVE_MAX_POSITION_USD,
                   "max_positions": cfg.LIVE_MAX_POSITIONS, "max_daily_loss_pct": cfg.LIVE_MAX_DAILY_LOSS_PCT,
                   "leverage": cfg.LIVE_LEVERAGE,
                   "markets": cfg.HL_MARKETS if kind == "sim" else cfg.HL_LIVE_MARKETS},
    }
    if kind == "real":
        st = hl_live.config_status()
        out["config"] = st
        if not st["problems"]:
            try:
                br = hl_live._broker(eng)
                out["exchange_balance"] = br.account_value()
                out["exchange"] = br.balances()          # collateral / margin used / available, from the exchange
            except Exception as e:
                out["exchange_error"] = str(e)[:200]
    return out


class Kind(BaseModel):
    kind: str = "sim"


def _scan_in_background(kind: str) -> bool:
    from src.solana import hl_live
    eng = _account_engine(kind)
    if eng.scanning():
        return False
    threading.Thread(target=hl_live.shadow_cycle if kind == "sim" else hl_live.live_cycle, daemon=True).start()
    return True


@router.post("/account/start", dependencies=LOCAL)
def account_start(req: Kind):
    """One click: turn the account on and start analysing immediately."""
    from src.solana import hl_live
    from src.solana import arena
    if req.kind == "real":
        _live_call("arm")
    else:
        hl_live.get_shadow_engine().ledger.set_state("running", True)
    team = arena.set_running(True)     # the lab bots are the team's brains for both wallets
    threading.Thread(target=arena.run_round, daemon=True).start()
    return {"kind": req.kind, "running": True, "scan_started": _scan_in_background(req.kind),
            "llm_bots_need_key": team["llm_bots_need_key"]}


@router.post("/account/stop", dependencies=LOCAL)
def account_stop(req: Kind):
    from src.solana import hl_live
    from src.solana import arena
    if req.kind == "real":
        hl_live.disarm("dashboard stop")
    else:
        hl_live.get_shadow_engine().ledger.set_state("running", False)
        if not hl_live.is_armed():
            arena.set_running(False)
    return {"kind": req.kind, "running": False}


@router.post("/account/scan", dependencies=LOCAL)
def account_scan(req: Kind):
    from src.solana import hl_live
    running = hl_live.is_armed() if req.kind == "real" else hl_live.get_shadow_engine().ledger.get_state("running", False)
    if not running:
        raise HTTPException(400, "clique em Start primeiro")
    # a manual scan wakes the wallet and the whole team (self-pause is only for unattended running)
    from src.solana import arena
    for eng in [_account_engine(req.kind), *arena.bots().values()]:
        eng.ledger.set_state("sleep", {})
        eng.ledger.set_state("idle_streak", 0)          # back-off restarts short after a manual look
    threading.Thread(target=arena.run_round, daemon=True).start()
    return {"started": _scan_in_background(req.kind)}


class Coffee(BaseModel):
    kind: str = "sim"
    hours: float = 4


@router.post("/account/coffee", dependencies=LOCAL)
def account_coffee(req: Coffee):
    """☕ Keep both wallets (simulation and real) and the whole team awake (no self-pause) for a few hours and scan now.
    Entry rules stay the same: looser dips lost money in the backtest, so coffee buys attention, not risk."""
    from src.solana import arena, hl_live
    hours = max(0.5, min(req.hours, 12))
    # one cup for the whole house: simulation and real share the team, so both stay awake together
    titulares = [e for m, e in arena.bots().items() if arena.is_titular(m)]   # reserves: 1 cup a day on their own card
    for eng in [_account_engine("sim"), _account_engine("real"), *titulares]:
        until = eng.give_coffee(hours)
    threading.Thread(target=arena.run_round, daemon=True).start()
    running = {"sim": _account_engine("sim").ledger.get_state("running", False), "real": hl_live.is_armed()}
    return {"coffee_until": until, "hours": hours, "accounts": ["sim", "real"],
            "scan_started": {k: _scan_in_background(k) for k, on in running.items() if on}}


class BotCoffee(BaseModel):
    bot: str
    hours: float = 4


@router.post("/arena/coffee", dependencies=LOCAL)
def arena_coffee(req: BotCoffee):
    from src.solana import arena
    try:
        return arena.coffee(req.bot, req.hours)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/account/kill-switch/reset", dependencies=LOCAL)
def account_kill_reset(req: Kind):
    """Re-arm after a kill switch (the user's explicit click on the dashboard banner)."""
    eng = _account_engine(req.kind)
    eng.reset_kill_switch("dashboard")
    eng.ledger.set_state("sync_failures", 0)
    return eng.kill_switch()


class ManualBuy(BaseModel):
    kind: str = "sim"
    coin: str
    usd: float = 10.0


@router.post("/account/manual-buy", dependencies=LOCAL)
def account_manual_buy(req: ManualBuy):
    """The user's own buy from the dashboard (never called by the bots): stop/target on the exchange, same limits."""
    from src.solana import hl_live
    if req.kind == "real" and not hl_live.is_armed():
        raise HTTPException(400, "a conta real está desligada — clique em Start primeiro")
    if not 10 <= req.usd <= cfg.LIVE_MAX_POSITION_USD:
        raise HTTPException(400, f"valor entre $10 (mínimo da Hyperliquid) e ${cfg.LIVE_MAX_POSITION_USD:.0f}")
    try:
        return _account_engine(req.kind).manual_entry(req.coin, req.usd)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ClosePos(BaseModel):
    kind: str = "sim"
    mint: str


@router.post("/account/close", dependencies=LOCAL)
def account_close(req: ClosePos):
    """The user's own sell of one open position (market order), from the dashboard."""
    from src.solana import hl_live
    if req.kind == "real" and not hl_live.is_armed():
        raise HTTPException(400, "a conta real está desligada — clique em Start primeiro")
    try:
        return _account_engine(req.kind).manual_exit(req.mint)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/account/position")
def account_position(kind: str, mint: str):
    """Everything about one open position: when, how much, how it is doing, why it was opened."""
    eng = _account_engine(kind)
    pos = next((p for p in eng.ledger.positions() if p["mint"] == mint), None)
    if pos is None:
        raise HTTPException(404, "posição não encontrada")
    entry = next((d for d in eng.ledger.decisions(500, kind="entry") if d.get("order_id") == pos["entry_order_id"]
                  or (d["mint"] == mint and d["ts"] >= pos["opened_at"][:19])), None)
    value = pos.get("mark_value") or pos["cost_usd"]
    return {**pos, "value": value, "pnl_usd": round(value - pos["cost_usd"], 4), "pnl_pct": round(value / pos["cost_usd"] - 1, 5),
            "held_h": round((datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600, 2),
            "entry_reason": entry["reason"] if entry else None, "funding_usd": round(eng.ledger.funding_paid(pos["entry_order_id"]), 5)}


class AccountMode(BaseModel):
    mode: str


@router.post("/account/mode", dependencies=LOCAL)
def account_mode(req: AccountMode):
    """Simulation and real always share the decision mode (that is what makes them comparable)."""
    from src.solana import hl_live
    try:
        hl_live.get_live_engine().set_decision_mode(req.mode)
        hl_live.get_shadow_engine().set_decision_mode(req.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"mode": req.mode}


@router.get("/logs")
def logs(after: int = 0, limit: int = 400):
    """Live log of every bot (agents, decisions, cycles, warnings), newest last; poll with after=<last seq>."""
    from src.solana import logbuf
    rows = logbuf.since(after, min(limit, 1000))
    return {"lines": rows, "last": rows[-1]["seq"] if rows else after}


def trade_stats(trades: list[dict]) -> dict:
    """History analysis of closed round trips (P&L already net of fees and funding)."""
    from datetime import datetime as dt
    if not trades:
        return {"trades": 0}
    wins, losses = [t for t in trades if t["pnl_usd"] > 0], [t for t in trades if t["pnl_usd"] <= 0]
    gross_win, gross_loss = sum(t["pnl_usd"] for t in wins), -sum(t["pnl_usd"] for t in losses)
    hold = [(dt.fromisoformat(t["exit_ts"]) - dt.fromisoformat(t["entry_ts"])).total_seconds() / 3600 for t in trades]

    def group(key):
        g = {}
        for t in trades:
            k = key(t)
            r = g.setdefault(k, {"trades": 0, "wins": 0, "pnl_usd": 0.0})
            r["trades"] += 1
            r["wins"] += t["pnl_usd"] > 0
            r["pnl_usd"] = round(r["pnl_usd"] + t["pnl_usd"], 4)
        return dict(sorted(g.items(), key=lambda kv: -kv[1]["pnl_usd"]))
    return {"trades": len(trades), "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(trades), 3),
            "pnl_usd": round(sum(t["pnl_usd"] for t in trades), 4), "avg_win": round(gross_win / len(wins), 4) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy": round(sum(t["pnl_usd"] for t in trades) / len(trades), 4),
            "fees_usd": round(sum(t["fees_usd"] + (t["funding_usd"] or 0) for t in trades), 4),
            "avg_hold_h": round(sum(hold) / len(hold), 1),
            "best": max(trades, key=lambda t: t["pnl_usd"]), "worst": min(trades, key=lambda t: t["pnl_usd"]),
            "by_bot": group(lambda t: t["strategy"]), "by_coin": group(lambda t: t["symbol"]),
            "by_exit": group(lambda t: "alvo" if "take-profit" in (t["reason"] or "") else "stop" if "stop" in (t["reason"] or "")
                             else "tempo" if "holding time" in (t["reason"] or "") else "IA/outro")}


def confidence(eng) -> dict:
    """Is the system proven enough to add money? Objective checks over the real record (the user's rule: a few days
    in a row of wins, bots behaving, AI cost under control, positive trades)."""
    from src.solana import hl_live
    days = hl_live.daily_report(eng, 14)                         # newest first, net = trades − AI that day
    streak = 0
    for d in days:
        if d["trades"] == 0 and d["net_usd"] == 0:
            continue                                              # a quiet day neither breaks nor builds the streak
        if d["net_usd"] <= 0:
            break
        streak += 1
    st = trade_stats(eng.ledger.closed_trades())
    P = profit(eng)
    trading, ai = P["trading_all"], P["ai_all"]
    kills = [d for d in eng.ledger.decisions(500, kind="kill_switch")
             if d["ts"] >= (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() and "reset" not in d["reason"]]
    checks = [
        {"name": "3 dias seguidos com lucro líquido", "ok": streak >= 3, "value": f"{streak} dia(s)"},
        {"name": "10+ trades fechados na conta real", "ok": st.get("trades", 0) >= 10, "value": f"{st.get('trades', 0)} trades"},
        {"name": "Acerto ≥ 50%", "ok": (st.get("win_rate") or 0) >= 0.5, "value": f"{(st.get('win_rate') or 0) * 100:.0f}%"},
        {"name": "Fator de lucro ≥ 1,3", "ok": (st.get("profit_factor") or 0) >= 1.3, "value": str(st.get("profit_factor") or "–")},
        {"name": "IA custa < 25% do lucro dos trades", "ok": trading > 0 and ai < 0.25 * trading, "value": f"IA ${ai:.2f} / trades ${trading:.2f}"},
        {"name": "Lucro líquido total positivo", "ok": P["net_all"] > 0, "value": f"${P['net_all']:+.2f}"},
        {"name": "Sem kill switch nos últimos 3 dias", "ok": not kills and not eng.kill_switch().get("active"), "value": f"{len(kills)} vez(es)"},
    ]
    passed = sum(c["ok"] for c in checks)
    return {"checks": checks, "passed": passed, "total": len(checks), "ready": passed == len(checks)}


@router.get("/admin")
def admin():
    """Owner's control room: money, AI spend, profit per trade per account and per bot, and where scaling would pay.
    Read-only; every number comes from the ledgers and the append-only cost book."""
    from src.solana import arena, costs, evolution, hl_live
    book = costs.book()
    now = datetime.now(timezone.utc)

    def account(kind: str) -> dict:
        eng = _account_engine(kind)
        p, led = eng.portfolio(), eng.ledger
        st = trade_stats(led.closed_trades())
        st.pop("best", None), st.pop("worst", None)
        week = now - timedelta(days=7)
        entries_7d = sum(1 for f in led.fills(2000) if f["side"] == "buy" and f["ts"] >= week.isoformat())
        return {"equity": round(p.equity, 4), "contributed": round(led.contributed(), 4),
                "profit": round(p.equity - led.contributed(), 4), "exposure": round(p.exposure, 4),
                "positions": len(p.open_mints), "stats": st, "entries_per_day_7d": round(entries_7d / 7, 2)}

    real, sim = account("real"), account("sim")
    lab = []
    for model, eng in arena.bots().items():
        trades = eng.ledger.closed_trades()
        st = trade_stats(trades)
        st.pop("best", None), st.pop("worst", None)
        pnl, ai, net = arena.net_of_cost(model)
        first = min((t["entry_ts"] for t in trades), default=None)
        days = max((now - datetime.fromisoformat(first)).total_seconds() / 86400, 1) if first else None   # no extrapolating hours
        ret = [t["pnl_usd"] / t["cost_usd"] for t in trades if t.get("cost_usd")]
        lab.append({"model": model, "label": (evolution.load()["bots"].get(model) or {}).get("label"), **st, "lab_pnl": round(pnl, 4), "ai_usd": round(ai, 4), "net": round(net, 4),
                    "trades_per_day": round(len(trades) / days, 1) if days else 0,
                    "ret_per_trade": round(sum(ret) / len(ret), 5) if ret else None,
                    "green_light": arena.green_light(model)})
    ai = {"total": round(book.total(), 4), "d30": round(book.total(30), 4), "d7": round(book.total(7), 4),
          "d1": round(book.total(1), 4), "month": round(book.month_spent(), 4), "allowance": round(costs.allowance(), 2),
          "calls_30d": book.calls(30), "by_model": book.breakdown("model"), "by_who": book.breakdown("who"),
          "by_day": book.breakdown("day", 14)}

    # scaling: per-trade return (real once it has 10+ trades, else every lab trade pooled) x size x trades/day
    real_trades = hl_live.get_live_engine().ledger.closed_trades()
    pool = real_trades if len(real_trades) >= 10 else [t for m in arena.bots().values() for t in m.ledger.closed_trades()]
    rets = [t["pnl_usd"] / t["cost_usd"] for t in pool if t.get("cost_usd")]
    r = sum(rets) / len(rets) if rets else 0.0
    tpd = max(real["entries_per_day_7d"], 1.0)
    ai_month = min(ai["d7"] / 7 * 30, ai["allowance"])     # the monthly allowance is a hard cap on AI spend
    scen = []
    for cap in (cfg.LIVE_MAX_CAPITAL_USD, 100, 250, 500):
        size = cfg.LIVE_MAX_POSITION_USD * cap / cfg.LIVE_MAX_CAPITAL_USD
        month = r * size * tpd * 30
        scen.append({"capital": cap, "position_usd": round(size, 2), "month_trading": round(month, 2),
                     "month_net": round(month - ai_month, 2)})
    conf = confidence(hl_live.get_live_engine())
    util = real["exposure"] / cfg.LIVE_MAX_CAPITAL_USD if cfg.LIVE_MAX_CAPITAL_USD else 0
    advice = []
    if len(real_trades) < 20:
        advice.append(f"Ainda cedo para pôr mais dinheiro: a conta real fechou {len(real_trades)} trades; espere 20+ e o checklist 'Pronto para recarregar' todo ✅.")
    if util >= 0.8:
        advice.append(f"O capital é o gargalo: {util:.0%} do limite de ${cfg.LIVE_MAX_CAPITAL_USD:.0f} está em posições — quando o checklist passar, subir o limite rende mais trades.")
    if ai["d30"] and real["stats"].get("pnl_usd", 0) < ai["d30"]:
        advice.append(f"A IA custou ${ai['d30']:.2f} em 30 dias, mais do que os trades reais renderam — mantenha os bots de regras como titulares.")
    ready = [b["model"] for b in lab if b["green_light"].get("ok") and not b["green_light"].get("trusted")]
    if ready:
        advice.append(f"Prontos para a conta real: {', '.join(ready)} — mais titulares = mais trades com o mesmo capital.")
    near = sorted((b for b in lab if not b["green_light"].get("ok") and b.get("trades")), key=lambda b: -(b.get("expectancy") or -9))[:2]
    if near:
        advice.append("Mais perto da luz verde: " + ", ".join(f"{b['model']} ({b['trades']} trades, acerto {b.get('win_rate', 0):.0%})" for b in near) + " — turbo neles.")
    if r <= 0 and rets:
        advice.append("O retorno médio por trade ainda é negativo: escalar agora só aumentaria a perda.")
    return {"ts": now.isoformat(timespec="seconds"), "real": real, "sim": sim, "lab": lab, "ai": ai,
            "limits": {"capital": cfg.LIVE_MAX_CAPITAL_USD, "position": cfg.LIVE_MAX_POSITION_USD,
                       "positions": cfg.LIVE_MAX_POSITIONS, "leverage": cfg.LIVE_LEVERAGE},
            "scale": {"ret_per_trade": round(r, 5), "source": "conta real" if pool is real_trades else "laboratório",
                      "sample": len(rets), "trades_per_day": tpd, "ai_month": round(ai_month, 2), "utilization": round(util, 3),
                      "scenarios": scen, "confidence": conf, "advice": advice}}


def _mood(trades: list[dict], open_pnl: float) -> dict:
    """How the bot 'feels' — its recent record in one face: last 10 trades, streak and what is open now."""
    last = trades[-10:]
    if not last and not open_pnl:
        return {"emoji": "😴", "label": "esperando", "why": "ainda sem trades fechados"}
    wr = sum(t["pnl_usd"] > 0 for t in last) / len(last) if last else 0.5
    streak = 0
    for t in reversed(trades):
        if (t["pnl_usd"] > 0) != (trades[-1]["pnl_usd"] > 0):
            break
        streak += 1 if t["pnl_usd"] > 0 else -1
    recent = sum(t["pnl_usd"] for t in last) + open_pnl
    if wr >= 0.65 and recent > 0:
        face = ("🤩", "em chamas") if streak >= 3 else ("😄", "confiante")
    elif recent >= 0:
        face = ("🙂", "tranquilo")
    elif wr >= 0.4:
        face = ("😐", "oscilando")
    else:
        face = ("😟", "preocupado") if streak > -3 else ("😣", "em má fase")
    return {"emoji": face[0], "label": face[1], "streak": streak,
            "why": f"{sum(t['pnl_usd'] > 0 for t in last)}/{len(last)} ganhos nos últimos trades · resultado recente ${recent:+.2f}"}


def _opportunities(model: str, watch: list[dict], held: set[str]) -> list[dict]:
    """Coins this bot is watching and how close each is to its buy rule: status 'pronto' (the rule is met; the buy
    still goes through the reviewer and Risk), 'perto' or 'longe', with what is missing in plain words."""
    from src.solana import hl_live, toptraders, whales
    base = model.removesuffix("_15m").rstrip("2")
    out = []
    try:                                     # live price (the radar's is the last closed candle's close)
        live = hl_live.live_snapshot()["prices"]
    except Exception:
        live = {}
    watch = [{**w, "price": live.get(w["symbol"], w["price"])} for w in watch]

    def add(w, progress: float, missing: str, ready: str):
        status = "pronto" if progress >= 1 else "perto" if progress >= 0.6 else "longe"
        out.append({"symbol": w["symbol"], "price": w["price"], "progress": round(min(progress, 1), 3), "status": status,
                    "detail": ready if status == "pronto" else missing, "held": w["symbol"] in held})
    for w in watch:
        if base == "rules" and w.get("up") and w.get("dip_at"):
            drop = (w["price"] - w["dip_at"]) / w["price"]          # how much it still has to fall
            if drop <= 0 and (w.get("rsi") or 100) >= 30:            # in the zone, but not oversold yet
                add(w, 0.95, f"já na zona de compra; falta o RSI cair abaixo de 30 (agora {w.get('rsi', 0):.0f})", "")
            else:
                add(w, 1 - drop / 0.05, f"precisa cair {drop:.1%} até {w['dip_at']:.6g} (e RSI abaixo de 30)",
                    f"caiu até a zona de compra ({w['dip_at']:.6g}) com RSI abaixo de 30")
        elif base in ("trend", "trend4h") and w.get("strong") and w.get("break_at"):
            rise = (w["break_at"] - w["price"]) / w["price"]
            add(w, 1 - rise / 0.03, f"precisa subir {rise:.1%} e romper {w['break_at']:.6g}", f"rompeu {w['break_at']:.6g}")
        elif base == "whales" and w.get("up"):
            sm = whales.summary(w["symbol"])
            net, need = (sm.get("1h") or {}).get("net_usd", 0), 2 * sm.get("threshold_usd", 0)
            if net > 0 and need:
                add(w, net / need, f"baleias compraram ${net:,.0f} na última hora; precisa ${need:,.0f}",
                    f"baleias compraram ${net:,.0f} na última hora (mínimo ${need:,.0f})")
        elif base == "copy" and w.get("up"):
            c = toptraders.consensus(w["symbol"])
            if c.get("long", 0) >= 1:
                ok = c["long"] >= 2 and c.get("new_longs_1h", 0) >= 1
                add(w, 1 if ok else min(c["long"], 2) / 2 * 0.9,
                    f"{c['long']} dos top 10 comprados, {c.get('new_longs_1h', 0)} entraram na última hora (precisa 2 + 1 entrando)",
                    f"{c['long']} dos top 10 comprados e {c.get('new_longs_1h', 0)} entrando agora")
    return sorted(out, key=lambda o: -o["progress"])[:8]


@router.get("/arena/bot/{model}")
def arena_bot(model: str):
    """Everything about one bot for its card's modal: record (lab and real), trades, daily results, mood,
    coins it is closest to buying and a 24h/48h projection from its own numbers."""
    from statistics import pstdev
    from src.solana import arena, hl_live, learning
    eng = arena.bots().get(model)
    if not eng:
        raise HTTPException(404, "bot não existe")
    lb = next((r for r in arena.leaderboard() if r["model"] == model), {})
    lab = eng.ledger.closed_trades()
    real = [t for t in hl_live.get_live_engine().ledger.closed_trades() if t["strategy"] == model]
    pos_lab = [{"symbol": p["symbol"], "opened_at": p["opened_at"], "cost": round(p["cost_usd"], 2),
                "now": round(p["mark_value"] or p["cost_usd"], 2), "pnl": round((p["mark_value"] or p["cost_usd"]) - p["cost_usd"], 4),
                "stop": p["stop_price"], "tp": p["tp_price"]} for p in eng.ledger.positions()]
    pos_real = [{"symbol": p["symbol"], "opened_at": p["opened_at"], "cost": round(p["cost_usd"], 2),
                 "now": round(p["mark_value"] or p["cost_usd"], 2), "pnl": round((p["mark_value"] or p["cost_usd"]) - p["cost_usd"], 4)}
                for p in hl_live.get_live_engine().ledger.positions() if p["strategy"] == model]
    days: dict[str, dict] = {}
    for src, rows in (("lab", lab), ("real", real)):
        for t in rows:
            d = days.setdefault(datetime.fromisoformat(t["exit_ts"]).astimezone(cfg.TZ).date().isoformat(),
                                {"lab": 0.0, "real": 0.0, "trades": 0})
            d[src] = round(d[src] + t["pnl_usd"], 4)
            d["trades"] += 1
    st = trade_stats(lab)
    st.pop("best", None), st.pop("worst", None)
    first = min((t["entry_ts"] for t in lab), default=None)
    active_days = max((datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 86400, 1) if first else 1
    tpd = len(lab) / active_days
    pnls = [t["pnl_usd"] for t in lab]
    exp = sum(pnls) / len(pnls) if pnls else 0.0
    sd = pstdev(pnls) if len(pnls) > 1 else 0.0
    proj = {}
    for h in (24, 48):
        n = tpd * h / 24
        proj[f"{h}h"] = {"trades": round(n, 1), "expected": round(n * exp, 2),
                         "low": round(n * exp - sd * n ** 0.5, 2), "high": round(n * exp + sd * n ** 0.5, 2)}
    open_pnl = sum(p["pnl"] for p in pos_lab)
    trades = [{"where": "real" if t in real else "lab", "symbol": t["symbol"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
               "cost": round(t["cost_usd"], 2), "pnl": round(t["pnl_usd"], 4), "pct": round(t["pnl_pct"] * 100, 2),
               "reason": (t.get("reason") or "")[:80]} for t in sorted(lab + real, key=lambda t: t["exit_ts"], reverse=True)[:60]]
    return {"model": model, "titular": arena.is_titular(model), "board": lb, "stats": st,
            "open_pnl": round(open_pnl, 4), "open_pnl_real": round(sum(p["pnl"] for p in pos_real), 4),
            "real_stats": {k: v for k, v in trade_stats(real).items() if k not in ("best", "worst")},
            "positions_lab": pos_lab, "positions_real": pos_real, "trades": trades,
            "days": [{"day": k, **v} for k, v in sorted(days.items(), reverse=True)][:14],
            "mood": _mood(lab, open_pnl), "learned": learning.summary(learning.record(eng.ledger, model)),
            "opportunities": _opportunities(model, _market_temp().get("watch") or [],
                                            {p["symbol"] for p in pos_lab} | {p["symbol"] for p in pos_real}),
            "projection": {**proj, "trades_per_day": round(tpd, 1), "expectancy": round(exp, 4), "sample": len(pnls),
                           "real_size": arena.is_titular(model)}}


@router.get("/admin/live")
def admin_live():
    """Real account as Hyperliquid sees it right now (balance, positions, fills, prices), refreshed every ~10s."""
    from src.solana import hl_live
    try:
        return hl_live.live_snapshot()
    except Exception as e:
        raise HTTPException(503, f"Hyperliquid indisponível: {str(e)[:120]}")


@router.get("/account/confidence")
def account_confidence(kind: str = "real"):
    return confidence(_account_engine(kind))


@router.get("/account/stats")
def account_stats(kind: str = "sim"):
    return trade_stats(_account_engine(kind).ledger.closed_trades())


@router.get("/account/transactions")
def account_transactions(kind: str = "sim", limit: int = 100):
    """Every money movement of the account, newest first: buys, sells, deposits, withdrawals."""
    led = _account_engine(kind).ledger
    rows = [{"ts": f["ts"], "type": "compra" if f["side"] == "buy" else "venda", "symbol": f["symbol"], "bot": f["strategy"],
             "usd": round(f["usd_gross"], 4), "price": f["price"], "qty": f["qty"], "fee_usd": round(f["fee_usd"] or 0, 5)}
            for f in led.fills(limit)]
    rows += [{"ts": m["ts"], "type": "depósito" if m["kind"] == "deposit" else "saque", "symbol": "USDC", "bot": None,
              "usd": m["amount_usd"], "price": None, "qty": None, "fee_usd": 0, "note": m["note"]} for m in led.cash_movements(limit)]
    return {"rows": sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]}


@router.get("/funding")
def funding_info():
    """Where to send money: the account's own address (public), its Arbitrum wallet balances and the safe steps."""
    from src.solana import market_data as md
    addr = cfg.HL_ACCOUNT_ADDRESS
    out = {"address": addr, "network": "Arbitrum One", "token": "USDC", "min_deposit_usd": 5,
           "hyperliquid_app": "https://app.hyperliquid.xyz/portfolio"}
    try:
        w = md.evm_wallet_balances(addr)
        out.update(arbitrum_usdc=w.get("arbitrum_usdc"), arbitrum_eth=w.get("arbitrum_eth"))
        from src.solana import hl_live         # unified account: the real total lives in the spot clearinghouse
        out["hyperliquid"] = hl_live._broker(hl_live.get_live_engine()).account_value()
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


@router.get("/news")
def news_feed(coin: str | None = None, hours: float = 24):
    """Crypto news (CoinDesk, Cointelegraph, Decrypt, The Block) tagged with the coins they mention."""
    from src.solana import news
    return {"status": news.status(), "items": news.latest(coin, min(hours, 72), 60)}


@router.get("/training")
def training():
    """Training center: how many virtual trades were recorded/resolved and what they teach, per situation."""
    from src.solana import trainer
    return {**trainer.stats(), "lessons": trainer.lessons()}


@router.get("/news/trending")
def news_trending(hours: float = 12):
    from src.solana import news
    return news.trending(min(hours, 48))


@router.get("/candles")
def candles(coin: str):
    """1h candles of a market the bots watch (shared hourly cache — no extra exchange load)."""
    from src.solana import hyperliquid as hl
    if coin not in cfg.HL_MARKETS and coin not in cfg.HL_LIVE_MARKETS:
        raise HTTPException(400, "unknown market")
    return {"coin": coin, "candles": hl.candles(coin)[-240:]}


@router.get("/account/equity")
def account_equity(kind: str = "sim", points: int = 400):
    """Equity over time and BTC price, for the 'us vs holding BTC' chart (thinned to `points`)."""
    rows = _account_engine(kind).ledger.equity_history(100_000)
    step = max(1, len(rows) // max(points, 1))
    return {"rows": [{"ts": r["ts"], "equity": r["equity"], "contributed": r["contributed"], "btc": r["btc_price"]}
                     for r in rows[::step]]}


@router.get("/toptraders")
def top_traders():
    """The top 10 Hyperliquid traders, their positions, recent moves and their consensus on our coins."""
    from src.solana import toptraders
    return toptraders.board(cfg.HL_LIVE_MARKETS)


@router.get("/whales")
def whales_board():
    """Whale flow per coin (last 1h / 4h) and the latest whale trades, from the live Hyperliquid trades feed."""
    from src.solana import whales
    ours = cfg.HL_LIVE_MARKETS
    market = [(c, whales.summary(c)) for c in whales.watching() if c not in ours]
    market = sorted([m for m in market if m[1]["1h"]["n"]], key=lambda m: -(m[1]["1h"]["buy_usd"] + m[1]["1h"]["sell_usd"]))
    return {"status": {**whales.status(), "watching": len(whales.watching())},
            "coins": {c: whales.summary(c) for c in ours},                 # the coins our bots trade
            "market": [{"coin": c, **s} for c, s in market[:12]],          # whales elsewhere in crypto (not traded yet)
            "recent": whales.recent(20), "wallets": whales.top_wallets(8)}


@router.get("/health")
def health():
    """Is every third party (exchange, data, AI, RPCs) reachable? Cached 60s, read-only calls."""
    from src.solana import health as h
    rows = h.check_all()
    return {"ok": all(r["ok"] is not False for r in rows), "checks": rows}


@router.get("/report/daily")
def report_daily(kind: str = "real", days: int = 14):
    from src.solana import hl_live
    return {"kind": kind, "days": hl_live.daily_report(_account_engine(kind), min(days, 90))}
