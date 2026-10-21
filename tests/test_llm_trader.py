"""LLM decision mode: the LLM decides if/when; Risk still decides how much, and invalid output is HOLD."""
import pytest

from src.solana import config as cfg
from src.solana.llm_trader import LLMDecider
from tests.conftest import flat_candles
from tests.test_engine import Price, build


def decider(answer, calls=None):
    def fn(prompt, system):
        if calls is not None:
            calls.append(system[:20])
        return answer
    return LLMDecider(llm_fn=fn)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-key")


def test_buy_with_stop_and_tp_clamped():
    sig = decider('{"action":"BUY","stop_pct":0.5,"tp_pct":3,"confidence":0.7,"reason":"oversold bounce"}') \
        .decide(flat_candles(60), {"symbol": "BTC"})
    assert sig and sig.stop_pct == 0.10 and sig.tp_pct == 0.20 and "oversold" in sig.reason


def test_invalid_or_hold_means_no_trade():
    for ans in ("I would buy", '{"action":"SHORT"}', '{"action":"HOLD","reason":"no edge"}', '{"action":"BUY","stop_pct":"x"}'):
        assert decider(ans).decide(flat_candles(60), {"symbol": "BTC"}) is None


def test_one_call_per_closed_candle():
    calls = []
    d = decider('{"action":"HOLD"}', calls)
    c = flat_candles(60)
    d.decide(c, {"symbol": "BTC"})
    d.decide(c, {"symbol": "BTC"})
    assert len(calls) == 1
    d.decide(c + [dict(c[-1], t=c[-1]["t"] + 3600)], {"symbol": "BTC"})
    assert len(calls) == 2


def test_engine_llm_mode_risk_still_sizes(ledger):
    eng = build(ledger, Price(1.0))
    eng.llm = decider('{"action":"BUY","stop_pct":0.05,"tp_pct":0.1,"reason":"go all in, size $1000"}')
    eng.set_decision_mode("llm")
    eng.run_cycle()
    f = ledger.fills()[0]
    assert f["strategy"] == "llm" and f["usd_gross"] == pytest.approx(10.0)   # RiskAgent's 10%, not the LLM's $1000
    entry = ledger.decisions(kind="entry")[0]
    assert entry["votes"]["debate"]["reason"].startswith("skipped")


def test_llm_sell_closes_position(ledger):
    price = Price(1.0)
    eng = build(ledger, price)
    eng.llm = decider('{"action":"BUY","stop_pct":0.05,"tp_pct":0.1,"reason":"x"}')
    eng.set_decision_mode("llm")
    eng.run_cycle()
    ledger.conn.execute("UPDATE positions SET pool_address='Pool111'")
    eng.llm.llm_fn = lambda p, s: '{"action":"SELL","reason":"momentum faded"}'
    eng.llm._seen.clear()
    eng.run_cycle()
    assert "LLM sell" in ledger.closed_trades()[0]["reason"]


def test_llm_mode_needs_api_key(ledger, monkeypatch):
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    eng = build(ledger, Price(1.0))
    eng.llm = LLMDecider()
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        eng.set_decision_mode("llm")
