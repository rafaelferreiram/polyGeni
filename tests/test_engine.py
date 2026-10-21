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
                       candles_fn=lambda pool: flat_candles(60))


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
