"""
The lab: one paper bot per model (plus the deterministic 'rules' baseline), each with its own $20 wallet and
the real account's exact limits, trading the same Hyperliquid markets. Their results per market are what the
Coordinator uses to decide which bot's proposal executes on the shared (real) wallet.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg
from src.solana import learning
from src.solana import hyperliquid as hl
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, Vote
from src.solana.engine import PaperEngine, bench_prices
from src.solana.ledger import Ledger, now_iso
from src.solana.llm_trader import LLMDecider
from src.solana.strategies import FAST_BOTS, LAB_ONLY_BOTS, RULE_BOTS, SHORT_BOTS

logger = logging.getLogger("solana.arena")


class _PerpsSafety:
    min_age_h = 0

    def check(self, c):
        return Vote("safety", True, "n/a for perps")


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def build_bot(model: str, db_dir: str = ".", llm_fn=None, scout=None, broker_factory=None, candles_fn=None) -> PaperEngine:
    from src.solana.hl_live import _risk          # real-account trade size and stops, more lab slots
    ledger = Ledger(f"{db_dir}/arena_{_slug(model)}.db", cfg.ARENA_START_USDC)
    if ledger.contributed() < cfg.LAB_CAPITAL_USD:          # paper top-up: counted as a contribution, never as profit
        ledger.move_cash("deposit", cfg.LAB_CAPITAL_USD - ledger.contributed(), "lab capital for faster evidence")
    decider = None
    if model not in RULE_BOTS:
        decider = LLMDecider(model=model, llm_fn=llm_fn)
        decider.name = f"llm:{model}"
        decider.record_fn = lambda: track_record(model)        # the AI sees its own reward and recent trades
    eng = PaperEngine(
        ledger, [scout or (hl.ShortScout() if model in SHORT_BOTS else hl.HyperliquidScout())], _PerpsSafety(), [RULE_BOTS[model]()] if model in RULE_BOTS else [],
        DebateAgent(enabled=False), _risk(lab=True),
        ExecutorAgent(broker_factory(ledger) if broker_factory else hl.HyperliquidPaperBroker(ledger, leverage=cfg.LIVE_LEVERAGE)),
        candles_fn=candles_fn or {hl.VENUE: (lambda coin: hl.candles(coin, "15m")) if model in FAST_BOTS else hl.candles},
        bench_fn=bench_prices if candles_fn is None else None,
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


def green_light(model: str) -> dict:
    """Is this bot ready for real money? Trusted (backtest-validated) bots are; the others need GREEN_LIGHT_TRADES
    closed lab trades, GREEN_LIGHT_WINS of them winners, with net profit after their AI cost, win rate ≥ GREEN_LIGHT_WIN_RATE and profit factor ≥
    GREEN_LIGHT_PROFIT_FACTOR."""
    if model in cfg.REAL_TRUSTED_BOTS and model not in LAB_ONLY_BOTS:
        return {"ok": True, "trusted": True}
    eng = (_bots or {}).get(model)
    trades = eng.ledger.closed_trades() if eng else []
    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [-t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    n, net = len(trades), net_of_cost(model)[2] if eng else 0.0
    wr = len(wins) / n if n else 0.0
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else (99.0 if wins else 0.0)
    need = {"trades": n >= cfg.GREEN_LIGHT_TRADES, "vitórias": len(wins) >= cfg.GREEN_LIGHT_WINS, "lucro": net > 0, "acerto": wr >= cfg.GREEN_LIGHT_WIN_RATE,
            "fator": pf >= cfg.GREEN_LIGHT_PROFIT_FACTOR}
    if model in SHORT_BOTS:      # the real account cannot short: this bot stays in the lab whatever its record
        need["conta real não faz short"] = False
    return {"ok": all(need.values()), "trusted": False, "trades": n, "wins": len(wins), "needed": cfg.GREEN_LIGHT_WINS, "needed_trades": cfg.GREEN_LIGHT_TRADES,
            "win_rate": round(wr, 3), "profit_factor": round(pf, 2), "net_usd": round(net, 4),
            "missing": [k for k, v in need.items() if not v]}


def paper_scheduler():
    from src.solana import engine as paper_engine
    return paper_engine.scheduler()


def _top_up(eng):
    if eng.ledger.contributed() < cfg.TRAIN_BOOST_CAPITAL_USD:      # paper top-up, a contribution — never profit
        eng.ledger.move_cash("deposit", cfg.TRAIN_BOOST_CAPITAL_USD - eng.ledger.contributed(), "treino intensivo")


def boost_training(model: str, hours: float = 24) -> dict:
    """'Treino intensivo': double the lab slots and lift the daily entry cap for a while, so the bot closes more lab
    trades (more evidence, faster) — entry rules, size, stops and targets stay exactly the same."""
    eng = bots().get(model)
    if not eng:
        raise ValueError(f"bot {model} não existe")
    until = (datetime.now(timezone.utc) + timedelta(hours=min(max(hours, 1), 72))).isoformat(timespec="seconds")
    eng.ledger.set_state("train_boost_until", until)
    _top_up(eng)
    local = datetime.fromisoformat(until).astimezone(cfg.TZ).strftime("%d/%m %H:%M")
    eng.ledger.log_decision("boost", reason=f"🚀 treino intensivo até {local} (Lisboa): {cfg.TRAIN_BOOST_MAX_POSITIONS} vagas, sem limite diário")
    _apply_training_boost(eng)
    job = paper_scheduler().get_job("arena_round")
    if job:                                  # analyse right away instead of waiting for the next lab round
        job.modify(next_run_time=datetime.now())
    return {"until": until}


def turbo(model: str) -> dict:
    """'Turbo do dia' for a bot in training: until midnight (Lisbon) it analyses the market every TURBO_CYCLE_SEC
    and, from now on, closes positions within TURBO_MAX_HOLD_H (fast exits, also on the real account once promoted)
    instead of once per lab round, with every slot and no daily cap (training boost). The AI bot's every-coin scan is a
    separate, paid switch (ai_full_scan). More looks at the market = setups caught as soon as they appear and stops/targets checked more often,
    so lab trades pile up faster (goal: ≥ 10 a day). Entry rules, size, stops and targets do not change."""
    eng = bots().get(model)
    if not eng:
        raise ValueError(f"bot {model} não existe")
    if green_light(model).get("ok"):
        raise ValueError(f"{model} já está pronto para a conta real")
    now = datetime.now(timezone.utc)
    midnight = datetime.combine(now.astimezone(cfg.TZ).date() + timedelta(days=1), datetime.min.time(), cfg.TZ)
    hours = max((midnight - now).total_seconds() / 3600, 1)
    until = midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
    eng.ledger.set_state("turbo_until", until)
    eng.ledger.set_state("fast_exits", True)        # the user's choice: turbo bots trade with short holds from now on
    if eng.ledger.get_state("train_boost_until", "") < until:
        boost_training(model, hours)
    _top_up(eng)
    eng.ledger.log_decision("boost", reason=f"⚡ turbo até 00:00 (Lisboa): analisa a cada {cfg.TURBO_CYCLE_SEC}s")
    _schedule_turbo(model, eng)
    return {"until": until, "every_s": cfg.TURBO_CYCLE_SEC}


def fast_exit_hold(strategy: str) -> float | None:
    """Max hours a position may stay open for a bot trained with turbo (None = its normal hold)."""
    eng = (_bots or {}).get(strategy)
    if not eng or not eng.ledger.get_state("fast_exits", False) or is_titular(strategy):
        return None              # titulares keep their strategy's own exits (a forced 2h exit cost Evair money)
    return cfg.TURBO_FAST_MAX_HOLD_H if strategy in FAST_BOTS else cfg.TURBO_MAX_HOLD_H


def _schedule_turbo(model: str, eng):
    job_id = f"turbo_{model}"

    def tick():
        if datetime.now(timezone.utc).isoformat() >= (eng.ledger.get_state("turbo_until") or ""):
            paper_scheduler().remove_job(job_id)
            return
        if eng.ledger.get_state("running", False) and (not eng.llm or eng.llm.enabled):
            _apply_training_boost(eng)
            eng.run_cycle()              # returns at once if the lab round is running this bot right now
    sch = paper_scheduler()
    if sch.get_job(job_id) is None:
        sch.add_job(tick, "interval", seconds=cfg.TURBO_CYCLE_SEC, id=job_id, max_instances=1, coalesce=True,
                    next_run_time=datetime.now())


def coffee(model: str, hours: float = 4) -> dict:
    """☕ for one bot: no self-pause and twice the daily entries for a few hours. A titular also wakes both accounts
    (it trades there); a reserve gets it in the lab only, at most once per Lisbon day."""
    eng = bots().get(model)
    if not eng:
        raise ValueError(f"bot {model} não existe")
    hours = max(0.5, min(hours, 12))
    today = datetime.now(cfg.TZ).date().isoformat()
    if not is_titular(model):
        if eng.ledger.get_state("coffee_day") == today:
            raise ValueError("reserva: só 1 café por dia — amanhã tem mais")
        eng.ledger.set_state("coffee_day", today)
        return {"coffee_until": eng.give_coffee(hours), "accounts": []}
    from src.solana import hl_live
    for e in (eng, hl_live.get_shadow_engine(), hl_live.get_live_engine()):
        until = e.give_coffee(hours)
    return {"coffee_until": until, "accounts": ["sim", "real"]}


def ai_full_scan(model: str, hours: float = 24) -> dict:
    """Training for the AI bot: it analyses every coin on every closed candle (no pre-filter) for a while.
    Costs real AI money (~29 calls/hour with Haiku ≈ $0.03/h); the monthly AI allowance still caps it."""
    eng = bots().get(model)
    if not eng or not eng.llm:
        raise ValueError(f"{model} não é um bot de IA")
    until = (datetime.now(timezone.utc) + timedelta(hours=min(max(hours, 1), 48))).isoformat(timespec="seconds")
    eng.ledger.set_state("ai_full_scan_until", until)
    eng.llm._seen.clear()
    local = datetime.fromisoformat(until).astimezone(cfg.TZ).strftime("%d/%m %H:%M")
    eng.ledger.log_decision("boost", reason=f"🧠 análise de todas as moedas a cada vela até {local} (Lisboa)")
    _apply_training_boost(eng)
    return {"until": until}


def _apply_training_boost(eng):
    now = datetime.now(timezone.utc).isoformat()
    if eng.llm:
        scan = eng.ledger.get_state("ai_full_scan_until")
        eng.llm.full_scan = bool(scan) and now < scan
    until = eng.ledger.get_state("train_boost_until")
    on = bool(until) and now < until
    eng.risk.max_positions = cfg.TRAIN_BOOST_MAX_POSITIONS if on else cfg.LAB_MAX_POSITIONS
    eng.risk.max_entries_per_day = 10_000 if on else cfg.LAB_MAX_ENTRIES_PER_DAY
    eng.risk.max_capital_usd = max(cfg.TRAIN_BOOST_CAPITAL_USD, cfg.LAB_CAPITAL_USD) if on else cfg.LAB_CAPITAL_USD


def eligible_for_real(members: list) -> list:
    """Real money only for bots with evidence: REAL_TRUSTED_BOTS (backtest-validated rule bots), and every other bot
    (AI) once it has ≥ LIVE_MIN_LAB_TRADES lab trades and positive expectancy after its AI cost."""
    ok = []
    for m in members:
        if m.model in cfg.REAL_TRUSTED_BOTS:
            ok.append(m)
            continue
        if green_light(m.model)["ok"]:
            ok.append(m)
    if len(ok) > cfg.MAX_TITULARES:      # at most MAX_TITULARES play: the best by reward points, then lab profit
        best = sorted(ok, key=lambda m: (-reward(m.model).get("points", 0), -net_of_cost(m.model)[2]))[:cfg.MAX_TITULARES]
        ok = [m for m in ok if m in best]
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


def is_titular(model: str) -> bool:
    return bool(green_light(model).get("ok"))


def reviewer_model(strategy: str) -> str | None:
    """Model the user picked to review this titular's signals on the accounts, while it lasts (None = default)."""
    eng = (_bots or {}).get(strategy)
    b = (eng.ledger.get_state("review_boost") if eng else None) or {}
    return b["model"] if b.get("until", "") > datetime.now(timezone.utc).isoformat() else None


def boost(model: str, to: str, cycles: int) -> dict:
    """Titulares only: a stronger model for N account cycles. An AI bot decides with it; a rule bot gets its signals
    reviewed by it (the reviewer that can veto a buy). Then it returns to the default (Haiku)."""
    eng = bots().get(model)
    if not eng:
        raise ValueError(f"bot {model} não existe")
    if to not in BOOST_MODELS or not 1 <= int(cycles) <= 288:
        raise ValueError("modelo ou número de ciclos inválido (1–288)")
    if not is_titular(model):
        raise ValueError("só titulares podem usar outro modelo — reservas têm 1 café por dia e o turbo")
    if not eng.llm:
        until = (datetime.now(timezone.utc) + timedelta(seconds=int(cycles) * cfg.LIVE_CYCLE_SEC)).isoformat(timespec="seconds")
        eng.ledger.set_state("review_boost", {"model": to, "until": until, "cycles": int(cycles)})
        eng.ledger.log_decision("boost", reason=f"🚀 sinais revisados por {to} por {cycles} ciclo(s) da conta")
        return eng.ledger.get_state("review_boost")
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
            _apply_training_boost(eng)
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
            "green_light": green_light(model), "train_boost_until": led.get_state("train_boost_until"),
            "ai_full_scan_until": led.get_state("ai_full_scan_until"), "turbo_until": led.get_state("turbo_until"),
            "coffee_until": led.get_state("coffee_until"), "coffee_day": led.get_state("coffee_day"),
            "review_boost": led.get_state("review_boost"),
            "learned": learning.summary(learning.record(led, model)),
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
    now = datetime.now(timezone.utc).isoformat()
    for model, eng in bots().items():           # a turbo survives a restart until its midnight
        if (eng.ledger.get_state("turbo_until") or "") > now:
            _schedule_turbo(model, eng)
