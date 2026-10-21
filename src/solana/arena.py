"""
The lab: one paper bot per model (plus the deterministic 'rules' baseline), each with its own $20 wallet and
the real account's exact limits, trading the same Hyperliquid markets. Their results per market are what the
Coordinator uses to decide which bot's proposal executes on the shared (real) wallet.
"""
import logging
import re

from src.solana import config as cfg
from src.solana import hyperliquid as hl
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, Vote
from src.solana.engine import PaperEngine, bench_prices
from src.solana.ledger import Ledger, now_iso
from src.solana.llm_trader import LLMDecider

logger = logging.getLogger("solana.arena")


class _PerpsSafety:
    min_age_h = 0

    def check(self, c):
        return Vote("safety", True, "n/a for perps")


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def build_bot(model: str, db_dir: str = ".", llm_fn=None, scout=None, broker_factory=None, candles_fn=None) -> PaperEngine:
    from src.solana.hl_live import _risk          # identical limits to the real account
    from src.solana.strategies import default_strategies
    ledger = Ledger(f"{db_dir}/arena_{_slug(model)}.db", cfg.ARENA_START_USDC)
    decider = None
    if model != "rules":
        decider = LLMDecider(model=model, llm_fn=llm_fn)
        decider.name = f"llm:{model}"
    eng = PaperEngine(
        ledger, [scout or hl.HyperliquidScout()], _PerpsSafety(), default_strategies() if model == "rules" else [],
        DebateAgent(enabled=False), _risk(), ExecutorAgent((broker_factory or hl.HyperliquidPaperBroker)(ledger)),
        candles_fn=candles_fn or {hl.VENUE: hl.candles}, bench_fn=bench_prices if candles_fn is None else None,
        llm=decider, profiles={hl.VENUE: {"max_pos_pct": 1.0, "stop_mult": cfg.MARKET_PROFILES.get(hl.VENUE, {}).get("stop_mult", 1.5)}},
    )
    ledger.set_state("decision_mode", "rules" if model == "rules" else "llm")
    ledger.set_state("model", model)
    return eng


def lab_decider(model: str):
    """The lab bot's LLMDecider, so the shared wallet can reuse its decision for the same candle."""
    eng = (_bots or {}).get(model)
    return eng.llm if eng else None


def score(model: str, market: str | None = None) -> tuple[float, int]:
    """Shrunk expectancy per trade (USD) of a lab bot, on one market or overall: sum(pnl) / (n + 3)."""
    eng = (_bots or {}).get(model)
    if not eng:
        return 0.0, 0
    trades = [t for t in eng.ledger.closed_trades() if market is None or t["symbol"] == market]
    return sum(t["pnl_usd"] for t in trades) / (len(trades) + 3), len(trades)


def eligible_for_real(members: list) -> list:
    """Real money only for bots with evidence: the rules bot (validated in study_v2) and AI bots with
    ≥ LIVE_MIN_LAB_TRADES lab trades and positive expectancy."""
    ok = []
    for m in members:
        if m.model == "rules":
            ok.append(m)
            continue
        exp, n = score(m.model)
        if n >= cfg.LIVE_MIN_LAB_TRADES and exp > 0:
            ok.append(m)
    return ok


def rank(market: str, members: list) -> list:
    """Coordinator: best lab track record on this market first, then overall, then the configured order."""
    order = {m: i for i, m in enumerate(cfg.ARENA_MODELS)}
    return sorted(members, key=lambda m: (-score(m.model, market)[0], -score(m.model)[0], order.get(m.model, 99)))


_bots: dict[str, PaperEngine] | None = None


def bots() -> dict[str, PaperEngine]:
    global _bots
    if _bots is None:
        _bots = {m: build_bot(m) for m in cfg.ARENA_MODELS}
    return _bots


def run_round() -> dict:
    """One cycle for every bot (sequential: they share the public rate limits)."""
    out = {}
    for model, eng in bots().items():
        if eng.ledger.get_state("running", False) and (not eng.llm or eng.llm.enabled):
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
        sleep = led.get_state("sleep") or {}
        needs_key = bool(eng.llm) and not eng.llm.enabled
        state = ("parado" if not led.get_state("running", False) else "sem chave API" if needs_key
                 else "a trabalhar" if eng.scanning() else "posição aberta" if led.positions()
                 else "a dormir" if sleep.get("until") else "à espera do próximo ciclo")
        rows.append({
            "model": model, "running": led.get_state("running", False), "state": state, "needs_key": needs_key,
            "sleep_until": sleep.get("until"), "sleep_reason": sleep.get("reason"),
            "live": led.get_state("live", {}), "equity": round(p.equity, 2),
            "pnl_usd": round(p.equity - led.contributed(), 2), "twr": perf.get("twr", 0.0), "hold_btc": perf.get("hold_btc"),
            "trades": len(trades), "win_rate": round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 3) if trades else None,
            "open": [x["symbol"] for x in led.positions()],
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
