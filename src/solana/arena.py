"""
Model arena (paper): one bot per LLM model trading the same Hyperliquid markets with the same RiskAgent,
each in its own ledger (arena_<model>.db), so results are directly comparable. Opt-in: it costs API tokens.
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
    ledger = Ledger(f"{db_dir}/arena_{_slug(model)}.db", cfg.ARENA_START_USDC)
    decider = LLMDecider(model=model, llm_fn=llm_fn)
    decider.name = f"llm:{model}"
    eng = PaperEngine(
        ledger, [scout or hl.HyperliquidScout()], _PerpsSafety(), [], DebateAgent(enabled=False), RiskAgent(),
        ExecutorAgent((broker_factory or hl.HyperliquidPaperBroker)(ledger)),
        candles_fn=candles_fn or {hl.VENUE: hl.candles}, bench_fn=bench_prices if candles_fn is None else None,
        llm=decider, profiles={hl.VENUE: cfg.MARKET_PROFILES.get(hl.VENUE, {})},
    )
    ledger.set_state("decision_mode", "llm")
    ledger.set_state("model", model)
    return eng


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
        if eng.ledger.get_state("running", False):
            try:
                out[model] = eng.run_cycle()
            except Exception as e:
                logger.exception(f"arena bot {model}")
                out[model] = {"error": str(e)}
    return out


def set_running(on: bool):
    from src.solana import engine as paper_engine
    if on and not cfg.ANTHROPIC_API_KEY:
        raise ValueError("a arena precisa de ANTHROPIC_API_KEY no .env")
    for eng in bots().values():
        eng.ledger.set_state("running", on)
        eng.ledger.log_decision("arena", reason=f"arena {'started' if on else 'stopped'} {now_iso()}")
    sch = paper_engine.scheduler()
    if on and sch.get_job("arena_round") is None:
        sch.add_job(run_round, "interval", seconds=cfg.ARENA_CYCLE_SEC, id="arena_round", max_instances=1, coalesce=True)
    return {"running": on}


def leaderboard() -> list[dict]:
    from src.api.paper_routes import performance   # same TWR / hold maths as the main dashboard
    rows = []
    for model, eng in bots().items():
        led = eng.ledger
        p = eng.portfolio()
        trades = led.closed_trades()
        perf = performance(led.equity_history(100_000)).get("all", {})
        last = eng.llm.last if eng.llm else {}
        rows.append({
            "model": model, "running": led.get_state("running", False), "equity": round(p.equity, 2),
            "pnl_usd": round(p.equity - led.contributed(), 2), "twr": perf.get("twr", 0.0), "hold_btc": perf.get("hold_btc"),
            "trades": len(trades), "win_rate": round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 3) if trades else None,
            "open": [x["symbol"] for x in led.positions()],
            "last_decision": next(({"market": k, **v} for k, v in last.items()), None),
            "cycle": eng.cycle, "kill_switch": eng.kill_switch().get("active", False),
        })
    return sorted(rows, key=lambda r: r["twr"] or 0, reverse=True)
