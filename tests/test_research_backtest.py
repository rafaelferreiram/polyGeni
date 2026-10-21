from src.solana.backtest import _metrics, hold, simulate
from src.solana.research import quant_brief
from src.solana.strategies import Signal
from tests.conftest import flat_candles

GOOD = {"organicScore": 90, "isVerified": True, "holderCount": 10_000, "liquidity": 5e6,
        "audit": {"topHoldersPercentage": 15, "devMints": 1},
        "stats24h": {"buyVolume": 100, "sellVolume": 90, "liquidityChange": 1, "holderChange": 0.1}}


def test_quant_brief_flags():
    assert quant_brief(GOOD)["risk_level"] == "low"
    bad = {**GOOD, "organicScore": 10, "isVerified": False,
           "stats24h": {"buyVolume": 10, "sellVolume": 100, "liquidityChange": -50, "holderChange": -5}}
    b = quant_brief(bad)
    assert b["risk_level"] == "high" and len(b["flags"]) >= 3
    assert quant_brief(None)["risk_level"] == "high"


class AlwaysOnce:
    def __init__(self):
        self.done = False

    def signal(self, candles):
        if self.done:
            return None
        self.done = True
        return Signal("t", candles[-1]["close"], 0.05, 0.10, "x")


def test_simulate_charges_costs_and_hits_tp():
    c = flat_candles(10)
    c[3] = dict(c[3], high=1.2)                     # bar 3 trades through TP 1.10
    r = simulate(AlwaysOnce(), {"X": c}, 1, 10, cost_bps=60, fee=0.02)
    assert len(r["trades"]) == 1
    # 20 USD position, +10% minus 2x60bps and fees
    assert 0 < r["trades"][0] < 2.0
    m = _metrics([100.0] + r["equity"], r["trades"])
    assert m["trades"] == 1 and m["win_rate"] == 100.0


def test_hold_benchmark_flat_market_loses_only_costs():
    eq = hold(flat_candles(10), 0, 10)["equity"]
    assert 98 < eq[-1] < 100
