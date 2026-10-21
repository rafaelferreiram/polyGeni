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


def _trend_candles(n=260, step=0.002, vol=100.0):
    out, px = [], 1.0
    for i in range(n):
        px *= 1 + step
        out.append(dict(t=i * 3600, open=px / (1 + step), high=px * 1.002, low=px / (1 + step) * 0.998, close=px, volume=vol))
    return out


def test_atr_and_ema_basics():
    from src.solana.strategies import atr, ema
    c = flat_candles(30)
    assert abs(atr(c) - 0.002) < 1e-9 and ema([1.0] * 50, 10) == 1.0


def test_trend_follow_rides_uptrend_and_respects_funding():
    from src.solana.strategies import TrendFollow
    c = _trend_candles()
    c[-1] = dict(c[-1], close=c[-1]["close"] * 1.01, high=c[-1]["close"] * 1.012)   # fresh breakout bar
    sig = TrendFollow().signal(c)
    assert sig and 0.005 <= sig.stop_pct <= 0.15 and sig.tp_pct > sig.stop_pct
    c[-1]["funding"] = 0.001                               # crowded longs → skip
    assert TrendFollow().signal(c) is None
    assert TrendFollow().signal(flat_candles(260)) is None


def test_mean_reversion_atr_needs_uptrend_when_trend_filter_on():
    from src.solana.strategies import MeanReversionATR
    c = _trend_candles(240)
    for i, f in enumerate([0.985, 0.975, 0.965]):             # sharp dip inside an uptrend
        last = c[-1]["close"]
        c.append(dict(t=c[-1]["t"] + 3600, open=last, high=last, low=last * f, close=last * f, volume=100))
    assert MeanReversionATR(trend_ema=100).signal(c) is not None
    flat_then_dip = flat_candles(240)
    for f in (0.985, 0.975, 0.965):
        last = flat_then_dip[-1]["close"]
        flat_then_dip.append(dict(t=flat_then_dip[-1]["t"] + 3600, open=last, high=last, low=last * f, close=last * f, volume=100))
    assert MeanReversionATR(trend_ema=100).signal(flat_then_dip) is None   # no uptrend → no trade
    assert MeanReversionATR(trend=False, trend_ema=100).signal(flat_then_dip) is not None
