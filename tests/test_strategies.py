from src.solana.strategies import MeanReversion, MomentumBreakout, rsi
from tests.conftest import flat_candles


def test_momentum_breakout_with_volume():
    c = flat_candles(30)
    c.append(dict(t=99, open=1.0, high=1.06, low=1.0, close=1.05, volume=400))
    sig = MomentumBreakout(lookback=24, vol_mult=1.5).signal(c)
    assert sig and sig.strategy == "momentum" and sig.stop_price < 1.05 < sig.tp_price


def test_momentum_needs_volume_and_history():
    c = flat_candles(30)
    c.append(dict(t=99, open=1.0, high=1.06, low=1.0, close=1.05, volume=100))
    assert MomentumBreakout().signal(c) is None
    assert MomentumBreakout().signal(flat_candles(5)) is None


def test_mean_reversion_on_oversold_dip():
    c = flat_candles(40)
    for i, px in enumerate([0.98, 0.96, 0.94, 0.92, 0.90]):
        c.append(dict(t=100 + i, open=px, high=px, low=px, close=px, volume=100))
    sig = MeanReversion().signal(c)
    assert sig and sig.indicators["rsi"] < 30
    assert MeanReversion().signal(flat_candles(40)) is None


def test_rsi_bounds():
    assert rsi([float(i) for i in range(30)]) == 100.0
    assert rsi([float(30 - i) for i in range(30)]) < 1
