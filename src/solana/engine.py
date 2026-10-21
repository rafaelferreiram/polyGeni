"""
24/7 paper loop: heartbeat -> reconcile ledger<->positions -> mark-to-market & exits
-> kill-switch check -> scout/safety/strategy/debate/risk/executor -> equity snapshot.

Run a few cycles from the shell:  python -m src.solana.engine --cycles 3
"""
import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from src.solana import config as cfg
from src.solana import market_data as md
from src.solana.agents import (DebateAgent, ExecutorAgent, Portfolio, RiskAgent, SafetyAgent,
                               ScoutAgent, Vote)
from src.solana.broker import PaperBroker
from src.solana.ledger import Ledger, now_iso
from src.solana import hyperliquid as hl
from src.solana.research import ResearchAgent
from src.solana.strategies import default_strategies

logger = logging.getLogger("solana.engine")


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class PaperEngine:
    def __init__(self, ledger: Ledger, scout, safety: SafetyAgent, strategies: list,
                 debate: DebateAgent, risk: RiskAgent, executor: ExecutorAgent,
                 candles_fn=md.pool_candles, max_hold_h: float = cfg.RISK_MAX_HOLD_H, research=None,
                 bench_fn=None, profiles: dict | None = None):
        self.ledger, self.safety, self.strategies = ledger, safety, strategies
        self.scouts = scout if isinstance(scout, list) else [scout]
        self.debate, self.risk, self.executor = debate, risk, executor
        # one candle source for every venue, or {venue: fn}
        self.candles_fns = candles_fn if isinstance(candles_fn, dict) else None
        self.candles_fn, self.max_hold_h = candles_fn, max_hold_h
        self.bench_fn = bench_fn
        self.profiles = profiles if profiles is not None else cfg.MARKET_PROFILES
        self.research = research
        self._safety_logged: dict[str, float] = {}

    # ─── state helpers ───
    @property
    def cycle(self) -> int:
        return self.ledger.get_state("cycle", 0)

    def kill_switch(self) -> dict:
        return self.ledger.get_state("kill_switch", {"active": False})

    def trip_kill_switch(self, reason: str):
        self.ledger.set_state("kill_switch", {"active": True, "reason": reason, "ts": now_iso()})
        self.ledger.log_decision("kill_switch", cycle=self.cycle, reason=reason,
                                 votes={"risk": {"approve": False, "reason": reason}})

    def reset_kill_switch(self, note: str = "manual reset"):
        self.ledger.set_state("kill_switch", {"active": False, "reason": note, "ts": now_iso()})
        self.ledger.log_decision("kill_switch", cycle=self.cycle, reason=f"reset: {note}")

    # ─── pause per market (instrument id or whole venue) ───
    def paused(self) -> list[str]:
        return self.ledger.get_state("paused_markets", [])

    def set_paused(self, market: str, paused: bool):
        cur = [m for m in self.paused() if m != market]
        self.ledger.set_state("paused_markets", cur + ([market] if paused else []))
        self.ledger.log_decision("pause" if paused else "resume", cycle=self.cycle, mint=market,
                                 reason=f"market {'paused' if paused else 'resumed'} manually")

    def _candles(self, c):
        fn = self.candles_fns.get(c.venue) if self.candles_fns else self.candles_fn
        return fn(c.pair_address)

    def _agent(self, name: str, **kw):
        agents = self.ledger.get_state("agents", {})
        a = agents.setdefault(name, {"approved": 0, "vetoed": 0})
        a.update(last_run=now_iso(), last_ok=not kw.get("vetoed"),
                 **{k: v for k, v in kw.items() if k not in ("approved", "vetoed")})
        a["approved"] += kw.get("approved", 0)
        a["vetoed"] += kw.get("vetoed", 0)
        self.ledger.set_state("agents", agents)

    # ─── portfolio ───
    def portfolio(self) -> Portfolio:
        positions = self.ledger.positions()
        exposure, by_strat = 0.0, {}
        for p in positions:
            v = p["mark_value"] if p["mark_value"] is not None else p["cost_usd"]
            exposure += v
            by_strat[p["strategy"]] = by_strat.get(p["strategy"], 0) + v
        cash = self.ledger.cash()
        equity = cash + exposure
        day = self.ledger.get_state("day_start") or {}
        day_equity = day.get("equity", equity) + (self.ledger.contributed() - day.get("contributed", self.ledger.contributed()))
        today = datetime.now(timezone.utc).date().isoformat()
        entries_today = sum(1 for f in self.ledger.fills(500) if f["side"] == "buy" and f["ts"].startswith(today))
        return Portfolio(equity=equity, cash=cash, exposure=exposure, open_mints={p["mint"] for p in positions},
                         strategy_exposure=by_strat, day_start_equity=day_equity,
                         kill_switch=self.kill_switch().get("active", False), entries_today=entries_today)

    # ─── reconciliation: the ledger is the source of truth ───
    def reconcile(self) -> list[str]:
        truth = self.ledger.positions_from_fills()
        table = {p["mint"]: p for p in self.ledger.positions()}
        issues = []
        for mint, t in truth.items():
            row = table.get(mint)
            if row is None or abs(row["qty"] - t["qty"]) > 1e-9 * max(1, t["qty"]):
                if row is not None:
                    issues.append(f"{t['symbol']}: table qty {row['qty']} != ledger {t['qty']}")
                self.ledger.upsert_position({**t, "mark_price": (row or {}).get("mark_price"),
                                             "mark_value": (row or {}).get("mark_value")})
        for mint, row in table.items():
            if mint not in truth:
                issues.append(f"{row['symbol']}: position without ledger backing — removed")
                self.ledger.delete_position(mint)
        for msg in issues:
            self.ledger.log_decision("reconcile", cycle=self.cycle, reason=msg)
        return issues

    # ─── exits ───
    def mark_and_exit(self):
        now = datetime.now(timezone.utc)
        for pos in self.ledger.positions():
            try:
                value = self.executor.mark(pos)
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                         reason=f"mark-to-market failed: {e}")
                continue
            price = value / pos["qty"]
            self.ledger.set_mark(pos["mint"], price, value)
            held_h = (now - _ts(pos["opened_at"])).total_seconds() / 3600
            reason = None
            if pos["stop_price"] and price <= pos["stop_price"]:
                reason = f"stop-loss hit ({price:.6g} <= {pos['stop_price']:.6g})"
            elif pos["tp_price"] and price >= pos["tp_price"]:
                reason = f"take-profit hit ({price:.6g} >= {pos['tp_price']:.6g})"
            elif held_h >= self.max_hold_h:
                reason = f"max holding time {self.max_hold_h:.0f}h reached"
            if reason:
                fill = self.executor.close(f"exit-{pos['entry_order_id']}", pos, reason)
                pnl = fill["usd_gross"] - fill["fee_usd"] - pos["cost_usd"]
                self.ledger.log_decision("exit", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                         strategy=pos["strategy"], order_id=fill["order_id"], reason=reason,
                                         signals={"mark_price": price, "held_h": round(held_h, 1),
                                                  "pnl_usd": round(pnl, 4)},
                                         votes={"risk": {"approve": True, "reason": reason}})
                self.ledger.delete_position(pos["mint"])

    # ─── one full cycle ───
    def run_cycle(self) -> dict:
        cycle = self.cycle + 1
        self.ledger.set_state("cycle", cycle)
        self.ledger.set_state("heartbeat", {"ts": now_iso(), "cycle": cycle})
        stats = {"cycle": cycle, "candidates": 0, "safe": 0, "signals": 0, "entries": 0, "vetoes": 0}
        try:
            self.reconcile()
            try:
                self.executor.accrue(self.ledger.positions())   # perp funding
            except Exception as e:
                self.ledger.log_decision("error", cycle=cycle, reason=f"funding accrual failed: {e}")
            self.mark_and_exit()
            self.reconcile()

            today = datetime.now(timezone.utc).date().isoformat()
            if (self.ledger.get_state("day_start") or {}).get("date") != today:
                p = self.portfolio()
                self.ledger.set_state("day_start", {"date": today, "equity": p.equity,
                                                    "contributed": self.ledger.contributed()})
            p = self.portfolio()
            if not p.kill_switch and self.risk.daily_loss_breached(p):
                self.trip_kill_switch(f"daily loss ≥ {self.risk.max_daily_loss_pct:.0%} "
                                      f"(${p.day_start_equity:.2f} → ${p.equity:.2f})")
            if not self.kill_switch().get("active"):
                self._entries(stats)
            else:
                stats["skipped"] = "kill switch active"
        except Exception as e:
            logger.exception("cycle failed")
            self.ledger.log_decision("error", cycle=cycle, reason=f"cycle error: {e}")
            stats["error"] = str(e)
        finally:
            self.reconcile()
            p = self.portfolio()
            bench = {}
            if self.bench_fn:
                try:
                    bench = self.bench_fn()
                except Exception as e:
                    logger.warning(f"benchmark prices: {e}")
            self.ledger.record_equity(cycle, p.cash, p.exposure, bench)
            stats.update(equity=round(p.equity, 4), cash=round(p.cash, 4), positions=len(p.open_mints),
                         finished=now_iso())
            self.ledger.set_state("last_cycle", stats)
            logger.info(f"[paper] cycle {cycle}: {stats}")
        return stats

    def _entries(self, stats: dict):
        candidates = []
        for scout in self.scouts:
            try:
                candidates += scout.candidates()
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, reason=f"scout {type(scout).__name__} failed: {e}")
        stats["candidates"] = len(candidates)
        self.ledger.set_state("markets", [{"id": c.mint, "symbol": c.symbol, "venue": c.venue,
                                           "price": c.price_usd} for c in candidates])
        self._agent("scout", last_reason=f"{len(candidates)} candidates", approved=len(candidates))
        paused = set(self.paused())
        for c in candidates:
            if c.mint in paused or c.venue in paused:
                continue
            profile = self.profiles.get(c.venue, {})
            if c.venue == "jupiter":
                safety = self.safety.check(c)
            else:   # rug checks are about SPL mints; perps markets are a fixed, listed universe
                safety = Vote("safety", True, f"n/a for {c.venue} perps (fixed market list)")
            if not safety.approve:
                self._agent("safety", last_reason=f"{c.symbol}: {safety.reason}", vetoed=1)
                if time.time() - self._safety_logged.get(c.mint, 0) > 3600:  # avoid flooding the log
                    self._safety_logged[c.mint] = time.time()
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             signals=c.summary(), votes=_votes(safety), reason=f"safety: {safety.reason}")
                continue
            stats["safe"] += 1
            self._agent("safety", last_reason=f"{c.symbol}: passed", approved=1)
            try:
                candles = self._candles(c)[:-1]  # drop the still-open candle
                age_h = (time.time() - candles[-1]["t"]) / 3600 if candles else None
                if age_h is None or age_h > cfg.STALE_CANDLE_H:
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             reason=f"risk: stale or missing price data ({age_h if age_h is None else round(age_h, 1)}h old)")
                    continue
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                         reason=f"candles unavailable: {e}")
                continue
            for strat in self.strategies:
                sig = strat.signal(candles)
                if sig is not None and profile.get("stop_mult", 1) != 1:
                    sig.stop_pct = min(sig.stop_pct * profile["stop_mult"], 0.5)
                    sig.tp_pct *= profile["stop_mult"]
                if sig is None:
                    self._agent(strat.name, last_reason=f"{c.symbol}: no signal")
                    continue
                stats["signals"] += 1
                self._agent(strat.name, last_reason=f"{c.symbol}: {sig.reason}", approved=1)
                strat_vote = Vote(strat.name, True, sig.reason, sig.indicators)
                closes = [k["close"] for k in candles[-24:]]
                brief, research_vote = None, None
                if self.research is not None and c.venue == "jupiter":
                    try:
                        brief = self.research.brief(c.mint, c.symbol, {"signal": sig.reason, **c.summary()})
                    except Exception as e:
                        logger.warning(f"research {c.symbol}: {e}")
                    research_vote = self.research.vote(brief)
                    self._agent("research", last_reason=f"{c.symbol}: {research_vote.reason}",
                                approved=int(research_vote.approve), vetoed=int(not research_vote.approve))
                debate = self.debate.review(c, sig, {"closes_24h": closes, "safety": safety.detail,
                                                     "research_brief": brief})
                self._agent("debate", last_reason=f"{c.symbol}: {debate.reason}",
                            approved=int(debate.approve), vetoed=int(not debate.approve))
                p = self.portfolio()
                risk = self.risk.evaluate(sig, c.mint, strat.capital_pct, p, profile.get("max_pos_pct"))
                self._agent("risk", last_reason=f"{c.symbol}: {risk.reason}",
                            approved=int(risk.approve), vetoed=int(not risk.approve))
                chain = [v for v in (safety, strat_vote, research_vote, debate, risk) if v is not None]
                votes = _votes(*chain)
                signals = {**c.summary(), "venue": c.venue, **sig.indicators, "ref_price": sig.ref_price,
                           "stop_price": sig.stop_price, "tp_price": sig.tp_price}
                vetoer = next((v for v in chain if not v.approve), None)
                if vetoer:
                    stats["vetoes"] += 1
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             strategy=strat.name, signals=signals, votes=votes,
                                             reason=f"{vetoer.agent}: {vetoer.reason}")
                    continue
                order_id = f"c{self.cycle}-{strat.name}-{c.mint[:12]}-buy"
                try:
                    fill = self.executor.open(order_id, c, sig, risk.detail["size_usd"], risk.detail["fingerprint"])
                except Exception as e:
                    self.ledger.log_decision("error", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy=strat.name,
                                             signals=signals, votes=votes, reason=f"executor refused/failed: {e}")
                    continue
                self.reconcile()
                stats["entries"] += 1
                self._agent("executor", last_reason=f"bought {c.symbol} ${fill['usd_gross']:.2f}", approved=1)
                self.ledger.log_decision("entry", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                         strategy=strat.name, signals=signals, votes=votes, order_id=order_id,
                                         reason=f"{sig.reason}; size ${fill['usd_gross']:.2f} @ {fill['price']:.6g}")
                break  # one strategy per token per cycle


def bench_prices() -> dict:
    """Benchmark prices for buy-and-hold comparisons (Hyperliquid mids: one public call)."""
    mids = hl.info({"type": "allMids"})
    return {"SOL": float(mids["SOL"]), "BTC": float(mids["BTC"])}


def _votes(*votes: Vote) -> dict:
    return {v.agent: {"approve": v.approve, "reason": v.reason, **v.detail} for v in votes}


# ─── singleton + scheduler ───────────────────────────────────────────────────

_engine: PaperEngine | None = None
_scheduler: BackgroundScheduler | None = None


def get_engine() -> PaperEngine:
    global _engine
    if _engine is None:
        ledger = Ledger(cfg.PAPER_DB_PATH, cfg.PAPER_START_USDC)
        scouts, brokers = [ScoutAgent()], [PaperBroker(ledger)]
        candles = {"jupiter": md.pool_candles}
        if cfg.HL_ENABLED:
            scouts.append(hl.HyperliquidScout())
            brokers.append(hl.HyperliquidPaperBroker(ledger))
            candles["hyperliquid"] = hl.candles
        _engine = PaperEngine(ledger, scouts, SafetyAgent(), default_strategies(), DebateAgent(),
                              RiskAgent(), ExecutorAgent(*brokers), candles_fn=candles,
                              research=ResearchAgent(ledger), bench_fn=bench_prices)
    return _engine


def start():
    global _scheduler
    eng = get_engine()
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(eng.run_cycle, "interval", seconds=cfg.PAPER_CYCLE_SEC, id="paper_cycle",
                           max_instances=1, coalesce=True, next_run_time=datetime.now())
        _scheduler.start()
    else:
        _scheduler.resume()
    eng.ledger.set_state("running", True)
    return {"status": "started", "mode": "PAPER", "interval_sec": cfg.PAPER_CYCLE_SEC}


def stop():
    if _scheduler:
        _scheduler.pause()
    get_engine().ledger.set_state("running", False)
    return {"status": "stopped", "mode": "PAPER"}


def is_running() -> bool:
    return _scheduler is not None and _scheduler.state == 1 and get_engine().ledger.get_state("running", False)


def heartbeat_ok() -> bool:
    hb = get_engine().ledger.get_state("heartbeat")
    return bool(hb) and datetime.now(timezone.utc) - _ts(hb["ts"]) < timedelta(seconds=2.5 * cfg.PAPER_CYCLE_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Run paper-trading cycles (no real transactions).")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=0, help="seconds between cycles")
    args = ap.parse_args()
    eng = get_engine()
    for i in range(args.cycles):
        print(eng.run_cycle())
        if i < args.cycles - 1:
            time.sleep(args.sleep)
