"""TradeIntent (§20): typed, versioned, validated before Risk — sides, price ordering, freshness, expiry."""
import time

import pytest

from src.solana import contract
from src.solana.strategies import Signal
from tests.conftest import candidate, flat_candles


def _sig(ref=1.0, stop=0.05, tp=0.10):
    return Signal("momentum", ref, stop, tp, "fixture")


def test_spot_intent_builds_buy_side_and_validates():
    c = candidate()
    it = contract.build(c, _sig(), "momentum", flat_candles(60)[:-1], 2.0,
                        {"state": "alta fraca"}, {"ev_usd": 0.05, "p": 0.6, "tags": ["alta"]}, cycle=7)
    it.validate()
    assert it.market_type == "spot" and it.side == "buy" and it.timeframe == "1h"
    assert it.market_regime == "alta fraca" and it.confidence == 0.6 and it.version == contract.VERSION
    assert it.expected_net_edge_usd == 0.05              # rule bot: zero marginal AI cost
    assert it.evidence_references == ["alta"] and it.bot_id == "momentum"


def test_perp_long_and_short_sides_follow_the_instrument():
    long_c = candidate(mint="hl:BTC", symbol="BTC", venue="hyperliquid")
    it = contract.build(long_c, _sig(), "trend", flat_candles(60)[:-1], 2.0, None, None, cycle=1)
    it.validate()
    assert it.market_type == "perp" and it.side == "open_long" and it.estimated_fees_bps == 9.0
    short_c = candidate(mint="hl:BTC~short", symbol="BTC", venue="hyperliquid")
    st = contract.build(short_c, _sig(), "short_break", flat_candles(60)[:-1], 2.0, None, None, cycle=1)
    st.validate()                                        # mirror space: stop < entry < target still holds
    assert st.side == "open_short"
    st.side = "open_long"                                # a long side on a ~short instrument is a semantic bug
    with pytest.raises(ValueError, match="~short"):
        st.validate()


def test_price_ordering_freshness_and_expiry_are_enforced():
    c, candles = candidate(), flat_candles(60)[:-1]
    bad = contract.build(c, _sig(), "momentum", candles, 2.0, None, None, cycle=1)
    bad.stop_price = 1.5                                 # stop above the entry
    with pytest.raises(ValueError, match="stop"):
        bad.validate()
    bad = contract.build(c, _sig(), "momentum", candles, 2.0, None, None, cycle=1)
    bad.take_profit_policy = {"type": "fixed", "price": 0.9}
    with pytest.raises(ValueError, match="alvo"):
        bad.validate()
    old = [dict(k, t=k["t"] - 4 * 3600) for k in candles]        # candle 4h old: stale
    with pytest.raises(ValueError, match="velhos"):
        contract.build(c, _sig(), "momentum", old, 2.0, None, None, cycle=1).validate()
    exp = contract.build(c, _sig(), "momentum", candles, 2.0, None, None, cycle=1)
    exp.expiration_timestamp = time.time() - 1           # the candle that produced it is gone
    with pytest.raises(ValueError, match="expirado"):
        exp.validate()
    with pytest.raises(ValueError, match="max_holding"):
        contract.build(c, _sig(), "momentum", candles, 0.0, None, None, cycle=1).validate()


def test_llm_marginal_ai_cost_is_subtracted_from_the_edge(monkeypatch, tmp_path):
    from src.solana import costs
    b = costs.CostBook(str(tmp_path / "c.db"))
    monkeypatch.setattr(costs, "_book", b)
    for _ in range(4):
        b.record("claude-haiku-5-5", "llm:claude-haiku-5-5", {"input_tokens": 100_000})   # $0.01 each
    it = contract.build(candidate(), _sig(), "llm:claude-haiku-5-5", flat_candles(60)[:-1], 2.0,
                        None, {"ev_usd": 0.05, "tags": []}, cycle=1)
    assert it.estimated_ai_cost_usd == pytest.approx(0.01)
    assert it.expected_net_edge_usd == pytest.approx(0.04)
    none = contract.build(candidate(), _sig(), "llm:claude-haiku-5-5", flat_candles(60)[:-1], 2.0, None, None, cycle=1)
    assert none.expected_net_edge_usd is None            # no evidence: no invented edge
