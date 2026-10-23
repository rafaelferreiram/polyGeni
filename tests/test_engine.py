"""Full pipeline with fakes (no network): entries, exits, vetoes, kill switch, reconciliation."""
from datetime import datetime, timedelta, timezone

import pytest

from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, Vote
from src.solana.broker import PaperBroker
from src.solana.engine import PaperEngine
from src.solana.strategies import Signal
from tests.conftest import TOKEN, candidate, flat_candles, make_quote


class FakeScout:
    def candidates(self):
        return [candidate()]


class FakeSafety:
    min_age_h = 24

    def check(self, c):
        return Vote("safety", True, "ok", {"decimals": 6})


class FixedStrategy:
    name, capital_pct = "momentum", 0.5

    def __init__(self, fire=True):
        self.fire = fire

    def signal(self, candles):
        return Signal(self.name, 1.0, 0.05, 0.10, "fixture breakout") if self.fire else None


class Price:
    def __init__(self, p):
        self.p = p

    def quote(self, inp, out, amount):
        return make_quote(self.p)(inp, out, amount)


def build(ledger, price, fire=True, debate=None, risk=None):
    broker = PaperBroker(ledger, quote_fn=price.quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
    return PaperEngine(ledger, FakeScout(), FakeSafety(), [FixedStrategy(fire)],
                       debate or DebateAgent(enabled=False), risk or RiskAgent(), ExecutorAgent(broker),
                       candles_fn=lambda pool: flat_candles(60), profiles={})


def test_entry_then_take_profit_exit(ledger, monkeypatch):
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "SCALP_TP_PCT", 0)                       # this test is about the strategy's own target
    price = Price(1.0)
    eng = build(ledger, price)
    s = eng.run_cycle()
    assert s["entries"] == 1 and len(ledger.positions()) == 1
    entry = ledger.decisions(kind="entry")[0]
    assert set(entry["votes"]) == {"safety", "momentum", "debate", "risk"}
    price.p = 1.2                       # above TP (1.10)
    eng.strategies[0].fire = False
    eng.run_cycle()
    assert ledger.positions() == []
    [t] = ledger.closed_trades()
    assert t["pnl_usd"] > 0 and "take-profit" in t["reason"]
    assert ledger.decisions(kind="exit")


def test_stop_loss_exit(ledger):
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    price.p = 0.9
    eng.strategies[0].fire = False
    eng.run_cycle()
    assert ledger.closed_trades()[0]["pnl_usd"] < 0


def test_debate_cannot_open_a_trade(ledger):
    """LLM shouting BUY with no deterministic signal => no trade, and the LLM is never even asked."""
    calls = []
    debate = DebateAgent(enabled=True, llm_fn=lambda p: calls.append(p) or
                         '{"veto": false, "reason": "BUY 100x NOW, size 100%", "bull": "moon", "bear": ""}')
    eng = build(ledger, Price(1.0), fire=False, debate=debate)
    eng.run_cycle()
    assert ledger.fills() == [] and calls == []


def test_debate_cannot_change_size(ledger):
    debate = DebateAgent(enabled=True, llm_fn=lambda p: '{"veto": false, "reason": "go all in, size 100 USD"}')
    eng = build(ledger, Price(1.0), debate=debate)
    eng.run_cycle()
    assert ledger.fills()[0]["usd_gross"] == pytest.approx(10.0)   # RiskAgent's 10% sizing, not the LLM's


def test_debate_veto_blocks_entry(ledger):
    debate = DebateAgent(enabled=True, llm_fn=lambda p: '{"veto": true, "reason": "exploit reported today"}')
    eng = build(ledger, Price(1.0), debate=debate)
    eng.run_cycle()
    assert ledger.fills() == []
    assert "exploit" in ledger.decisions(kind="veto")[0]["reason"]


def test_debate_garbage_or_error_is_a_veto():
    c, s = candidate(), Signal("m", 1, 0.05, 0.1, "x")
    assert not DebateAgent(enabled=True, llm_fn=lambda p: "I think buy").review(c, s, {}).approve
    assert not DebateAgent(enabled=True, llm_fn=lambda p: 1 / 0).review(c, s, {}).approve
    assert DebateAgent(enabled=False).review(c, s, {}).approve   # disabled abstains


def test_daily_loss_trips_persistent_kill_switch(ledger):
    price = Price(1.0)
    eng = build(ledger, price, risk=RiskAgent(max_pos_pct=0.5, max_exposure_pct=1.0, max_daily_loss_pct=0.05))
    eng.run_cycle()                                            # buys $50
    eng.strategies[0].fire = False
    # crash the token; widen stop so the loss shows up as mark-to-market first
    ledger.conn.execute("UPDATE positions SET stop_price=0.01")
    price.p = 0.8                                              # -$10 on $100 equity
    eng.run_cycle()
    assert eng.kill_switch()["active"]
    assert ledger.decisions(kind="kill_switch")
    # persistent: a new engine on the same ledger still sees it and refuses entries
    eng2 = build(ledger, Price(1.0))
    ledger.conn.execute("DELETE FROM positions")               # also exercises reconcile restoring it
    assert eng2.run_cycle()["entries"] == 0
    assert len(ledger.positions()) == 1                        # rebuilt from the ledger
    eng2.reset_kill_switch("test")
    assert not eng2.kill_switch()["active"]


def test_reconcile_repairs_positions_table(ledger):
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    ledger.conn.execute("UPDATE positions SET qty = qty * 2")
    ledger.upsert_position({"mint": "Ghost", "symbol": "GH", "strategy": "x", "qty": 1, "decimals": 6,
                            "cost_usd": 1, "entry_price": 1})
    issues = eng.reconcile()
    assert len(issues) == 2
    assert {p["mint"] for p in ledger.positions()} == {TOKEN}
    assert ledger.positions()[0]["qty"] == pytest.approx(ledger.positions_from_fills()[TOKEN]["qty"])


def test_research_high_risk_vetoes(ledger):
    from src.solana.research import ResearchAgent
    eng = build(ledger, Price(1.0))
    eng.research = ResearchAgent(ledger, token_info_fn=lambda m: None, llm_enabled=False)  # no data => high risk
    eng.run_cycle()
    assert ledger.fills() == []
    assert ledger.decisions(kind="veto")[0]["reason"].startswith("research:")


def test_market_profile_caps_size_and_widens_stops(ledger):
    eng = build(ledger, Price(1.0))
    eng.profiles = {"jupiter": {"max_pos_pct": 0.05, "stop_mult": 2.0}}
    eng.run_cycle()
    fill = ledger.fills()[0]
    assert fill["usd_gross"] == pytest.approx(5.0)                 # 5% slice, not the default 10%
    assert fill["stop_price"] == pytest.approx(0.95)               # flat candles: ATR stop at its 2.5% floor, widened x2
    assert fill["tp_price"] == pytest.approx(1.20)


def test_paused_market_gets_no_entries(ledger):
    eng = build(ledger, Price(1.0))
    eng.set_paused(TOKEN, True)
    assert eng.run_cycle()["entries"] == 0
    eng.set_paused(TOKEN, False)
    assert eng.run_cycle()["entries"] == 1


def test_stale_candles_are_vetoed(ledger):
    eng = build(ledger, Price(1.0))
    eng.candles_fn = lambda pool: [dict(c, t=c["t"] - 10 * 3600) for c in flat_candles(60)]
    assert eng.run_cycle()["entries"] == 0
    assert "stale" in ledger.decisions(kind="veto")[0]["reason"]


def test_daily_entry_cap(ledger):
    from src.solana.agents import RiskAgent
    price = Price(1.0)
    eng = build(ledger, price, risk=RiskAgent(max_entries_per_day=1))
    eng.rests_on_goal = False                         # the +20% win would otherwise end the day (daily goal)
    eng.run_cycle()
    price.p = 1.2                                     # TP exit next cycle, strategy still firing
    eng._entered.clear()                              # as if a new candle brought a fresh signal
    eng.run_cycle()
    assert len(ledger.closed_trades()) == 1
    assert len([f for f in ledger.fills() if f["side"] == "buy"]) == 1
    assert "daily entry cap" in ledger.decisions(kind="veto")[0]["reason"]


def test_executor_refuses_order_not_approved_by_risk(ledger):
    from src.solana.agents import ExecutorAgent, order_fingerprint
    from src.solana.broker import PaperBroker
    from tests.conftest import candidate, make_quote
    ex = ExecutorAgent(PaperBroker(ledger, quote_fn=make_quote(1.0), sol_usd_fn=lambda: 100.0))
    sig = Signal("momentum", 1.0, 0.05, 0.1, "x")
    approved = order_fingerprint("momentum", TOKEN, 10.0, 0.05, 0.1)
    with pytest.raises(RuntimeError, match="fingerprint"):
        ex.open("o1", candidate(), sig, 50.0, approved)        # size tampered after approval
    assert ledger.fills() == []
    ex.open("o2", candidate(), sig, 10.0, approved)
    assert len(ledger.fills()) == 1


def test_sleeping_wallet_skips_entries_until_woken(ledger, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "BOT_SELF_PAUSE", True)          # opt-in; by default the bots never sleep
    eng = build(ledger, Price(1.0))
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    ledger.set_state("sleep", {"until": until, "reason": "test", "prices": {}})
    s = eng.run_cycle()
    assert s["entries"] == 0 and s["sleeping_until"] == until
    ledger.set_state("sleep", {})                           # what "Analisar agora" does
    assert eng.run_cycle()["entries"] == 1


def test_coffee_keeps_the_wallet_awake(ledger):
    from datetime import datetime, timedelta, timezone
    eng = build(ledger, Price(1.0))
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    ledger.set_state("sleep", {"until": until, "reason": "test", "prices": {}})
    assert eng.coffee() is None
    eng.give_coffee(2)
    assert eng.coffee() and ledger.get_state("sleep") == {}
    ledger.set_state("sleep", {"until": until, "reason": "test", "prices": {}})   # even a stale sleep is ignored
    s = eng.run_cycle()
    assert s["entries"] == 1 and "sleeping_until" not in s


def test_setup_rank_prefers_oversold_uptrend():
    from src.solana.engine import _setup_rank
    up = [{"close": 100 + i * 0.5} for i in range(250)]
    dip = up[:-5] + [{"close": up[-6]["close"] - 1.5 * k} for k in range(1, 6)]   # pullback, still above EMA200
    down = [{"close": 300 - i * 0.5} for i in range(250)]
    ranked = sorted([("up", up), ("dip", dip), ("down", down)], key=lambda r: _setup_rank(r[1]))
    assert [n for n, _ in ranked] == ["dip", "up", "down"]


def test_market_temp_summarises_breadth_and_moves():
    from types import SimpleNamespace
    from src.solana.engine import _market_temp
    bar = lambda i, px: {"t": i, "open": px, "high": px * 1.001, "low": px * 0.999, "close": px, "volume": 1}
    up = [bar(i, 100 + i * 0.5) for i in range(250)]
    down = [bar(i, 300 - i * 0.5) for i in range(250)]
    t = _market_temp([(SimpleNamespace(symbol="UP"), up), (SimpleNamespace(symbol="DOWN"), down)])
    assert t["n"] == 2 and t["uptrend"] == 1 and t["best"][0][0] == "UP" and t["worst"][0][0] == "DOWN"
    radar = {r["symbol"]: r for r in t["watch"]}
    assert radar["DOWN"]["status"] == "sem tendência" and radar["UP"]["status"] != "sem tendência"
    assert radar["UP"]["dip_at"] < radar["UP"]["price"] < radar["UP"]["break_at"] * 1.01


def test_unreachable_exchange_skips_entries_without_tripping_the_kill_switch(ledger, monkeypatch):
    from src.solana import config as cfg
    eng = build(ledger, Price(1.0))
    def down():
        raise RuntimeError("HTTP 429 from https://api.hyperliquid.xyz/info")
    monkeypatch.setattr(eng.executor, "sync", down)
    monkeypatch.setattr(cfg, "SYNC_FAIL_LIMIT", 3)
    s = eng.run_cycle()
    assert s["entries"] == 0 and s["skipped"] == "exchange unreachable this cycle" and not eng.kill_switch()["active"]
    eng.run_cycle()
    eng.run_cycle()                                   # third failure in a row: now it is an outage
    assert eng.kill_switch()["active"]


def test_live_log_records_cycles_agents_and_decisions(ledger):
    from src.solana import logbuf
    start = logbuf.since(0)[-1]["seq"] if logbuf.since(0) else 0
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    lines = logbuf.since(start)
    msgs = " | ".join(l["msg"] for l in lines)
    assert "ciclo #1 começou" in msgs and "entry" in msgs
    assert any(l["level"] == "TRADE" for l in lines)
    assert logbuf.label("./live_mainnet.db") == "real" and logbuf.label("shadow.db") == "sim"
    assert logbuf.label("arena_trend.db") == "lab:endrick"


def test_bots_never_sleep_by_default(ledger):
    from datetime import datetime, timedelta, timezone
    eng = build(ledger, Price(1.0))
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    ledger.set_state("sleep", {"until": until, "reason": "old", "prices": {}})
    assert eng.run_cycle()["entries"] == 1          # a leftover sleep is ignored: they watch every cycle


def test_daily_goals_track_entries_and_cycles(ledger):
    from src.api.paper_routes import goals
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    g = goals(eng)
    assert g["trades"]["done"] == 1 and g["analyses"]["done"] >= 1
    assert g["profit"]["target_usd"] > 0 and g["hit"] is False


def test_bots_rest_after_the_daily_goal(ledger, monkeypatch):
    from src.solana import config as cfg
    eng = build(ledger, Price(1.0))
    eng.run_cycle()                                           # sets day start, opens one position
    day = ledger.get_state("day_start")
    ledger.set_state("day_start", {**day, "equity": day["equity"] / 1.05})   # pretend we are +5% today
    monkeypatch.setattr(cfg, "DAILY_GOAL_PCT", 0.02)
    s = eng.run_cycle()
    assert s["entries"] == 0 and "meta do dia batida" in s["skipped"]
    eng.rests_on_goal = False                                 # lab bots keep going
    assert "skipped" not in eng.run_cycle()


def test_trend_positions_are_day_trades():
    from src.solana.engine import _max_hold
    assert _max_hold("trend", 72) == 2 and _max_hold("rules", 72) == 2 and _max_hold("llm:claude-haiku-5-5", 72) == 2   # 2h cap for all


def test_a_hold_cap_change_never_closes_old_positions_retroactively(ledger, monkeypatch):
    """2026-10-22 01:33 UTC: deploying the new 2h cap closed 5 open positions at once (−$0.74). The cap in force at
    entry is stored with the trade and a later config change only applies to NEW positions."""
    from datetime import datetime, timedelta, timezone
    from src.solana import config as cfg
    price = Price(1.0)
    eng = build(ledger, price)
    monkeypatch.setattr(cfg, "MAX_HOLD_CAP_H", 24.0)                  # the rule when the trade was opened
    eng.run_cycle()
    [pos] = ledger.positions()
    assert ledger.get_state(f"hold:{pos['entry_order_id']}") == 24.0  # the trade's contract, persisted
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with ledger._lock:
        ledger.conn.execute("UPDATE positions SET opened_at=?", (old,))
    monkeypatch.setattr(cfg, "MAX_HOLD_CAP_H", 2.0)                   # the "deploy": a much tighter cap
    monkeypatch.setattr(cfg, "STALE_EXIT_H", 100.0)                   # isolate the time-cap behaviour
    monkeypatch.setattr(cfg, "SCALP_TP_PCT", 0)
    eng.strategies[0].fire = False
    eng.mark_and_exit()
    assert len(ledger.positions()) == 1                               # 3h held, old 24h contract: stays open
    with ledger._lock:                                                # at 25h even the old contract is over
        ledger.conn.execute("UPDATE positions SET opened_at=?",
                            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"),))
    eng.mark_and_exit()
    assert ledger.positions() == [] and "max holding time 24h" in ledger.closed_trades()[0]["reason"]
    assert ledger.get_state(f"hold:{ledger.closed_trades()[0]['entry_order_id']}") is None   # cleaned up


def test_time_exit_still_fires_when_mark_to_market_fails(ledger, monkeypatch):
    """2026-10-21 17:12 UTC: 429 storms made executor.mark raise and the old code skipped the whole exit check,
    holding positions past their cap. A time exit needs no fresh price."""
    from datetime import datetime, timedelta, timezone
    from src.solana import config as cfg
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    with ledger._lock:
        ledger.conn.execute("UPDATE positions SET opened_at=?",
                            ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),))
    monkeypatch.setattr(type(eng.executor), "mark", lambda self, pos: (_ for _ in ()).throw(RuntimeError("429 rate limit")))
    eng.strategies[0].fire = False
    eng.mark_and_exit()
    assert ledger.positions() == []
    [t] = ledger.closed_trades()
    assert "fecho pelo tempo" in t["reason"] and any("mark-to-market failed" in d["reason"]
                                                     for d in ledger.decisions(kind="error"))


def test_manual_buy_goes_through_risk_with_stop_and_target(ledger):
    import pytest
    eng = build(ledger, Price(1.0))
    sym = eng.scouts[0].candidates()[0].symbol
    out = eng.manual_entry(sym, 8.0)
    assert out["usd"] <= 8.0 and out["stop"] < out["price"] < out["tp"]
    pos = ledger.positions()[0]
    assert pos["strategy"] == "manual" and pos["stop_price"] and pos["tp_price"]
    with pytest.raises(ValueError):
        eng.manual_entry(sym, 8.0)                       # risk: already holding it
    eng.trip_kill_switch("test")
    with pytest.raises(ValueError, match="kill switch"):
        eng.manual_entry("OTHER", 8.0)


def test_trade_stats_summarise_history():
    from src.api.paper_routes import trade_stats
    base = {"entry_ts": "2026-10-21T10:00:00+00:00", "exit_ts": "2026-10-21T14:00:00+00:00", "fees_usd": 0.01, "funding_usd": 0.0}
    trades = [{**base, "symbol": "BTC", "strategy": "trend", "pnl_usd": 0.6, "reason": "take-profit hit"},
              {**base, "symbol": "ETH", "strategy": "rules", "pnl_usd": -0.3, "reason": "stop-loss hit"},
              {**base, "symbol": "BTC", "strategy": "trend", "pnl_usd": 0.3, "reason": "max holding time 24h reached"}]
    s = trade_stats(trades)
    assert s["trades"] == 3 and s["wins"] == 2 and s["profit_factor"] == 3.0 and s["avg_hold_h"] == 4.0
    assert s["by_bot"]["trend"]["pnl_usd"] == 0.9 and list(s["by_coin"]) == ["BTC", "ETH"]
    assert s["by_exit"]["alvo"]["trades"] == 1 and s["by_exit"]["tempo"]["trades"] == 1
    assert trade_stats([]) == {"trades": 0}


def test_confidence_checklist_starts_not_ready(ledger):
    from src.api.paper_routes import confidence
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    c = confidence(eng)
    assert c["total"] == 7 and not c["ready"] and any(x["name"].startswith("Sem kill switch") and x["ok"] for x in c["checks"])


def test_manual_exit_closes_a_position_and_records_it(ledger):
    import pytest
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    mint = ledger.positions()[0]["mint"]
    out = eng.manual_exit(mint)
    assert ledger.positions() == [] and "pnl_usd" in out
    assert ledger.decisions(5, kind="exit")[0]["reason"] == "fechada manualmente pelo painel"
    with pytest.raises(ValueError):
        eng.manual_exit(mint)


def test_protected_stop_breakeven_then_trails():
    from src.solana.engine import protected_stop
    pos = {"stop_price": 98.0, "tp_price": 106.0, "qty": 1.0, "cost_usd": 100.0}
    assert protected_stop(pos, 100.4) is None                     # small move: keep the original stop
    assert abs(protected_stop(pos, 101.0) - 100.5) < 1e-9         # quick win (+1%): lock half of the gain
    assert abs(protected_stop(pos, 101.5) - 101.2) < 1e-9         # +1.5% (second tier): lock 80% of the gain
    assert abs(protected_stop(pos, 103.2) - 102.56) < 1e-9        # keeps locking 80% as it rises
    assert abs(protected_stop(pos, 105.0) - 104.0) < 1e-9
    assert protected_stop({**pos, "stop_price": 104.5}, 105.0) is None   # never lowers the stop


def test_no_reentry_on_the_same_signal_candle(ledger):
    """After a take-profit, the same candle's signal (with its stale target) must not buy again."""
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    price.p = 1.2                       # target hit -> exit; the strategy still "fires" on the same candle
    eng.run_cycle()
    eng.run_cycle()
    assert len(ledger.closed_trades()) == 1 and ledger.positions() == []


def test_bot_records_situations_and_avoids_specific_losing_ones(ledger, monkeypatch):
    from src.solana import config as _cfg
    monkeypatch.setattr(_cfg, "BURST_MAX_ENTRIES", 0)            # this test buys three times in a row on purpose
    from src.solana import config as cfg, learning
    monkeypatch.setattr(learning, "CACHE_S", 0)
    monkeypatch.setattr(cfg, "LEARN_MIN_TRADES", 3)
    price = Price(1.0)
    eng = build(ledger, price)
    eng.rests_on_goal = False
    for _ in range(3):                                # same situation, stopped out three times
        price.p = 1.0
        eng.__dict__.get("_entered", set()).clear()
        eng.run_cycle()
        price.p = 0.9
        eng.strategies[0].fire = False
        eng.run_cycle()
        eng.strategies[0].fire = True
    assert len(ledger.closed_trades()) == 3 and all(t["pnl_usd"] < 0 for t in ledger.closed_trades())
    assert ledger.decisions(kind="entry")[0]["signals"]["tags"]           # each entry keeps its situation
    rec = learning.record(ledger)
    assert learning.summary(rec)["seen"] == 3
    # every tag here is on all of its trades: that is the strategy itself, not a situation to avoid
    assert learning.avoid(rec, list(rec)) == []
    # a specific losing situation among varied trades is avoided
    rec = {"moeda A": {"n": 6, "wins": 3, "pnl": 0.1}, "moeda B": {"n": 6, "wins": 0, "pnl": -0.5},
           "RSI alto": {"n": 4, "wins": 0, "pnl": -0.4}, "alta": {"n": 12, "wins": 3, "pnl": -0.4}}
    monkeypatch.setattr(cfg, "LEARN_MIN_TRADES", 4)
    assert learning.avoid(rec, ["RSI alto", "alta", "moeda B"]) == ["RSI alto", "moeda B"]
    assert learning.avoid({}, ["RSI alto"], inherited=["RSI alto"]) == ["RSI alto"]      # inherited lesson


def test_daily_quota_is_observational_by_default_and_rollback_restores_the_relax(ledger, monkeypatch):
    """Being behind a trade count must never loosen entries (incentive against profitability): the quota is logged
    and shown, nothing else. QUOTA_RELAX_ENTRIES=1 is the rollback to the old behaviour."""
    from src.solana import arena, config as cfg
    monkeypatch.setattr(arena, "is_titular", lambda m: True)
    monkeypatch.setattr(cfg, "DAILY_MIN_TRADES", 100_000)          # far behind whatever the hour
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    entry = ledger.decisions(kind="entry")[0]
    assert "cota do dia" not in entry["reason"] and eng.strategies[0].relax == 0   # entry untouched
    [q] = ledger.decisions(kind="quota")
    assert "observacional" in q["reason"]                          # still measured and visible
    monkeypatch.setattr(cfg, "QUOTA_RELAX_ENTRIES", True)          # rollback switch: the old loosening
    eng.__dict__.pop("_entered", None); eng.ledger.delete_state("entered")
    ledger2 = ledger.__class__(":memory:", 100.0)
    eng2 = build(ledger2, Price(1.0))
    eng2.run_cycle()
    entry2 = ledger2.decisions(kind="entry")[0]
    assert "cota do dia (nível 2)" in entry2["reason"] and eng2.strategies[0].relax == 2
    monkeypatch.setattr(arena, "is_titular", lambda m: False)        # reserves: no quota, but they train aggressively
    assert eng2._quota_levels() == {} and eng2.strategies[0].relax == cfg.RESERVE_RELAX


def test_a_manual_order_makes_a_running_cycle_stop_early(ledger):
    eng = build(ledger, Price(1.0))
    eng._yield = True                         # what manual_exit/manual_entry set while waiting for the cycle
    assert eng.run_cycle()["entries"] == 0 and ledger.positions() == []
    eng._yield = False
    assert eng.run_cycle()["entries"] == 1


def test_learning_backfills_situations_of_old_trades(monkeypatch):
    from types import SimpleNamespace
    from src.solana import hyperliquid as hl, learning
    from tests.conftest import flat_candles
    candles = flat_candles(300)
    state = {}
    led = SimpleNamespace(path="x", get_state=lambda k, d=None: state.get(k, d), set_state=lambda k, v: state.__setitem__(k, v),
                          decisions=lambda n, kind=None: [],
                          closed_trades=lambda: [{"order_id": "exit-o1", "symbol": "BTC", "strategy": "s", "pnl_usd": -0.1,
                                                  "entry_ts": __import__("datetime").datetime.fromtimestamp(candles[250]["t"] + 3600, __import__("datetime").timezone.utc).isoformat(),
                                                  "exit_ts": "2099-01-01T00:00:00+00:00"}])
    monkeypatch.setattr(hl, "candles", lambda coin, interval="1h": candles)
    monkeypatch.setattr(learning, "CACHE_S", 0)
    assert learning.backfill(led) == 1 and "moeda BTC" in state["tags_backfill"]["o1"]
    assert learning.record(led)["moeda BTC"]["n"] == 1          # the old trade now counts


def test_margin_of_error_skips_only_confident_losers():
    from src.solana import edge
    bad = {"RSI alto": {"n": 40, "wins": 8}, "moeda A": {"n": 50, "wins": 25}, "moeda B": {"n": 50, "wins": 25}}
    e = edge.estimate(["RSI alto"], {}, bad, avg_win=0.10, avg_loss=0.10)     # the team won 8 of 40 in this situation
    assert e["skip"] and e["p"] < 0.3 and e["ev_usd"] < 0
    good = {"queda numa alta": {"n": 40, "wins": 26}, "moeda A": {"n": 100, "wins": 50}}
    assert not edge.estimate(["queda numa alta"], {}, good, 0.10, 0.10)["skip"]
    # wins rarely but wins big: 30% at +$0.40 vs −$0.10 is still a good trade
    assert not edge.estimate(["RSI alto"], {}, {**bad, "RSI alto": {"n": 40, "wins": 12}}, 0.40, 0.10)["skip"]
    # a label on every trade is the strategy itself: never judged here
    assert not edge.estimate(["alta"], {}, {"alta": {"n": 100, "wins": 20}, "moeda A": {"n": 100, "wins": 20}}, 0.1, 0.1)["skip"]
    # judged against the bot's usual: a 20% situation for a bot that usually wins 25% is not "clearly worse"
    assert not edge.estimate(["RSI alto"], {}, bad, 0.10, 0.10, baseline=0.25)["skip"]
    assert edge.estimate(["RSI alto"], {}, bad, 0.10, 0.10, baseline=0.55)["skip"]
    thin = edge.estimate(["RSI alto"], {"RSI alto": {"n": 3, "wins": 0}}, {}, 0.10, 0.10)
    assert not thin["skip"] and thin["margin"] > 0.2            # too little evidence: wide margin, never blocks


def test_btc_filter_blocks_new_buys_when_btc_is_weak(ledger, monkeypatch):
    from src.solana import config as cfg, hyperliquid as hl
    from tests.conftest import flat_candles
    monkeypatch.setattr(cfg, "BTC_FILTER", True)
    falling = flat_candles(200)
    for i, c in enumerate(falling):
        c["close"] = 100 - i * 0.1                     # steady fall: last close below its 50h average
    monkeypatch.setattr(hl, "candles", lambda coin, interval="1h": falling)
    eng = build(ledger, Price(1.0))
    assert eng.run_cycle()["entries"] == 0 and "filtro BTC" in ledger.decisions(kind="btc_filter")[0]["reason"]


def test_titular_holds_at_most_3_positions_on_the_account(ledger, monkeypatch):
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "MAX_POSITIONS_PER_BOT", 0)       # already at the cap
    eng = build(ledger, Price(1.0))
    assert eng.run_cycle()["entries"] == 0


def test_account_waits_after_a_burst_of_buys(ledger, monkeypatch):
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "BURST_MAX_ENTRIES", 1)
    price = Price(1.0)
    eng = build(ledger, price)
    eng.rests_on_goal = False
    assert eng.run_cycle()["entries"] == 1
    price.p = 1.2                                  # target hit: the position closes, the coin is free again
    eng.strategies[0].fire = False
    eng.run_cycle()
    price.p, eng.strategies[0].fire = 1.0, True
    eng.__dict__.get("_entered", set()).clear()
    assert ledger.positions() == [] and eng.run_cycle()["entries"] == 0     # one buy in the last 30 min already
    monkeypatch.setattr(cfg, "BURST_MAX_ENTRIES", 2)
    assert eng.run_cycle()["entries"] == 1


def test_universe_filter_keeps_the_most_liquid_and_volatile_coins_plus_focus_and_held(ledger, monkeypatch):
    from src.solana import config as cfg, engine as E
    from tests.conftest import candidate
    monkeypatch.setattr(cfg, "UNIVERSE_SIZE", 1)
    monkeypatch.setattr(cfg, "FOCUS_COINS", ("FOC",))
    monkeypatch.setattr(E, "_universe", {})
    cands = [candidate(mint=f"M{s}", symbol=s, volume_h24=v) for s, v in (("SMALL", 1e6), ("BIG", 3e6), ("FOC", 1.0))]
    ready = [(c, flat_candles(30)) for c in cands]
    assert E.universe(ready) == ["BIG"] and E.current_universe()["scores"]["BIG"] > E.current_universe()["scores"]["SMALL"]

    class Scout:
        def candidates(self):
            return cands
    eng = build(ledger, Price(1.0))
    eng.scouts = [Scout()]
    assert eng.run_cycle()["entries"] == 2                               # BIG (universe) and FOC (focus coin), not SMALL
    assert {p["symbol"] for p in ledger.positions()} == {"BIG", "FOC"}


def test_entry_stop_is_atr_sized_and_size_stays_the_minimum_order(ledger):
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    [pos] = ledger.positions()
    assert pos["stop_price"] == pytest.approx(0.975)      # flat candles: 1.5 ATR ≈ 0.3% -> floor of half the 5% stop
    assert pos["tp_price"] == pytest.approx(1.10)         # the target is the strategy's


def test_a_position_that_goes_nowhere_is_closed_after_an_hour_but_a_winner_keeps_running(ledger):
    """Data: 149 time exits lost $9.15. After STALE_EXIT_H without +STALE_EXIT_PCT the position leaves early."""
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    eng.strategies[0].fire = False
    old = (datetime.now(timezone.utc) - timedelta(hours=1.5)).isoformat(timespec="seconds")
    with ledger._lock:
        ledger.conn.execute("UPDATE positions SET opened_at=?", (old,))
    price.p = 1.0                                   # flat after 1.5h: no traction
    eng.mark_and_exit()
    assert ledger.positions() == [] and "sem tração" in ledger.closed_trades()[0]["reason"]
    price.p = 1.0
    eng.strategies[0].fire = True
    eng.__dict__.pop("_entered", None); eng.ledger.delete_state("entered")               # one trade per signal candle: let it enter again
    eng.run_cycle()                                  # a new position, again held 1.5h but this time it is up 1%
    with ledger._lock:
        ledger.conn.execute("UPDATE positions SET opened_at=?", (old,))
    eng.strategies[0].fire = False
    price.p = 1.005
    eng.mark_and_exit()
    assert len(ledger.positions()) == 1              # +0.5%: not stale and under the +1% scalp exit, it keeps going to its stop/target/cap


def test_short_candidates_go_only_to_short_bots_and_the_real_gate_can_close_them(ledger, monkeypatch):
    """One scout offers BTC long and BTC~short: a long bot must never trade the short instrument and a short bot never the long one;
    on the real account the short gate (switch / regime / limits) decides before any short bot may open."""
    class Both:
        def candidates(self):
            return [candidate(mint=TOKEN, symbol="LNG"), candidate(mint="hl:BTC~short", symbol="SHT")]

    class Fire:
        capital_pct = 0.5

        def __init__(self, name):
            self.name = name

        def signal(self, candles):
            return Signal(self.name, 1.0, 0.05, 0.10, "fixture")

    broker = PaperBroker(ledger, quote_fn=Price(1.0).quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
    eng = PaperEngine(ledger, Both(), FakeSafety(), [Fire("momentum"), Fire("short_break")], DebateAgent(enabled=False), RiskAgent(),
                      ExecutorAgent(broker), candles_fn=lambda pool: flat_candles(60), profiles={})
    eng.run_cycle()
    entered = {(d["symbol"], d["strategy"]) for d in ledger.decisions(kind="entry")}
    assert ("LNG", "short_break") not in entered and ("SHT", "momentum") not in entered      # no cross-trading
    assert ("LNG", "momentum") in entered
    ledger2 = ledger.__class__(":memory:", 100.0)
    broker2 = PaperBroker(ledger2, quote_fn=Price(1.0).quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
    eng2 = PaperEngine(ledger2, Both(), FakeSafety(), [Fire("short_break")], DebateAgent(enabled=False), RiskAgent(),
                       ExecutorAgent(broker2), candles_fn=lambda pool: flat_candles(60), profiles={})
    eng2.short_gate = lambda btc_ok: "short desligado na conta real"                           # what the real engine installs
    eng2.run_cycle()
    assert ledger2.decisions(kind="entry") == []


def test_a_small_profit_is_sold_at_once_and_a_lab_reserve_trades_through_the_btc_filter(ledger, monkeypatch):
    from src.solana import arena, config as cfg2
    monkeypatch.setattr(cfg2, "SCALP_TP_PCT", 0.01)
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    eng.strategies[0].fire = False
    price.p = 1.015                                                    # +1.5% over the cost basis: "see a profit and sell"
    eng.mark_and_exit()
    [t] = ledger.closed_trades()
    assert "lucro curto" in t["reason"] and t["pnl_usd"] > 0
    # BTC under its 50h average: longs are blocked for titulars (and the real account), but a lab reserve keeps learning
    monkeypatch.setattr(cfg2, "BTC_FILTER", True)
    monkeypatch.setattr(PaperEngine, "_btc_ok", lambda self: False)
    eng.strategies[0].fire = True
    eng.__dict__.pop("_entered", None); eng.ledger.delete_state("entered")
    eng.run_cycle()
    assert len(ledger.positions()) == 0                                # a plain engine: filter holds
    eng.lab_model = "whales2_g2_5"
    monkeypatch.setattr(arena, "is_titular", lambda m: True)           # a titular's lab twin keeps the filter
    eng.run_cycle()
    assert len(ledger.positions()) == 0
    monkeypatch.setattr(arena, "is_titular", lambda m: False)          # a reserve in the lab keeps learning
    eng.run_cycle()
    assert len(ledger.positions()) == 1


def test_fast_15m_bots_trade_the_real_engine_through_a_weak_btc_one_at_a_time_while_others_stay_blocked(ledger, monkeypatch):
    from src.solana import arena, config as cfg2, strategies as st
    monkeypatch.setattr(cfg2, "LIVE_FAST_ALLOW", True)
    monkeypatch.setattr(cfg2, "LIVE_FAST_MAX_OPEN", 1)
    st._fast_cache.clear()

    class Two:
        def candidates(self):
            return [candidate(mint=TOKEN, symbol="AAA"), candidate(mint="TokenMint2222222222222222222222222222222222", symbol="BBB")]

    class Fire:
        capital_pct = 0.5

        def __init__(self, name):
            self.name = name

        def signal(self, candles):
            return Signal(self.name, 1.0, 0.05, 0.10, "fixture")

    broker = PaperBroker(ledger, quote_fn=Price(1.0).quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
    monkeypatch.setattr(PaperEngine, "_btc_ok", lambda self: False)                   # BTC below its 50h average
    cfg2.BTC_FILTER = True
    eng = PaperEngine(ledger, Two(), FakeSafety(), [Fire("momentum")], DebateAgent(enabled=False), RiskAgent(),
                      ExecutorAgent(broker), candles_fn=lambda pool: flat_candles(60), profiles={})
    eng.fast_real = True
    eng.run_cycle()
    assert ledger.positions() == []                                                    # a normal bot: the BTC filter holds
    eng.strategies = [Fire("whales_15m")]
    eng.run_cycle()
    assert [p["symbol"] for p in ledger.positions()] == ["AAA"]                        # the 15m bot trades through it... and only ONE position
    assert arena.size_for("whales_15m") == cfg2.LIVE_FAST_USD == 10.0
    members = [type("M", (), {"model": m})() for m in ("whales_15m", "short_break_15m", "whales2")]
    # fast lane plays; the squad floor calls up the best non-short reserve too (owner's rule); the short never
    assert [m.model for m in arena.eligible_for_real(members)] == ["whales_15m", "whales2"]
    monkeypatch.setattr(cfg2, "LIVE_FAST_ALLOW", False)
    called = [m.model for m in arena.eligible_for_real(members)]                       # switch off: no fast lane —
    assert "short_break_15m" not in called and set(called) <= {"whales_15m", "whales2"}   # floor call-ups only, never the short


def test_only_listed_fast_bots_get_real_access(monkeypatch):
    from src.solana import config as cfg2, strategies as st
    st._fast_cache.clear()
    monkeypatch.setattr(cfg2, "LIVE_FAST_BOTS", {"whales_15m", "copy_15m"})
    assert st.is_fast("whales_15m") and st.is_fast("llm:copy_15m")
    assert not st.is_fast("trend4h_15m") and not st.is_fast("short_break_15m") and not st.is_fast("rules")      # fast but not listed / a short / not fast


def test_lab_reserves_scan_every_coin_while_titulars_keep_the_hourly_universe(ledger, monkeypatch):
    from src.solana import arena, config as cfg2, engine as eng_mod
    monkeypatch.setattr(cfg2, "UNIVERSE_SIZE", 1)                                  # the hourly universe holds ONE coin
    monkeypatch.setattr(eng_mod, "universe", lambda ready: [ready[0][0].symbol])

    class Three:
        def candidates(self):
            return [candidate(mint=f"TokenMint{i}" + "1" * 30, symbol=f"C{i}") for i in range(3)]

    class Fire:
        capital_pct = 0.3
        name = "momentum"

        def signal(self, candles):
            return Signal(self.name, 1.0, 0.05, 0.10, "fixture")

    def run(lab_model, titular):
        led = ledger.__class__(":memory:", 100.0)
        broker = PaperBroker(led, quote_fn=Price(1.0).quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
        e = PaperEngine(led, Three(), FakeSafety(), [Fire()], DebateAgent(enabled=False), RiskAgent(), ExecutorAgent(broker),
                        candles_fn=lambda pool: flat_candles(60), profiles={})
        if lab_model:
            e.lab_model = lab_model
            monkeypatch.setattr(arena, "is_titular", lambda m: titular)
        e.run_cycle()
        return len(led.positions())

    assert run(None, False) == 1                                                   # the real/plain engine: only the universe coin
    assert run("whales2_g2_5", True) == 1                                          # a titular's lab twin: same
    assert run("whales2_g2_5", False) == 3                                         # a reserve: all coins


def test_holding_policy_short_for_fast_family_long_for_slow_families_real_capped():
    """§5.5: each family trades its own horizon in the lab — 15m twins on the scalp clock, trend/trend4h on their
    strategy horizon (up to LAB_HOLD_CAP_H) — while real money stays under the owner's 2h cap until evidence."""
    from src.solana.engine import _max_hold
    assert _max_hold("whales_15m", 72, lab=True) == 0.5              # fast family: short trades
    assert _max_hold("trend", 72, lab=True) == 24                    # slow family: its own 24h horizon in the lab
    assert _max_hold("trend4h", 72, lab=True) == 48                  # capped by LAB_HOLD_CAP_H
    assert _max_hold("trend", 72) == 2 and _max_hold("trend4h", 72) == 2   # real account keeps the 2h cap


def test_a_restart_never_repeats_the_same_candles_entry(ledger, monkeypatch):
    """The one-trade-per-signal memory is persisted: a new engine on the same ledger (an app restart) refuses to
    re-enter on the candle that already produced a trade."""
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "SCALP_TP_PCT", 0)
    price = Price(1.2)                                     # above the TP so the trade closes on the next cycle
    eng = build(ledger, price)
    assert eng.run_cycle()["entries"] == 1
    eng.run_cycle()                                        # take-profit: position closed, signal spent
    assert ledger.positions() == [] and len(ledger.closed_trades()) == 1
    fresh = build(ledger, price)                           # the restart: new engine, same ledger
    assert fresh.run_cycle()["entries"] == 0               # same candle, no repeat
    assert "sinal já usado" in ledger.get_state("agents")["momentum"]["last_reason"]


def test_regime_is_logged_tagged_on_entries_and_the_intent_rides_the_decision(ledger, monkeypatch):
    """P1: the regime is observational — stored, logged on change, tagged on every entry (the evidence a future
    gate needs) — and the §20 intent, validated before Risk, is attached to the entry decision with its sizing."""
    from src.solana import config as cfg, hyperliquid as hl
    from tests.test_regime import _candles
    monkeypatch.setattr(cfg, "REGIME_ENABLED", True)
    bull = _candles([100 * 1.005 ** i for i in range(250)])
    monkeypatch.setattr(hl, "candles", lambda coin, interval="1h": bull)
    eng = build(ledger, Price(1.0))
    eng.run_cycle()
    reg = ledger.get_state("regime")
    # BTC rallies but the fake market's breadth is 0/1 coins in uptrend: honestly downgraded to "alta fraca"
    assert reg["state"] == "alta fraca" and reg["calibrated"] is False
    entry = ledger.decisions(kind="entry")[0]
    assert "regime alta fraca" in entry["signals"]["tags"]
    it = entry["signals"]["intent"]
    assert it["side"] == "buy" and it["market_type"] == "spot" and it["version"] == 1
    assert it["decision_id"] == entry["order_id"] and it["risk_budget_request_usd"] > 0
    stop_frac = 1 - it["stop_price"] / it["expected_entry_price"]        # ATR-resized stop (2.5% on flat candles)
    assert it["estimated_loss_at_stop_usd"] == pytest.approx(it["risk_budget_request_usd"] * stop_frac, rel=1e-4)
    eng.run_cycle()                                       # unchanged regime: logged only on change
    assert sum(d["kind"] == "regime" for d in ledger.decisions(100)) == 1


def test_a_second_entry_highly_correlated_with_an_open_position_is_skipped(ledger, monkeypatch):
    """P1: two near-copies of one bet add exposure, not diversification — the redundant entry is vetoed.
    CORR_MAX_ENTRY=0 disables the guard (rollback)."""
    import time as _t
    from src.solana import config as cfg

    def wavy(n=60):
        closes, pat = [1.0], (0.011, -0.006, 0.014, -0.01, 0.004)
        for i in range(n - 1):
            closes.append(closes[-1] * (1 + pat[i % len(pat)]))
        now = int(_t.time()) // 3600 * 3600
        return [dict(t=now - (n - 1 - i) * 3600, open=c, high=c * 1.001, low=c * 0.999, close=c, volume=100.0)
                for i, c in enumerate(closes)]

    class TwoScout:
        def candidates(self):
            return [candidate(), candidate(mint="Token2222222222222222222222222222222222222222",
                                           symbol="TK2", pair_address="Pool222")]

    series = wavy()
    broker = PaperBroker(ledger, quote_fn=Price(1.0).quote, sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
    eng = PaperEngine(ledger, TwoScout(), FakeSafety(), [FixedStrategy()], DebateAgent(enabled=False), RiskAgent(),
                      ExecutorAgent(broker), candles_fn=lambda pool: series, profiles={})
    s = eng.run_cycle()
    assert s["entries"] == 1                                            # identical series: the copy is redundant
    assert any("exposição redundante" in d["reason"] for d in ledger.decisions(kind="veto"))
    monkeypatch.setattr(cfg, "CORR_MAX_ENTRY", 0.0)                     # rollback: guard off
    assert eng.run_cycle()["entries"] == 1                              # TK2 now enters (TKN is already held)
    assert {p["symbol"] for p in ledger.positions()} == {"TKN", "TK2"}


def test_exit_watchdog_manages_positions_without_a_cycle_and_without_llm(ledger, monkeypatch):
    """§6/§12.3: protection must not wait for the 5-minute entry cycle. The watchdog marks, applies stops/targets/
    time exits — and NEVER asks the LLM (the fast lane pays no analysis) — then steps aside while a cycle runs."""
    from datetime import datetime, timedelta, timezone
    from src.solana import config as cfg
    monkeypatch.setattr(cfg, "SCALP_TP_PCT", 0)
    price = Price(1.0)
    eng = build(ledger, price)
    assert eng.watch_exits() is None                     # no positions: free no-op
    eng.run_cycle()
    calls = []

    class Owner:                                         # an LLM exit reviewer that must stay silent in the fast lane
        name, enabled, _rules = "momentum", True, None

        def review_exit(self, pos, candles, px):
            calls.append(px)
            return None
    ledger.set_state("decision_mode", "llm")
    eng.team = [Owner()]
    price.p = 1.02                                       # between stop and target: only the LLM could exit here
    assert eng.watch_exits() == {"watched": True}
    assert calls == [] and len(ledger.positions()) == 1  # no LLM call, position untouched
    with ledger._lock:                                   # now the position is past its hold cap
        ledger.conn.execute("UPDATE positions SET opened_at=?",
                            ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),))
    ledger.set_state("hold:" + ledger.positions()[0]["entry_order_id"], 2.0)
    assert eng.watch_exits() == {"watched": True}
    assert ledger.positions() == [] and calls == []      # time exit fired deterministically, still no LLM
    assert "max holding" in ledger.closed_trades()[0]["reason"]


def test_exit_watchdog_steps_aside_while_a_cycle_runs(ledger):
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    assert eng._busy.acquire(blocking=False)             # a cycle is "running"
    try:
        assert eng.watch_exits() == {"busy": True}       # never two managers at once
        assert len(ledger.positions()) == 1
    finally:
        eng._busy.release()


def test_watchdog_tick_respects_armed_and_running(monkeypatch):
    from types import SimpleNamespace
    from src.solana import hl_live
    seen = []
    live = SimpleNamespace(watch_exits=lambda: seen.append("real"),
                           ledger=SimpleNamespace(get_state=lambda k, d=None: {"armed": {"armed": False}}.get(k, d)))
    sim = SimpleNamespace(watch_exits=lambda: seen.append("sim"),
                          ledger=SimpleNamespace(get_state=lambda k, d=None: {"running": True}.get(k, d)))
    monkeypatch.setattr(hl_live, "get_live_engine", lambda: live)
    monkeypatch.setattr(hl_live, "get_shadow_engine", lambda: sim)
    hl_live.watch_exits()
    assert seen == ["sim"]                               # disarmed real account is not managed; running sim is
