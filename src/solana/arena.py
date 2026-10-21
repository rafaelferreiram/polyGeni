"""
The lab: one paper bot per model (plus the deterministic 'rules' baseline), each with its own $20 wallet and
the real account's exact limits, trading the same Hyperliquid markets. Their results per market are what the
Coordinator uses to decide which bot's proposal executes on the shared (real) wallet.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg
from src.solana import hyperliquid as hl
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, Vote
from src.solana.engine import PaperEngine, bench_prices
from src.solana.ledger import Ledger, now_iso
from src.solana.llm_trader import LLMDecider
from src.solana.strategies import RULE_BOTS

logger = logging.getLogger("solana.arena")


class _PerpsSafety:
    min_age_h = 0

    def check(self, c):
        return Vote("safety", True, "n/a for perps")


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def build_bot(model: str, db_dir: str = ".", llm_fn=None, scout=None, broker_factory=None, candles_fn=None) -> PaperEngine:
    from src.solana.hl_live import _risk          # identical limits to the real account
    ledger = Ledger(f"{db_dir}/arena_{_slug(model)}.db", cfg.ARENA_START_USDC)
    decider = None
    if model not in RULE_BOTS:
        decider = LLMDecider(model=model, llm_fn=llm_fn)
        decider.name = f"llm:{model}"
        decider.record_fn = lambda: track_record(model)        # the AI sees its own reward and recent trades
    eng = PaperEngine(
        ledger, [scout or hl.HyperliquidScout()], _PerpsSafety(), [RULE_BOTS[model]()] if model in RULE_BOTS else [],
        DebateAgent(enabled=False), _risk(),
        ExecutorAgent(broker_factory(ledger) if broker_factory else hl.HyperliquidPaperBroker(ledger, leverage=cfg.LIVE_LEVERAGE)),
        candles_fn=candles_fn or {hl.VENUE: hl.candles}, bench_fn=bench_prices if candles_fn is None else None,
        llm=decider, profiles={hl.VENUE: {"max_pos_pct": 1.0, "stop_mult": cfg.MARKET_PROFILES.get(hl.VENUE, {}).get("stop_mult", 1.5)}},
    )
    ledger.set_state("decision_mode", "rules" if model in RULE_BOTS else "llm")
    eng.rests_on_goal = False                 # the lab keeps measuring all day
    ledger.set_state("model", model)
    return eng


def lab_decider(model: str):
    """The lab bot's LLMDecider, so the shared wallet can reuse its decision for the same candle."""
    eng = (_bots or {}).get(model)
    return eng.llm if eng else None


def score(model: str, market: str | None = None, days: float | None = None) -> tuple[float, int]:
    """Shrunk expectancy per trade (USD) of a lab bot, on one market or overall: sum(pnl) / (n + 3).
    With `days`, only recent trades count — that is how the Coordinator follows the market regime."""
    eng = (_bots or {}).get(model)
    if not eng:
        return 0.0, 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    trades = [t for t in eng.ledger.closed_trades() if (market is None or t["symbol"] == market) and t["exit_ts"] >= since]
    return sum(t["pnl_usd"] for t in trades) / (len(trades) + 3), len(trades)


def reward(model: str) -> dict:
    """The bots' reward: points = net profit of the last LAB_SCORE_DAYS in the lab, after its AI cost (1 point =
    1 cent). It is not cosmetic — it decides (1) the Coordinator's priority, (2) position size: a bot with a losing
    recent record trades the minimum ($10) until it recovers, and (3) the AI bot sees its own record before deciding."""
    from src.solana import costs
    d = cfg.LAB_SCORE_DAYS
    eng = (_bots or {}).get(model)
    since = (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
    trades = [t for t in eng.ledger.closed_trades() if t["exit_ts"] >= since] if eng else []
    pnl = sum(t["pnl_usd"] for t in trades)
    ai = 0.0 if model in RULE_BOTS else _ai_cost(model, d)
    exp, n = score(model, None, d)
    streak = 0
    for t in trades:                                  # newest first
        if t["pnl_usd"] <= 0:
            break
        streak += 1
    proven, losing = n >= 3 and exp > 0, n >= 3 and exp < 0
    return {"points": round((pnl - ai) * 100), "trades": n, "wins": sum(t["pnl_usd"] > 0 for t in trades),
            "pnl_usd": round(pnl, 4), "ai_usd": round(ai, 4), "expectancy": round(exp, 4), "streak": streak,
            "tier": "positivo" if proven else "em observação" if losing else "em avaliação",
            "size_usd": hl_min_order() if losing else cfg.LIVE_MAX_POSITION_USD}


def hl_min_order() -> float:
    from src.solana.hl_live import MIN_ORDER_USD
    return MIN_ORDER_USD


def size_for(strategy: str) -> float | None:
    """Position size reward/penalty for the bot behind a strategy name ('rules', 'trend', 'llm:<model>')."""
    model = strategy[4:] if strategy.startswith("llm:") else strategy
    return reward(model)["size_usd"] if model in (_bots or {}) else None


def track_record(model: str, n: int = 8) -> dict:
    """What the AI bot is shown about itself before each decision (its incentive and its lessons)."""
    eng = (_bots or {}).get(model)
    r = reward(model)
    last = [{"symbol": t["symbol"], "pnl_pct": round(t["pnl_pct"] * 100, 2), "why_exit": str(t.get("reason", ""))[:60]}
            for t in (eng.ledger.closed_trades()[:n] if eng else [])]
    return {"reward_points": r["points"], "tier": r["tier"], "win_streak": r["streak"], "recent_trades": last,
            "rule": "points = net profit after AI cost; higher points = priority and full position size; "
                    "losing record = minimum size until it recovers; paid analyses cost points"}


def eligible_for_real(members: list) -> list:
    """Real money only for bots with evidence: REAL_TRUSTED_BOTS (backtest-validated rule bots), and every other bot
    (AI) once it has ≥ LIVE_MIN_LAB_TRADES lab trades and positive expectancy after its AI cost."""
    ok = []
    for m in members:
        if m.model in cfg.REAL_TRUSTED_BOTS:
            ok.append(m)
            continue
        exp, n = score(m.model)
        if n >= cfg.LIVE_MIN_LAB_TRADES and exp > 0 and net_of_cost(m.model)[2] > 0:   # profitable AFTER its AI cost
            ok.append(m)
    return ok


def rank(market: str, members: list) -> list:
    """Coordinator: best RECENT lab record (LAB_SCORE_DAYS) on this market first, then recent overall, then the
    configured order. Mean reversion and trend following win in different regimes; recent results tell which."""
    order = {m: i for i, m in enumerate(cfg.ARENA_MODELS)}
    d = cfg.LAB_SCORE_DAYS
    return sorted(members, key=lambda m: (-score(m.model, market, d)[0], -reward(m.model)["points"], order.get(m.model, 99)))


_bots: dict[str, PaperEngine] | None = None


def bots() -> dict[str, PaperEngine]:
    global _bots
    if _bots is None:
        _bots = {m: build_bot(m) for m in cfg.ARENA_MODELS}
    return _bots


def net_of_cost(model: str) -> tuple[float, float, float]:
    """(trading P&L in the lab, AI cost so far, net) for one bot."""
    from src.solana import costs
    eng = bots().get(model)
    pnl = (eng.portfolio().equity - eng.ledger.contributed()) if eng else 0.0
    cost = _ai_cost(model) if model not in RULE_BOTS else 0.0
    return pnl, cost, pnl - cost


def _ai_cost(model: str, days: float | None = None) -> float:
    """Everything this AI bot spent — its own model and any boosted model it ran with (calls are tagged by bot)."""
    from src.solana import costs
    b = costs.book()
    return b.total(days=days, who=f"llm:{model}") or b.total(days=days, model=model)


BOOST_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")


def boost(model: str, to: str, cycles: int) -> dict:
    """Run an AI bot with a stronger model for N cycles (then it returns to its own model, e.g. Haiku)."""
    eng = bots().get(model)
    if not eng or not eng.llm:
        raise ValueError(f"{model} não é um bot de IA")
    if to not in BOOST_MODELS or not 1 <= int(cycles) <= 288:
        raise ValueError("modelo ou número de ciclos inválido (1–288)")
    eng.ledger.set_state("boost", {"model": to, "cycles_left": int(cycles), "total": int(cycles)})
    eng.llm._seen.clear()          # re-decide every market of this candle with the new model
    eng.ledger.log_decision("boost", reason=f"🚀 {to} por {cycles} ciclo(s); depois volta a {model}")
    return eng.ledger.get_state("boost")


def _apply_boost(model: str, eng):
    """Called before each lab cycle: use the boosted model while cycles remain, then return to the default."""
    if not eng.llm:
        return
    b = eng.ledger.get_state("boost") or {}
    if b.get("cycles_left", 0) > 0:
        eng.llm.model = b["model"]
        eng.ledger.set_state("boost", {**b, "cycles_left": b["cycles_left"] - 1})
    elif eng.llm.model != model:
        eng.llm.model = model
        eng.ledger.set_state("boost", {})
        eng.llm._seen.clear()
        eng.ledger.log_decision("boost", reason=f"fim do boost: voltou a {model}")


def _pause_if_unprofitable(model: str, eng) -> bool:
    if model in RULE_BOTS or not eng.ledger.get_state("running", False):
        return False
    pnl, cost, net = net_of_cost(model)
    if net < -cfg.LLM_BOT_MAX_NET_LOSS_USD:
        eng.ledger.set_state("running", False)
        eng.ledger.set_state("paused_reason", f"custa mais do que ganha (IA ${cost:.2f} vs lucro ${pnl:.2f})")
        eng.ledger.log_decision("pause", reason=f"auto-pause: AI cost ${cost:.2f} > trading P&L ${pnl:.2f}")
        return True
    return False


def run_round() -> dict:
    """One cycle for every bot (sequential: they share the public rate limits)."""
    out = {}
    for model, eng in bots().items():
        if _pause_if_unprofitable(model, eng):
            out[model] = {"paused": "unprofitable"}
            continue
        if eng.ledger.get_state("running", False) and (not eng.llm or eng.llm.enabled):
            _apply_boost(model, eng)
            try:
                out[model] = eng.run_cycle()
            except Exception as e:
                logger.exception(f"arena bot {model}")
                out[model] = {"error": str(e)}
    return out


def set_running(on: bool):
    """Start/stop the whole team. Without ANTHROPIC_API_KEY only the 'rules' bot can actually work."""
    from src.solana import engine as paper_engine
    for eng in bots().values():
        eng.ledger.set_state("running", on)
        if on:
            eng.ledger.set_state("paused_reason", None)
        eng.ledger.log_decision("arena", reason=f"arena {'started' if on else 'stopped'} {now_iso()}")
    sch = paper_engine.scheduler()
    if on and sch.get_job("arena_round") is None:
        from datetime import datetime
        sch.add_job(run_round, "interval", seconds=cfg.ARENA_CYCLE_SEC, id="arena_round", max_instances=1,
                    coalesce=True, next_run_time=datetime.now())
    return {"running": on, "llm_bots_need_key": not cfg.ANTHROPIC_API_KEY}


def leaderboard() -> list[dict]:
    from src.api.paper_routes import performance   # same TWR / hold maths as the main dashboard
    rows = []
    for model, eng in bots().items():
        led = eng.ledger
        p = eng.portfolio()
        trades = led.closed_trades()
        perf = performance(led.equity_history(100_000)).get("all", {})
        last = eng.llm.last if eng.llm else {}
        sleep = (led.get_state("sleep") or {}) if cfg.BOT_SELF_PAUSE else {}
        needs_key = bool(eng.llm) and not eng.llm.enabled
        paused = led.get_state("paused_reason")
        state = ((f"pausado: {paused}" if paused else "parado") if not led.get_state("running", False)
                 else "sem chave API" if needs_key
                 else "trabalhando" if eng.scanning() else "posição aberta" if led.positions()
                 else "dormindo" if sleep.get("until") else "aguardando o próximo ciclo")
        rows.append({
            "model": model, "running": led.get_state("running", False), "state": state, "needs_key": needs_key,
            "sleep_until": sleep.get("until"), "sleep_reason": sleep.get("reason"),
            "live": led.get_state("live", {}), "equity": round(p.equity, 2),
            "ai_cost_usd": round(net_of_cost(model)[1], 4), "net_usd": round(net_of_cost(model)[2], 4),
            "pnl_usd": round(p.equity - led.contributed(), 2), "twr": perf.get("twr", 0.0), "hold_btc": perf.get("hold_btc"),
            "trades": len(trades), "win_rate": round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 3) if trades else None,
            "open": [x["symbol"] for x in led.positions()], "reward": reward(model),
            "model_now": eng.llm.model if eng.llm else None, "boost": led.get_state("boost") or {},
            "last_decision": next(({"market": k, **{x: v.get(x) for x in ("action", "reason", "model")}}
                                   for k, v in sorted(last.items(), key=lambda kv: -(kv[1].get("t") or 0))), None)
                             if eng.llm else _rules_last(led),
            "cycle": eng.cycle, "kill_switch": eng.kill_switch().get("active", False),
        })
    return sorted(rows, key=lambda r: r["twr"] or 0, reverse=True)


def _rules_last(led) -> dict | None:
    d = next((x for x in led.decisions(20) if x["kind"] in ("entry", "veto", "exit", "sleep")), None)
    return {"market": d["symbol"] or "", "action": d["kind"], "reason": d["reason"]} if d else None


def resume_if_running():
    """After a restart, bots that were on keep working (state lives in each bot's ledger)."""
    if any(e.ledger.get_state("running", False) for e in bots().values()):
        set_running(True)
