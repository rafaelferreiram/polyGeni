"""Full pipeline with fakes (no network): entries, exits, vetoes, kill switch, reconciliation."""
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


def test_entry_then_take_profit_exit(ledger):
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
    assert fill["stop_price"] == pytest.approx(0.90)               # 5% stop widened x2
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
    assert _max_hold("trend", 72) == 24 and _max_hold("rules", 72) == 72 and _max_hold("llm:claude-haiku-5-5", 72) == 72


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
    pos = {"stop_price": 98.0, "tp_price": 103.0, "qty": 1.0, "cost_usd": 100.0}
    assert protected_stop(pos, 101.0) is None                     # not halfway yet: keep the original stop
    assert abs(protected_stop(pos, 101.6) - 100.6) < 1e-9         # halfway: stop above entry, 1 ATR below the price
    assert abs(protected_stop(pos, 102.9) - 101.9) < 1e-9         # and it keeps trailing up
    assert protected_stop({**pos, "stop_price": 102.0}, 102.9) is None   # never lowers the stop


def test_no_reentry_on_the_same_signal_candle(ledger):
    """After a take-profit, the same candle's signal (with its stale target) must not buy again."""
    price = Price(1.0)
    eng = build(ledger, price)
    eng.run_cycle()
    price.p = 1.2                       # target hit -> exit; the strategy still "fires" on the same candle
    eng.run_cycle()
    eng.run_cycle()
    assert len(ledger.closed_trades()) == 1 and ledger.positions() == []


def test_bot_learns_to_avoid_a_situation_it_keeps_losing_in(ledger, monkeypatch):
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
    price.p = 1.0
    eng.__dict__.get("_entered", set()).clear()
    eng.risk.max_daily_loss_pct = 1.0                 # the daily loss stop must not be what blocks it
    eng.run_cycle()
    assert len(ledger.closed_trades()) == 3 and ledger.positions() == []
    assert "aprendizado: evita" in ledger.decisions(kind="veto")[0]["reason"]
    summary = learning.summary(learning.record(ledger))
    assert summary["avoid"] and summary["seen"] == 3
