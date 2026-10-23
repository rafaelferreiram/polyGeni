"""
24/7 paper loop: heartbeat -> reconcile ledger<->positions -> mark-to-market & exits
-> kill-switch check -> scout/safety/strategy/debate/risk/executor -> equity snapshot.
Used by the real account, its simulation and every lab bot (hl_live.py, arena.py).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from src.solana import config as cfg
from src.solana import learning
from src.solana import market_data as md
from src.solana.agents import DebateAgent, ExecutorAgent, Portfolio, RiskAgent, SafetyAgent, Vote
from src.solana.ledger import Ledger, now_iso
from src.solana import hyperliquid as hl

logger = logging.getLogger("solana.engine")


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def protected_stop(pos: dict, price: float) -> float | None:
    """Hold winners, never let them turn into losers:
    - quick win: up QUICK_WIN_PCT or more -> the stop locks QUICK_WIN_KEEP of the gain (at least entry + 0.3% fees);
    - halfway to the target -> the stop trails 1 ATR (target distance / 3) below the price.
    Returns the new, higher stop, or None. Only ever raises the stop. The exchange SL stays as the backstop."""
    stop, tp, qty = pos.get("stop_price"), pos.get("tp_price"), pos.get("qty") or 0
    if not (stop and tp and qty) or tp <= stop:
        return None
    entry = pos["cost_usd"] / qty
    if price < entry * (1 + cfg.QUICK_WIN_PCT) and price < entry + (tp - entry) / 2:
        return None
    new = max(entry * 1.003, entry + cfg.QUICK_WIN_KEEP * (price - entry))
    if price >= entry * (1 + cfg.QUICK_WIN2_PCT):           # second tier: bank most of a good move
        new = max(new, entry + cfg.QUICK_WIN2_KEEP * (price - entry))
    if price >= entry + (tp - entry) / 2:
        new = max(new, price - (tp - entry) / 3)
    return new if new > stop * 1.0005 else None


class PaperEngine:
    def __init__(self, ledger: Ledger, scout, safety: SafetyAgent, strategies: list,
                 debate: DebateAgent, risk: RiskAgent, executor: ExecutorAgent,
                 candles_fn=md.pool_candles, max_hold_h: float = cfg.RISK_MAX_HOLD_H, research=None,
                 bench_fn=None, profiles: dict | None = None, llm=None, coordinator=None):
        self.ledger, self.safety, self.strategies = ledger, safety, strategies
        self.scouts = scout if isinstance(scout, list) else [scout]
        self.debate, self.risk, self.executor = debate, risk, executor
        # one candle source for every venue, or {venue: fn}
        self.candles_fns = candles_fn if isinstance(candles_fn, dict) else None
        self.candles_fn, self.max_hold_h = candles_fn, max_hold_h
        self.bench_fn = bench_fn
        self.profiles = profiles if profiles is not None else cfg.MARKET_PROFILES
        self._busy = threading.Lock()   # scheduler and "scan now" must never run two cycles at once
        self.llm = llm                  # one LLMDecider, or a TEAM of bots sharing one wallet (see property)
        self.coordinator = coordinator  # (market, team) -> team ordered best-first for that market
        self.research = research
        self._safety_logged: dict[str, float] = {}

    @property
    def llm(self):
        return self.team[0] if self.team else None

    @llm.setter
    def llm(self, value):
        self.team = value if isinstance(value, list) else ([value] if value else [])

    # ─── decision mode: deterministic rules or LLM (Risk keeps the hard limits in both) ───
    def decision_mode(self) -> str:
        return self.ledger.get_state("decision_mode", "rules")

    def set_decision_mode(self, mode: str):
        if mode not in ("rules", "llm"):
            raise ValueError("mode must be 'rules' or 'llm'")
        if mode == "llm" and not any(d.enabled for d in self.team if getattr(d, "_rules", None) is None):
            raise ValueError("o modo LLM precisa de ANTHROPIC_API_KEY no .env")
        self.ledger.set_state("decision_mode", mode)
        self.ledger.log_decision("mode", cycle=self.cycle, reason=f"decision mode set to {mode}")

    def _deciders(self) -> list:
        return list(self.team) if self.decision_mode() == "llm" and self.team else self.strategies

    def _ordered(self, c) -> list:
        deciders = self._deciders()
        if self.coordinator and len(deciders) > 1:
            deciders = self.coordinator(c.symbol, deciders)
        return deciders

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
        if kw.get("last_reason"):
            from src.solana import logbuf
            quiet = "no signal" in kw["last_reason"] or kw["last_reason"].endswith(": passed")
            logbuf.add(logbuf.label(self.ledger.path), "DEBUG" if quiet else "INFO", f"{name}: {kw['last_reason']}")
        # live view: which agent is acting right now and on what
        self.ledger.set_state("live", {"agent": name, "ts": now_iso(), "detail": kw.get("last_reason", ""),
                                       "cycle": self.cycle, "running": True})
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

    def _btc_ok(self) -> bool:
        """BTC above its 50-hour average (closed 1h candles). Logged once when it flips; data errors don't block."""
        if not cfg.BTC_FILTER:
            return True
        from src.solana import hyperliquid as hl
        from src.solana.strategies import ema
        try:
            closes = [c["close"] for c in hl.candles("BTC")[:-1]]
            ok = len(closes) < 60 or closes[-1] > ema(closes[-150:], 50)
        except Exception:
            return True
        if self.ledger.get_state("btc_ok") != ok:
            self.ledger.set_state("btc_ok", ok)
            self.ledger.log_decision("btc_filter", cycle=self.cycle, reason=(
                "✅ filtro BTC: BTC voltou acima da média de 50h — compras liberadas" if ok else
                "⏸ filtro BTC: BTC abaixo da média de 50h — sem compras novas até ele voltar (vendas e stops seguem normais)"
                + (" · reserva do laboratório: segue operando para aprender" if self.__dict__.get("lab_model") else "")))
        return ok

    def _quota_levels(self) -> dict[str, int]:
        """Titulares behind their daily minimum of completed trades: 1 = one trade behind pro rata, 2 = two or more.
        The level is handed to the rule object (it loosens its entry a notch per level) and returned per strategy."""
        from src.solana import arena
        if not cfg.DAILY_MIN_TRADES:
            return {}
        now = datetime.now(cfg.TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        expected = cfg.DAILY_MIN_TRADES * (now.hour * 60 + now.minute) / 1440
        closed = [t for t in self.ledger.closed_trades() if t["exit_ts"] >= start]
        model = self.ledger.get_state("model")
        levels, prev = {}, self.ledger.get_state("quota_levels") or {}
        for strat in self._deciders():
            who = strat.name if strat.name in (arena._bots or {}) or strat.name in cfg.REAL_TRUSTED_BOTS else (model or strat.name)
            rule = getattr(strat, "_rules", None) or strat
            if not arena.is_titular(who):
                rule.relax = cfg.RESERVE_RELAX      # reserves train aggressively (looser entries: more trades to learn from)
                continue
            done = sum(t["strategy"] == strat.name for t in closed)
            behind = expected - done
            levels[strat.name] = 0 if behind < 1 else 1 if behind < 2 else 2
            # observational by default: the count is tracked and shown, but never loosens entries or shrinks targets
            # (forcing trades to hit a daily number is an incentive against profitability). QUOTA_RELAX_ENTRIES=1 = rollback.
            rule.relax = levels[strat.name] if cfg.QUOTA_RELAX_ENTRIES else 0
            if levels[strat.name] != prev.get(strat.name, 0):
                self.ledger.log_decision("quota", strategy=strat.name, reason=(
                    (f"⏱ {who}: cota do dia {done} de {cfg.DAILY_MIN_TRADES} operações completas — entrada um pouco mais solta (nível {levels[strat.name]}) e alvo mais perto"
                     if cfg.QUOTA_RELAX_ENTRIES else
                     f"⏱ {who}: {done} de {cfg.DAILY_MIN_TRADES} operações completas hoje (métrica observacional; as entradas não mudam)")
                    if levels[strat.name] else f"✅ {who}: cota do dia em dia ({done} de {cfg.DAILY_MIN_TRADES})"))
        self.ledger.set_state("quota_levels", levels)
        return levels

    def _redundant(self, c, corr_map: dict) -> str | None:
        """Economically redundant entry: its recent 1h returns correlate ≥ CORR_MAX_ENTRY with a same-direction
        position already open (two near-copies of one bet add exposure, not diversification). Opposite directions
        are hedges, never blocked. Uses candles the cycle already fetched — zero extra API calls."""
        if not cfg.CORR_MAX_ENTRY:
            return None
        mine = self.ledger.positions()
        if not mine:
            return None
        short = c.mint.endswith("~short")
        a = _hourly_returns(corr_map.get((c.symbol, short)) or [], cfg.CORR_WINDOW_H)
        if len(a) < 24:
            return None
        for p in mine:
            if p["mint"].endswith("~short") != short or p["symbol"] == c.symbol:
                continue
            b = _hourly_returns(corr_map.get((p["symbol"], short)) or [], cfg.CORR_WINDOW_H)
            n = min(len(a), len(b))
            if n < 24:
                continue
            r = _pearson(a[-n:], b[-n:])
            if r is not None and r >= cfg.CORR_MAX_ENTRY:
                return (f"exposição redundante: correlação {r:.2f} com {p['symbol']} já aberta "
                        f"(limite {cfg.CORR_MAX_ENTRY:.2f} em {cfg.CORR_WINDOW_H}h)")
        return None

    def _learn_ledger(self, strategy: str):
        """Where a bot's experience lives: its lab ledger (the accounts learn from the same bot's lab record)."""
        from src.solana import arena
        eng = (arena._bots or {}).get(strategy)
        return eng.ledger if eng else self.ledger

    # ─── exits ───
    def _hold_limit(self, pos: dict) -> float:
        """The max-hold in force WHEN THE POSITION WAS OPENED (stored at entry). A config change never closes old
        positions retroactively: on 2026-10-22 01:33 UTC a new 2h cap closed 5 open trades at once (−$0.74)."""
        stored = self.ledger.get_state(f"hold:{pos['entry_order_id']}")
        return stored if stored else _max_hold(pos["strategy"], self.max_hold_h, bool(self.__dict__.get("lab_model")))

    def _close(self, pos: dict, reason: str, extra_signals: dict | None = None) -> bool:
        try:
            fill = self.executor.close(f"exit-{pos['entry_order_id']}", pos, reason)
        except Exception as e:
            self.ledger.log_decision("error", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                     strategy=pos["strategy"], reason=f"exit failed ({reason}): {e}")
            return False
        pnl = fill["usd_gross"] - fill["fee_usd"] - pos["cost_usd"]
        self.ledger.log_decision("exit", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                 strategy=pos["strategy"], order_id=fill["order_id"], reason=reason,
                                 signals={**(extra_signals or {}), "pnl_usd": round(pnl, 4)},
                                 votes={"risk": {"approve": True, "reason": reason}})
        self.ledger.delete_position(pos["mint"])
        self.ledger.delete_state(f"hold:{pos['entry_order_id']}")
        return True

    def watch_exits(self) -> dict | None:
        """Fast lane (§6/§12.3): manage OPEN positions — marks, protected stops, stop/target/time exits — outside
        the full cycle, so protection never waits for (or pays for) analysis. Deterministic only: no LLM exit
        review, no entries. Skips silently when a cycle is already running (it does the same work)."""
        if not self.ledger.positions():
            return None
        if not self._busy.acquire(blocking=False):
            return {"busy": True}
        try:
            self.mark_and_exit(llm_review=False)
            self.reconcile()
            return {"watched": True}
        except Exception as e:
            self.ledger.log_decision("error", cycle=self.cycle, reason=f"exit watchdog: {e}")
            return {"error": str(e)[:200]}
        finally:
            self._busy.release()

    def mark_and_exit(self, llm_review: bool = True):
        now = datetime.now(timezone.utc)
        for pos in self.ledger.positions():
            held_h = (now - _ts(pos["opened_at"])).total_seconds() / 3600
            limit = self._hold_limit(pos)
            try:
                value = self.executor.mark(pos)
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                         reason=f"mark-to-market failed: {e}")
                if held_h >= limit:      # a time exit needs no fresh price: never let a 429 storm hold it past the cap
                    self._close(pos, f"max holding time {limit:g}h reached (cotação indisponível; fecho pelo tempo)",
                                {"held_h": round(held_h, 1)})
                continue
            price = value / pos["qty"]
            self.ledger.set_mark(pos["mint"], price, value)
            reason = None
            if cfg.PROTECT_PROFIT and (new := protected_stop(pos, price)):
                self.ledger.set_stop(pos["mint"], new)
                try:
                    self.executor.move_stop(pos, new)                # the exchange's own stop must follow the ledger's
                    where = " (na corretora também)" if hasattr(self.executor._b(pos["venue"]), "move_stop") else ""
                except Exception as e:
                    where = ""
                    self.ledger.log_decision("error", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"], strategy=pos["strategy"],
                                             reason=f"stop na corretora NÃO subiu (segue o antigo; o ledger vigia): {e}")
                self.ledger.log_decision("hold", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"],
                                         strategy=pos["strategy"], reason=f"lucro protegido: stop sobe para {new:.6g}{where}")
                pos = {**pos, "stop_price": new}
            if pos["stop_price"] and price <= pos["stop_price"]:
                reason = f"stop-loss hit ({price:.6g} <= {pos['stop_price']:.6g})"
            elif cfg.SCALP_TP_PCT and price >= pos["cost_usd"] / pos["qty"] * (1 + cfg.SCALP_TP_PCT):
                reason = f"💰 lucro curto: +{cfg.SCALP_TP_PCT:.1%} sobre o custo, já vende"
            elif pos["tp_price"] and price >= pos["tp_price"]:
                reason = f"take-profit hit ({price:.6g} >= {pos['tp_price']:.6g})"
            elif held_h >= cfg.STALE_EXIT_H and price < pos["cost_usd"] / pos["qty"] * (1 + cfg.STALE_EXIT_PCT):
                reason = f"sem tração: {held_h:.1f}h sem passar de +{cfg.STALE_EXIT_PCT:.1%} — fecha antes do tempo máximo"
            elif held_h >= limit:
                reason = f"max holding time {limit:g}h reached"
            elif llm_review and self.decision_mode() == "llm" and self.team and pos.get("pool_address"):
                # the bot that opened the position manages its exit
                owner = next((d for d in self.team if d.name == pos["strategy"]), self.llm)
                try:
                    fn = self.candles_fns.get(pos["venue"]) if self.candles_fns else self.candles_fn
                    reason = owner.review_exit(pos, fn(pos["pool_address"])[:-1], price) if owner.enabled else None
                except Exception as e:
                    logger.warning(f"LLM exit review failed: {e}")
            if reason:
                self._close(pos, reason, {"mark_price": price, "held_h": round(held_h, 1)})

    # ─── one full cycle ───
    def run_cycle(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"busy": True, "cycle": self.cycle}
        try:
            return self._run_cycle()
        finally:
            self._busy.release()

    def scanning(self) -> bool:
        return self._busy.locked()

    def _run_cycle(self) -> dict:
        cycle = self.cycle + 1
        self.ledger.set_state("cycle", cycle)
        self.ledger.set_state("heartbeat", {"ts": now_iso(), "cycle": cycle})
        stats = {"cycle": cycle, "candidates": 0, "safe": 0, "signals": 0, "entries": 0, "vetoes": 0}
        from src.solana import logbuf
        logbuf.add(logbuf.label(self.ledger.path), "INFO", f"── ciclo #{cycle} começou ──")
        try:
            sync_ok = True
            try:
                problems = self.executor.sync()        # live venues: exchange is the source of truth
                self.ledger.set_state("sync_failures", 0)
            except Exception as e:
                # a network error / rate limit is not a mismatch: skip new entries this cycle (exchange state unknown;
                # open positions keep their exchange stops) and only stop the bots if the exchange stays unreachable
                sync_ok, problems = False, []
                fails = self.ledger.get_state("sync_failures", 0) + 1
                self.ledger.set_state("sync_failures", fails)
                self.ledger.log_decision("error", cycle=cycle, reason=f"exchange unreachable ({fails}x): {str(e)[:160]}")
                if fails >= cfg.SYNC_FAIL_LIMIT:
                    problems = [f"exchange unreachable for {fails} cycles in a row: {str(e)[:120]}"]
            for msg in problems:
                self.ledger.log_decision("reconcile", cycle=cycle, reason=msg)
            if problems and not self.kill_switch().get("active"):
                self.trip_kill_switch(f"exchange/ledger mismatch: {problems[0]}")
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
            if self.kill_switch().get("active"):
                stats["skipped"] = "kill switch active"
            elif not sync_ok:
                stats["skipped"] = "exchange unreachable this cycle"
            elif self.rests_on_goal and cfg.DAILY_GOAL_REST and self._goal_hit(p):
                stats["skipped"] = (f"meta do dia batida (+{(p.equity / p.day_start_equity - 1) * 100:.1f}%) — "
                                    "descansando até amanhã; posições abertas seguem protegidas")
            elif cfg.BOT_SELF_PAUSE and not self.coffee() and self._sleeping(stats):
                pass
            else:
                self._entries(stats)
                if cfg.BOT_SELF_PAUSE and not self.coffee():
                    self._maybe_sleep(stats)
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
            self.ledger.set_state("live", {"agent": None, "ts": now_iso(), "detail": "cycle finished",
                                           "cycle": cycle, "running": False})
            logger.info(f"[paper] cycle {cycle}: {stats}")
        return stats

    def manual_exit(self, mint: str) -> dict:
        """The user closes an open position from the dashboard (market sell; on the real account the exchange's
        stop/target orders are cancelled by the broker). Recorded like any exit."""
        self._yield = True                                  # a running cycle stops at the next coin
        try:
            if not self._busy.acquire(timeout=120):         # never race a running cycle
                raise ValueError("os bots estão no meio de um ciclo — tente de novo em instantes")
        finally:
            self._yield = False
        try:
            pos = next((p for p in self.ledger.positions() if p["mint"] == mint), None)
            if pos is None:
                raise ValueError("essa posição já não está aberta")
            reason = "fechada manualmente pelo painel"
            fill = self.executor.close(f"exit-{pos['entry_order_id']}", pos, reason)
            pnl = fill["usd_gross"] - fill["fee_usd"] - pos["cost_usd"]
            self.ledger.log_decision("exit", cycle=self.cycle, mint=pos["mint"], symbol=pos["symbol"], strategy=pos["strategy"],
                                     order_id=fill["order_id"], reason=reason, signals={"pnl_usd": round(pnl, 4)})
            self.ledger.delete_position(pos["mint"])
            self.ledger.delete_state(f"hold:{pos['entry_order_id']}")
            self.reconcile()
        finally:
            self._busy.release()
        return {"symbol": pos["symbol"], "usd": round(fill["usd_gross"], 4), "price": fill["price"], "pnl_usd": round(pnl, 4)}

    def manual_entry(self, symbol: str, usd: float) -> dict:
        """A buy the user clicks on the dashboard: same risk limits, exchange stop (2 ATR) / target (3 ATR) and ledger
        as the bots' trades, so the exchange and the books never disagree (a buy made outside would trip the kill switch)."""
        from src.solana.strategies import Signal, _atr_stops
        if self.kill_switch().get("active"):
            raise ValueError("kill switch ativo — rearme primeiro")
        c = next((x for s in self.scouts for x in s.candidates() if x.symbol == symbol), None)
        if c is None:
            raise ValueError(f"{symbol} não está entre os mercados dos bots")
        candles = self._candles(c)[:-1]
        stop, tp, a = _atr_stops(candles, 2.0, 3.0)
        sig = Signal("manual", c.price_usd, stop, tp, f"compra manual pelo painel (até ${usd:.2f})", {"atr": a})
        self._yield = True                                  # a running cycle stops at the next coin
        try:
            if not self._busy.acquire(timeout=120):         # never race a running cycle
                raise ValueError("os bots estão no meio de um ciclo — tente de novo em instantes")
        finally:
            self._yield = False
        try:
            risk = self.risk.evaluate(sig, c.mint, 1.0, self.portfolio(), self.profiles.get(c.venue, {}).get("max_pos_pct"),
                                      max_usd=usd)
            if not risk.approve:
                raise ValueError(f"o Risk recusou: {risk.reason}")
            order_id = f"manual-{int(time.time())}-{c.mint[:12]}-buy"
            fill = self.executor.open(order_id, c, sig, risk.detail["size_usd"], risk.detail["fingerprint"])
            self.reconcile()
            self.ledger.log_decision("entry", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy="manual",
                                     order_id=order_id, reason=f"{sig.reason}; size ${fill['usd_gross']:.2f} @ {fill['price']:.6g}")
        finally:
            self._busy.release()
        return {"symbol": c.symbol, "usd": round(fill["usd_gross"], 2), "price": fill["price"],
                "stop": sig.stop_price, "tp": sig.tp_price}

    rests_on_goal = True        # lab bots set False: they keep trading to keep measuring
    size_policy = None          # strategy name -> max position USD (the bots' reward/penalty), set for team wallets

    def _goal_hit(self, p) -> bool:
        return p.day_start_equity > 0 and (p.equity - p.day_start_equity) / p.day_start_equity >= cfg.DAILY_GOAL_PCT

    # ─── coffee: the user keeps the bots awake (no self-pause) for a while; entry rules are unchanged ───
    def coffee(self) -> str | None:
        until = self.ledger.get_state("coffee_until")
        return until if until and datetime.now(timezone.utc) < _ts(until) else None

    def give_coffee(self, hours: float) -> str:
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.ledger.set_state("coffee_until", until)
        self.ledger.set_state("sleep", {})
        self.ledger.set_state("idle_streak", 0)
        self.ledger.log_decision("coffee", cycle=self.cycle, reason=f"☕ café: acordados e atentos até {_ts(until).astimezone(cfg.TZ).strftime('%H:%M')}")
        return until

    # ─── self-pause: nothing to do → sleep; price moves or open positions → wake ───
    def _sleeping(self, stats: dict) -> bool:
        sleep = self.ledger.get_state("sleep") or {}
        if not sleep.get("until") or self.ledger.positions():
            return False
        if datetime.now(timezone.utc) >= _ts(sleep["until"]):
            self.ledger.set_state("sleep", {})
            return False
        try:
            prices = {c.mint: c.price_usd for s in self.scouts for c in s.candidates()}
        except Exception:
            prices = {}
        for mint, px in prices.items():
            ref = (sleep.get("prices") or {}).get(mint)
            if ref and px and abs(px / ref - 1) >= cfg.BOT_WAKE_MOVE_PCT:
                self.ledger.set_state("sleep", {})
                self.ledger.log_decision("wake", cycle=self.cycle, mint=mint, reason=f"acordei: o preço mexeu {px / ref - 1:+.1%}")
                return False
        stats["sleeping_until"] = sleep["until"]
        self._agent("scout", last_reason=f"dormindo até {_ts(sleep['until']).astimezone(cfg.TZ).strftime('%H:%M')} — {sleep.get('reason', '')}"[:200])
        return True

    def _maybe_sleep(self, stats: dict):
        if stats.get("entries") or self.ledger.positions() or stats.get("error"):
            self.ledger.set_state("idle_streak", 0)
            return
        minutes, reason = None, ""
        if self.decision_mode() == "llm" and self.team:
            sugg = [d for t in self.team for d in t.last.values() if d.get("action") == "HOLD" and d.get("next_check_min")]
            if sugg:
                pick = min(sugg, key=lambda d: d["next_check_min"])
                minutes = max(15, min(float(pick["next_check_min"]), cfg.BOT_MAX_SLEEP_MIN))
                reason = f"LLM: nada a fazer — {pick.get('reason', '')}"[:160]
        if minutes is None and not stats.get("signals"):
            streak = self.ledger.get_state("idle_streak", 0) + 1
            self.ledger.set_state("idle_streak", streak)
            if streak >= 2:
                minutes = min(15 * 2 ** (streak - 2), cfg.BOT_MAX_SLEEP_MIN)
                reason = f"mercado sem sinais há {streak} ciclos"
        if minutes:
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            self.ledger.set_state("sleep", {"until": until.isoformat(timespec="seconds"), "reason": reason,
                                            "prices": getattr(self, "_last_prices", {})})
            self.ledger.log_decision("sleep", cycle=self.cycle, reason=f"dormindo {minutes:.0f} min: {reason}")
            stats["sleeping_until"] = until.isoformat(timespec="seconds")

    def _entries(self, stats: dict):
        candidates = []
        for scout in self.scouts:
            try:
                candidates += scout.candidates()
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, reason=f"scout {type(scout).__name__} failed: {e}")
        stats["candidates"] = len(candidates)
        self._last_prices = {c.mint: c.price_usd for c in candidates}
        self.ledger.set_state("markets", [{"id": c.mint, "symbol": c.symbol, "venue": c.venue,
                                           "price": c.price_usd} for c in candidates])
        self._agent("scout", last_reason=f"{len(candidates)} candidates", approved=len(candidates))
        paused = set(self.paused())
        ready = []
        for c in candidates:
            if self.__dict__.get("_yield"):          # the user is buying/selling by hand: finish the cycle now
                break
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
                if candles and "funding_1h" in (c.price_change or {}):
                    candles[-1] = {**candles[-1], "funding": c.price_change["funding_1h"]}
                age_h = (time.time() - candles[-1]["t"]) / 3600 if candles else None
                if age_h is None or age_h > cfg.STALE_CANDLE_H:
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             reason=f"risk: stale or missing price data ({age_h if age_h is None else round(age_h, 1)}h old)")
                    continue
            except Exception as e:
                self.ledger.log_decision("error", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                         reason=f"candles unavailable: {e}")
                continue
            ready.append((c, candles, safety, profile))
        # best opportunity first: with one slot, the deepest dip inside an uptrend gets it, not the first coin listed
        ready.sort(key=lambda r: (r[0].symbol.upper().removeprefix("K") not in cfg.FOCUS_COINS, _setup_rank(r[1])))   # focus coins first
        mt = _market_temp(ready)                 # the radar and the market thermometer still see every coin
        self.ledger.set_state("market_temp", mt)
        lab_reserve = bool(self.__dict__.get("lab_model"))
        if lab_reserve:
            from src.solana import arena as _arena
            lab_reserve = not _arena.is_titular(self.__dict__["lab_model"])
        if (uni := universe(ready)) and not lab_reserve:     # titulars and the real account scan the hour's most liquid and volatile coins; reserves scan all 30 to learn faster
            held_syms = {x["symbol"] for x in self.ledger.positions()}
            ready = [r for r in ready if r[0].symbol in uni or r[0].symbol in held_syms
                     or r[0].symbol.upper().removeprefix("K") in cfg.FOCUS_COINS]
        breadth = {k: mt.get(k) for k in ("n", "uptrend", "avg_rsi", "avg_ch24")}   # whole-market context for the AI
        from src.solana import trainer
        trainer.set_breadth(breadth)
        from src.solana import regime as regime_mod
        reg = regime_mod.current(breadth)             # observational: logged, shown, tagged — gates nothing yet
        if reg:
            prev_reg = self.ledger.get_state("regime") or {}
            if (prev_reg.get("state"), prev_reg.get("vol_state")) != (reg["state"], reg["vol_state"]):
                self.ledger.log_decision("regime", cycle=self.cycle, signals=reg["detail"], reason=(
                    f"🌡 regime do mercado: {reg['state']} · volatilidade em {reg['vol_state']} "
                    f"(confiança {reg['confidence']}; observacional — o filtro BTC-50h segue mandando)"))
            self.ledger.set_state("regime", reg)
        corr_map = {(r[0].symbol, r[0].mint.endswith("~short")): r[1] for r in ready}
        held = {x["mint"] for x in self.ledger.positions()}
        quota = self._quota_levels()
        btc_ok = self._btc_ok()                      # BTC below its 50h average: no new longs (shorts are for that)
        lab_bot = self.__dict__.get("lab_model")
        if lab_bot and not btc_ok:                   # a reserve in the lab must trade to learn; only titulars keep the filter
            from src.solana import arena
            btc_ok = not arena.is_titular(lab_bot)
        per_bot = {}
        if not self.__dict__.get("lab_model") and cfg.BURST_MAX_ENTRIES:
            since = (datetime.now(timezone.utc) - timedelta(minutes=cfg.BURST_WINDOW_MIN)).isoformat()
            if sum(f["side"] == "buy" and f["ts"] >= since for f in self.ledger.fills(50)) >= cfg.BURST_MAX_ENTRIES:
                ready = []                         # enough new buys in this window: wait, don't chase one move
        for x in self.ledger.positions():
            per_bot[x["strategy"]] = per_bot.get(x["strategy"], 0) + 1
        from src.solana import meme
        guards = {s.name: meme.stop_guard(self.ledger, s.name, s.name) for s in self._deciders()}   # 3 stops in 4h: pause 6h
        for c, candles, safety, profile in ready:
            if self.__dict__.get("_yield"):
                break
            if c.mint in held:          # already holding it: its stop/target and exit review handle it — no AI tokens
                continue
            if why := self._redundant(c, corr_map):   # correlated with an open position: no real diversification
                self._agent("risk", last_reason=f"{c.symbol}: {why}")
                if time.time() - self._safety_logged.get(f"corr:{c.mint}", 0) > 3600:   # don't flood the log
                    self._safety_logged[f"corr:{c.mint}"] = time.time()
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             reason=f"risk: {why}")
                continue
            for strat in self._ordered(c):
                from src.solana.strategies import SHORT_BOTS
                is_short = c.mint.endswith("~short")
                if is_short != ((getattr(strat, "model", None) or strat.name) in SHORT_BOTS):     # short bots see shorts, the others longs
                    continue
                gate = getattr(self, "short_gate", None)                                          # the real account's short rules
                if is_short and gate and (why := gate(btc_ok)):
                    self._agent(strat.name, last_reason=f"{c.symbol} short: {why}")
                    continue
                fast_real = bool(getattr(self, "fast_real", False))                                # the real engine, fast bots allowed
                from src.solana.strategies import is_fast
                if fast_real and not is_short and is_fast(getattr(strat, "model", None) or strat.name):
                    if sum(1 for x in self.ledger.positions() if is_fast(x["strategy"])) >= cfg.LIVE_FAST_MAX_OPEN:
                        self._agent(strat.name, last_reason=f"{c.symbol}: já há {cfg.LIVE_FAST_MAX_OPEN} posição de bot de 15 min aberta (limite)")
                        continue
                    pass_filter = True                                                              # 15m bots trade through a weak BTC
                else:
                    pass_filter = btc_ok
                if not pass_filter and not is_short:
                    continue
                if guards.get(strat.name):
                    self._agent(strat.name, last_reason=f"🛡 guarda de stops: {guards[strat.name]}")
                    continue
                if not self.__dict__.get("lab_model") and per_bot.get(strat.name, 0) >= cfg.MAX_POSITIONS_PER_BOT:
                    self._agent(strat.name, last_reason=f"{c.symbol}: já tem {cfg.MAX_POSITIONS_PER_BOT} posições abertas")
                    continue
                if hasattr(strat, "decide"):
                    if not strat.enabled:
                        self._agent(strat.name, last_reason="sem ANTHROPIC_API_KEY — este bot não decide", vetoed=1)
                        continue
                    from src.solana import news, toptraders, whales
                    sig = strat.decide(candles, {**c.summary(), "symbol": c.symbol, "venue": c.venue,
                                                 "whale_flow": whales.summary(c.symbol), "market_breadth": breadth,
                                                 "news_12h": news.for_llm(c.symbol),
                                                 "top_traders": toptraders.consensus(c.symbol)})
                    d = strat.last.get(c.symbol, {})
                    msg = f"{c.symbol}: {d.get('action', 'HOLD')} — {d.get('reason', '')}"[:200]
                    self._agent(strat.name, last_reason=msg, approved=int(sig is not None))
                    self._agent("llm", last_reason=f"{strat.name} → {msg}"[:200], approved=int(sig is not None))
                else:
                    sig = strat.signal(candles)
                if sig is not None:                      # stop sized by this coin's ATR, within the strategy's bounds
                    from src.solana.strategies import atr_stop
                    sig.stop_pct = atr_stop(candles, sig.stop_pct)
                    if is_short and gate:                # real-account short: the stop never exceeds LIVE_SHORT_MAX_STOP_PCT
                        sig.stop_pct = min(sig.stop_pct, cfg.LIVE_SHORT_MAX_STOP_PCT)
                if sig is not None and profile.get("stop_mult", 1) != 1:
                    sig.stop_pct = min(sig.stop_pct * profile["stop_mult"], 0.5)
                    sig.tp_pct *= profile["stop_mult"]
                lab_model = self.__dict__.get("lab_model")
                if sig is not None and lab_model and _fast_family(lab_model):   # scalp clamps: 15m family only —
                    if cfg.LAB_SCALP_STOP_PCT:         # a 0.6% stop makes no sense on a 24h trend hold (§5.5)
                        sig.stop_pct = min(sig.stop_pct, cfg.LAB_SCALP_STOP_PCT)
                    if cfg.LAB_SCALP_TP_PCT:
                        sig.tp_pct = min(sig.tp_pct, cfg.LAB_SCALP_TP_PCT)
                if sig is None and lab_model:          # an opportunity a teammate passed to this bot
                    from src.solana import arena
                    sig = arena.take_handoff(lab_model, c.symbol, strat.name)
                if sig is None:
                    self._agent(strat.name, last_reason=f"{c.symbol}: no signal")
                    continue
                # one trade per signal: after an exit, the same candle's signal (and its now-stale target) is spent.
                # Persisted in the ledger so a restart never repeats the same candle's entry (incident class: 2026-10-21).
                entered = self.__dict__.setdefault(
                    "_entered", {tuple(k) for k in self.ledger.get_state("entered", [])})
                if (c.mint, strat.name, candles[-1]["t"]) in entered:
                    self._agent(strat.name, last_reason=f"{c.symbol}: sinal já usado neste candle")
                    continue
                if cfg.QUOTA_RELAX_ENTRIES and quota.get(strat.name):   # rollback mode only: closer target when behind
                    sig.tp_pct *= 0.6
                    sig.reason = f"{sig.reason} · cota do dia (nível {quota[strat.name]})"
                tags = learning.situation(candles, c.symbol)
                if reg and reg.get("state") not in (None, "sem dados"):
                    tags = tags + [f"regime {reg['state']}"]      # per-regime record: the evidence a future gate needs
                if cfg.SELF_LEARNING:
                    rec = learning.record(self._learn_ledger(strat.name), strat.name)
                    from src.solana import evolution
                    team = learning.team_record()
                    avoided = learning.avoid(rec, tags, evolution.inherited_avoid(strat.name))
                    shared = [t for t in learning.team_avoid(rec, team, tags) if t not in avoided]
                    if avoided or shared:
                        why = ", ".join([f"'{t}' ({rec[t]['wins']}/{rec[t]['n']} ganhos, ${rec[t]['pnl']:+.2f})" if t in rec
                                         else f"'{t}' (lição herdada)" for t in avoided] +
                                        [f"'{t}' (rede do time: {team[t]['wins']}/{team[t]['n']} ganhos)" for t in shared])
                        self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy=strat.name,
                                                 signals={"tags": tags}, reason=f"aprendizado: evita {why}")
                        stats["vetoes"] += 1
                        continue
                if lab_model and cfg.SELF_LEARNING and not getattr(sig, "handed", False):
                    from src.solana import arena
                    if mate := learning.better_teammate(lab_model, tags):
                        arena.post_handoff(mate[0], c.symbol, sig, lab_model, mate[1])
                        self.ledger.log_decision("handoff", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy=strat.name,
                                                 signals={"tags": tags, "to": mate[0]},
                                                 reason=f"📨 passou {c.symbol} para {mate[0]}: {mate[1]}")
                        continue
                est = None
                if cfg.EDGE_CHECK:                      # margin of error: odds of this trade from every memory we have
                    from src.solana import arena, edge as edge_mod
                    mine = self._learn_ledger(strat.name)
                    pay = edge_mod.payoff(mine.closed_trades(), edge_mod.payoff(
                        [t for e in list((arena._bots or {}).values()) for t in e.ledger.closed_trades()]))
                    done = mine.closed_trades()
                    base = sum(t["pnl_usd"] > 0 for t in done) / len(done) if len(done) >= 10 else None
                    est = edge_mod.estimate(tags, learning.record(mine, strat.name), learning.team_record(), *pay, baseline=base)
                    if est["skip"]:
                        self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy=strat.name,
                                                 signals={"tags": tags, "edge": est},
                                                 reason=f"margem de erro: {edge_mod.describe(est)} — nem no melhor caso compensa")
                        stats["vetoes"] += 1
                        continue
                    sig.reason = f"{sig.reason} · {edge_mod.describe(est)}"
                    sig.indicators = {**(sig.indicators or {}), "edge": est}
                from src.solana import contract
                intent = contract.build(c, sig, strat.name, candles,
                                        _max_hold(strat.name, self.max_hold_h, bool(lab_model)),
                                        reg, {**(est or {}), "tags": tags} if est else {"tags": tags}, self.cycle)
                try:
                    intent.validate()                   # semantics first: Risk never sees a malformed intention
                except ValueError as e:
                    stats["vetoes"] += 1
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             strategy=strat.name, signals={"intent": intent.as_dict()},
                                             reason=f"contrato do sinal: {e}")
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
                key = (c.mint, strat.name, candles[-1]["t"])     # one review per signal per candle, not every 5 min
                debated = self.__dict__.setdefault("_debated", {})
                debate = (Vote("debate", True, "skipped: the LLM already made this decision")
                          if strat.name.startswith("llm") else debated.get(key) or
                          debated.setdefault(key, self.debate.review(c, sig, {"closes_24h": closes, "safety": safety.detail,
                                                                                    "research_brief": brief})))
                self._agent("debate", last_reason=f"{c.symbol}: {debate.reason}",
                            approved=int(debate.approve), vetoed=int(not debate.approve))
                p = self.portfolio()
                risk = self.risk.evaluate(sig, c.mint, strat.capital_pct, p, profile.get("max_pos_pct"),
                                          entry_cap_mult=2 if self.coffee() else 1,
                                          max_usd=self.size_policy(strat.name) if self.size_policy else None)
                self._agent("risk", last_reason=f"{c.symbol}: {risk.reason}",
                            approved=int(risk.approve), vetoed=int(not risk.approve))
                chain = [v for v in (safety, strat_vote, research_vote, debate, risk) if v is not None]
                votes = _votes(*chain)
                signals = {**c.summary(), "venue": c.venue, **sig.indicators, "ref_price": sig.ref_price,
                           "stop_price": sig.stop_price, "tp_price": sig.tp_price, "tags": tags}
                vetoer = next((v for v in chain if not v.approve), None)
                if vetoer:
                    stats["vetoes"] += 1
                    self.ledger.log_decision("veto", cycle=self.cycle, mint=c.mint, symbol=c.symbol,
                                             strategy=strat.name, signals=signals, votes=votes,
                                             reason=f"{vetoer.agent}: {vetoer.reason}")
                    continue
                order_id = f"c{self.cycle}-{strat.name}-{c.mint[:12]}-buy"
                intent.decision_id = order_id            # the approved sizing completes the contract
                intent.risk_budget_request_usd = round(risk.detail["size_usd"], 2)
                intent.estimated_loss_at_stop_usd = round(risk.detail["size_usd"] * sig.stop_pct, 4)
                signals["intent"] = intent.as_dict()
                try:
                    fill = self.executor.open(order_id, c, sig, risk.detail["size_usd"], risk.detail["fingerprint"])
                except Exception as e:
                    self.ledger.log_decision("error", cycle=self.cycle, mint=c.mint, symbol=c.symbol, strategy=strat.name,
                                             signals=signals, votes=votes, reason=f"executor refused/failed: {e}")
                    continue
                entered.add((c.mint, strat.name, candles[-1]["t"]))
                recent = sorted(entered, key=lambda k: k[2])[-100:]           # the last few candles are enough
                self.ledger.set_state("entered", [list(k) for k in recent])
                # the hold cap in force NOW is this trade's contract: a later config change never closes it retroactively
                self.ledger.set_state(f"hold:{order_id}", _max_hold(strat.name, self.max_hold_h, bool(lab_model)))
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


def _fast_family(name: str) -> bool:
    """15-minute twin (or a successor of one): the scalp side of the club."""
    from src.solana import evolution
    from src.solana.strategies import FAST_BOTS
    return evolution.base_of(name.removeprefix("llm:")) in FAST_BOTS


def _max_hold(strategy: str, default: float, lab: bool = False) -> float:
    """Each family trades its own horizon (§5.5 holding policy — short AND long trades, by evidence):
    - accounts (real money): the owner's MAX_HOLD_CAP_H (2h) stays the hard cap until the lab proves longer holds;
    - lab, fast family (15m twins): the scalp clock (LAB_SCALP_HOLD_H) — the short-trade experiment;
    - lab, slower families: their strategy's own horizon (trend 24h, trend4h 48h…) up to LAB_HOLD_CAP_H — the
      evidence longer holds need before the real cap can ever move. Paper money only."""
    from src.solana import arena
    from src.solana.strategies import RULE_BOTS
    own = getattr(RULE_BOTS.get(strategy), "max_hold_h", None) or default
    if lab:
        if _fast_family(strategy) and cfg.LAB_SCALP_HOLD_H:
            hold = min(own, cfg.LAB_SCALP_HOLD_H)
        else:
            hold = min(own, cfg.LAB_HOLD_CAP_H)
    else:
        hold = min(own, cfg.MAX_HOLD_CAP_H)               # no buy-and-hold with real money
    fast = arena.fast_exit_hold(strategy)
    return min(hold, fast) if fast else hold


def _market_temp(ready: list) -> dict:
    """Market thermometer from the candles the bots already fetched: trend breadth, RSI, 24h moves, near-signal coins."""
    from statistics import mean, pstdev
    from src.solana.strategies import MeanReversionATR, TrendStrong, ema, rsi
    rows = []
    for c, candles, *_ in ready:
        closes = [k["close"] for k in candles]
        if len(closes) < 30:
            continue
        px = closes[-1]
        trend = ema(closes[-600:], 200) if len(closes) >= 200 else None
        e50 = ema(closes[-150:], 50)
        slope = e50 / ema(closes[-174:-24], 50) - 1 if len(closes) >= 174 else 0
        lower = mean(closes[-20:]) - 2 * pstdev(closes[-20:])          # dip bot buys below this (with RSI < 30)
        high24 = max(k.get("high", k["close"]) for k in candles[-25:-1])  # trend bot buys above this
        up = bool(trend and px > trend)
        strong = bool(up and e50 > trend and slope >= TrendStrong.min_slope)
        ready_now = "quedas" if MeanReversionATR().signal(candles) else "tendência" if TrendStrong().signal(candles) else None
        dip, brk = lower / px - 1, high24 / px - 1
        status = ("pronto" if ready_now else "sem tendência" if not up
                  else "perto" if dip > -0.015 or (strong and brk < 0.01) else "aguardando")
        rows.append({"symbol": c.symbol, "price": px, "up": up, "strong": strong, "rsi": round(rsi(closes), 1),
                     "ch24": round((px / closes[-25] - 1) * 100, 2), "dip_at": lower, "dip_pct": round(dip * 100, 2),
                     "break_at": high24, "break_pct": round(brk * 100, 2), "status": status, "ready_by": ready_now})
    if not rows:
        return {}
    by = sorted(rows, key=lambda r: r["ch24"])
    return {"ts": now_iso(), "n": len(rows), "uptrend": sum(r["up"] for r in rows),
            "avg_rsi": round(sum(r["rsi"] for r in rows) / len(rows), 1),
            "avg_ch24": round(sum(r["ch24"] for r in rows) / len(rows), 2),
            "near": [r["symbol"] for r in rows if r["up"] and r["rsi"] < 40],
            "best": [[r["symbol"], r["ch24"]] for r in by[-3:][::-1]], "worst": [[r["symbol"], r["ch24"]] for r in by[:3]],
            # the radar: closest to a buy first
            "watch": sorted(rows, key=lambda r: (["pronto", "perto", "aguardando", "sem tendência"].index(r["status"]),
                                                 min(-r["dip_pct"], r["break_pct"] if r["strong"] else 99)))}


_universe: dict = {}      # the hour's active universe, shared by every engine (they all scan the same coins)


def universe(ready: list) -> list[str]:
    """The UNIVERSE_SIZE coins with the most 24h volume × 1h log-range volatility (mean ln(high/low) of the last 24
    closed bars): where a day trade can both move and be filled. Computed once per hour per market list (candles only
    change hourly); the engine adds FOCUS_COINS and held coins on top. [] = filter off."""
    import math
    if not cfg.UNIVERSE_SIZE:
        return []
    key = (int(time.time()) // 3600, tuple(sorted(r[0].symbol for r in ready)))
    if _universe.get("key") != key:
        scores = {}
        for c, candles, *_ in ready:
            bars = [k for k in candles[-24:] if k.get("high") and k.get("low")]
            vol = sum(math.log(k["high"] / k["low"]) for k in bars) / len(bars) if bars else 0.0
            scores[c.symbol] = c.volume_h24 * vol
        coins = sorted(scores, key=lambda s: -scores[s])[:cfg.UNIVERSE_SIZE]
        _universe.update(key=key, ts=now_iso(), coins=coins, size=cfg.UNIVERSE_SIZE, focus=list(cfg.FOCUS_COINS),
                         scores={s: round(v) for s, v in scores.items()})
    return _universe["coins"]


def current_universe() -> dict:
    return {k: v for k, v in _universe.items() if k != "key"}


def _setup_rank(candles: list[dict]) -> tuple:
    """Sort key: markets in an uptrend first, then the most oversold (lowest RSI). Cheap, no extra API calls."""
    from src.solana.strategies import ema, rsi
    closes = [k["close"] for k in candles]
    if len(closes) < 15:
        return (1, 100.0)
    trend = ema(closes[-600:], 200) if len(closes) >= 200 else None
    return (0 if trend and closes[-1] > trend else 1, rsi(closes))


def _hourly_returns(candles: list[dict], window: int) -> list[float]:
    closes = [k["close"] for k in candles[-(window + 1):]]
    return [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1) if closes[i]]


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 2 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return None                      # a flat series correlates with nothing meaningfully
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / (va * vb) ** 0.5


def _votes(*votes: Vote) -> dict:
    return {v.agent: {"approve": v.approve, "reason": v.reason, **v.detail} for v in votes}


# ─── singleton + scheduler ───────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def scheduler() -> BackgroundScheduler:
    """One background scheduler shared by the paper loop and the live loop (separate jobs)."""
    global _scheduler
    if _scheduler is None:
        # many jobs share the pool (account cycles, lab round, turbo ticks): a job that starts late still runs
        _scheduler = BackgroundScheduler(executors={"default": {"type": "threadpool", "max_workers": 20}},
                                         job_defaults={"misfire_grace_time": 300, "coalesce": True})
        _scheduler.start()
    return _scheduler
