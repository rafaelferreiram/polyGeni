"""
Natural selection for the reserves. Every EVOLVE_PERIOD_H (12h) of a reserve's life its trades of that period are
judged: win rate below EVOLVE_MIN_WIN_RATE (60%) — or no trade at all — and the bot is retired. Its place goes to a
successor named after a Palmeiras Libertadores champion (1999, 2020, 2021), same family of strategy, born with:
  1. adjusted exits learned from how the parent lost (wider stop if stops did the damage, closer target if the target
     was rarely reached, no forced 2h exit if time exits lost, longer hold if trades ran out of time),
  2. the parent's lessons: situations the parent lost in are avoided from day one (learning.avoid),
  3. a fresh record: zero trades, its own ledger, the full 20 trades / 12 wins path to the real account.
Titulares are never retired. Entry rules stay the family's (the backtests said loosening them loses money); evolution
tunes exits and what to avoid. Lineage (who descends from whom, with what changes) lives in lineage.json.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg

logger = logging.getLogger("solana.evolution")
CHAMPIONS = ["Weverton", "Zé Rafael", "Danilo", "Marcos Rocha", "Luan", "Alex",
             "Arce", "Zinho", "Paulo Nunes", "Oséas", "Júnior Baiano", "Roque Júnior", "César Sampaio", "Felipe Melo",
             "Gabriel Menino", "Patrick de Paula", "Scarpa", "Piquerez", "Mayke", "Viña", "Willian"]
PATH = os.getenv("LINEAGE_PATH", "lineage.json")


def load() -> dict:
    try:
        with open(PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"active": list(cfg.ARENA_MODELS), "bots": {}, "retired": []}


def save(data: dict):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, PATH)


def active_models() -> list[str]:
    return load()["active"]


def base_of(model: str) -> str:
    return load()["bots"].get(model, {}).get("base", model)


def register(data: dict | None = None):
    """Make every evolved bot a known strategy (RULE_BOTS / FAST_BOTS) so the engine, the accounts and the UI see it."""
    from src.solana import strategies as st
    for model, b in (data or load())["bots"].items():
        if model in st.RULE_BOTS:
            continue
        parent_cls = st.RULE_BOTS[b["base"]]
        st.RULE_BOTS[model] = evolved(parent_cls, model, b["params"])
        if b["base"] in st.FAST_BOTS:
            st.FAST_BOTS.add(model)
        if b["base"] in st.SHORT_BOTS:               # a short successor stays a lab-only short (same set as LAB_ONLY_BOTS)
            st.SHORT_BOTS.add(model)


def evolved(cls, name: str, params: dict):
    """The family's strategy with tuned exits: stop/target distance multipliers and its own max hold."""
    def adjust(sig):
        if sig is not None:
            sig.strategy = name
            sig.stop_pct = min(sig.stop_pct * params.get("stop_mult", 1.0), 0.5)
            sig.tp_pct *= params.get("tp_mult", 1.0)
        return sig

    def init(self, *a, **kw):
        cls.__init__(self, *a, **kw)
        self.name = name
        if params.get("max_hold_h"):
            self.max_hold_h = params["max_hold_h"]
    attrs = {"__init__": init, "name": name, "signal": lambda self, c: adjust(cls.signal(self, c))}
    if hasattr(cls, "decide"):
        attrs["decide"] = lambda self, c, ctx: adjust(cls.decide(self, c, ctx))
    if params.get("max_hold_h"):
        attrs["max_hold_h"] = params["max_hold_h"]
    return type(f"{cls.__name__}_{name}", (cls,), attrs)


def inherited_avoid(model: str) -> list[str]:
    return load()["bots"].get(model, {}).get("inherited_avoid", [])


def no_fast_exit(model: str) -> bool:
    return bool(load()["bots"].get(model, {}).get("params", {}).get("no_fast_exit"))


def mutate(trades: list[dict], params: dict, hold_h: float) -> tuple[dict, list[str]]:
    """New exit parameters from how the parent's trades ended, and the reasons in plain words."""
    p, why = dict(params), []
    n = len(trades) or 1
    kind = lambda t: ("time" if "max holding" in (t.get("reason") or "") else "tp" if "take-profit" in (t.get("reason") or "")
                      else "stop" if "stop-loss" in (t.get("reason") or "") else "other")
    by = {k: [t for t in trades if kind(t) == k] for k in ("time", "tp", "stop")}
    pnl = lambda ts: sum(t["pnl_usd"] for t in ts)
    if len(by["time"]) / n >= 0.5 and pnl(by["time"]) < 0:
        p["max_hold_h"] = max(round(min(hold_h, cfg.MAX_HOLD_CAP_H) * 0.5, 1), 0.5)
        why.append(f"sai mais cedo ({p['max_hold_h']:.1f}h): o pai perdia ficando preso até o tempo máximo (os dados: quanto mais tempo, pior)")
    if len(by["stop"]) / n >= 0.4 and pnl(by["stop"]) < 0:
        p["stop_mult"] = min(params.get("stop_mult", 1.0) * 1.25, 2.0)
        why.append(f"stop {p['stop_mult']:.2f}x mais largo: o pai era stopado demais")
    if trades and len(by["tp"]) / n < 0.15:
        p["tp_mult"] = max(params.get("tp_mult", 1.0) * 0.8, 0.5)
        why.append(f"alvo {p['tp_mult']:.2f}x mais perto: o pai quase nunca chegava ao alvo")
    elif len(by["tp"]) / n > 0.4:
        p["tp_mult"] = min(params.get("tp_mult", 1.0) * 1.15, 1.5)
        why.append(f"alvo {p['tp_mult']:.2f}x mais longe: o pai batia o alvo com facilidade")
    if not why:
        why.append("mesmas saídas do pai; herda o que ele aprendeu")
    return p, why


def judge(trades: list[dict], born: datetime, now: datetime) -> dict | None:
    """At each EVOLVE_PERIOD_H checkpoint of the bot's life: the verdict on the period that just ended (or None)."""
    period = timedelta(hours=cfg.EVOLVE_PERIOD_H)
    if now - born < period:
        return None
    k = int((now - born) / period)
    start, end = born + (k - 1) * period, born + k * period
    window = [t for t in trades if start.isoformat() <= t["exit_ts"] < end.isoformat()]
    wins = sum(t["pnl_usd"] > 0 for t in window)
    wr = wins / len(window) if window else 0.0
    return {"checkpoint": k, "end": end.isoformat(timespec="seconds"), "trades": len(window), "wins": wins,
            "win_rate": round(wr, 3), "pass": bool(window) and wr >= cfg.EVOLVE_MIN_WIN_RATE}


def check(now: datetime | None = None) -> list[dict]:
    """Scheduler job: judge every reserve at its checkpoint; replace the ones below the bar. Returns what happened."""
    from src.solana import arena, learning
    now = now or datetime.now(timezone.utc)
    data = load()
    events = []
    for model in list(data["active"]):
        eng = arena.bots().get(model)
        if not eng or arena.is_titular(model):
            continue
        trades = eng.ledger.closed_trades()
        born_s = eng.ledger.get_state("born_at")
        if not born_s:
            born_s = min((t["entry_ts"] for t in trades), default=now.isoformat(timespec="seconds"))
            eng.ledger.set_state("born_at", born_s)
        v = judge(trades, datetime.fromisoformat(born_s), now)
        if v is None or eng.ledger.get_state("judged_checkpoint") == v["checkpoint"]:
            continue
        eng.ledger.set_state("judged_checkpoint", v["checkpoint"])
        eng.ledger.set_state("last_verdict", v)
        if v["pass"]:
            eng.ledger.log_decision("evolution", reason=f"✅ passou no corte de {cfg.EVOLVE_PERIOD_H}h: {v['wins']}/{v['trades']} ({v['win_rate']:.0%})")
            continue
        events.append(replace(model, v, trades, learning.record(eng.ledger, model), data))
    if events:
        save(data)
        _refresh_accounts()
    return events


def replace(model: str, verdict: dict, trades: list[dict], record: dict, data: dict) -> dict:
    """Retire `model`, create its successor in the same slot (mutates `data`)."""
    from src.solana import arena, learning, strategies as st
    parent = data["bots"].get(model, {})
    base = parent.get("base", model)
    gen = parent.get("gen", 1) + 1
    used = {b.get("label") for b in data["bots"].values()}
    label = next((c for c in CHAMPIONS if c not in used), f"{base} g{gen}")
    new = f"{base}_g{gen}_{len(data['bots']) + 1}"
    hold = getattr(st.RULE_BOTS[model](), "max_hold_h", None) or cfg.RISK_MAX_HOLD_H
    params, why = mutate(trades, parent.get("params", {}), hold)
    seen = learning.summary(record)["seen"] or 1
    # only specific situations: a tag on most of the parent's trades ("alta", its own setup) would mean never trading
    avoid = sorted(set(parent.get("inherited_avoid", [])) | {t for t, r in record.items()
                   if r["n"] >= 3 and r["n"] <= seen / 2 and r["wins"] / r["n"] < cfg.LEARN_MAX_WIN_RATE and r["pnl"] < 0})
    data["bots"][new] = {"label": label, "base": base, "gen": gen, "parent": model, "parent_label": parent.get("label"),
                         "born_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "params": params,
                         "why": why, "inherited_avoid": avoid}
    data["active"][data["active"].index(model)] = new
    data["retired"].append({"model": model, "label": parent.get("label"), "at": verdict["end"], "verdict": verdict, "successor": new})
    old = arena.bots().pop(model)
    old.ledger.set_state("running", False)
    old.ledger.log_decision("evolution", reason=f"❌ aposentado no corte de {cfg.EVOLVE_PERIOD_H}h: {verdict['wins']}/{verdict['trades']} "
                                                 f"({verdict['win_rate']:.0%} < {cfg.EVOLVE_MIN_WIN_RATE:.0%}); sucessor: {label}")
    register(data)
    eng = arena.build_bot(new)
    eng.ledger.set_state("born_at", data["bots"][new]["born_at"])
    eng.ledger.set_state("running", any(e.ledger.get_state("running", False) for e in arena.bots().values()))
    eng.ledger.log_decision("evolution", reason=f"🐣 nasceu {label} (geração {gen}, filho de {parent.get('label') or model}): " + "; ".join(why)
                            + (f"; já evita: {', '.join(avoid)}" if avoid else ""))
    arena.bots()[new] = eng
    learning._cache.clear()
    logger.info(f"evolution: {model} -> {new} ({label})")
    return {"retired": model, "born": new, "label": label, "why": why, "verdict": verdict}


def _refresh_accounts():
    from src.solana import hl_live
    for get in (hl_live.get_live_engine, hl_live.get_shadow_engine):
        try:
            get().llm = hl_live.team()
        except Exception as e:
            logger.warning(f"evolution: team refresh failed: {e}")
