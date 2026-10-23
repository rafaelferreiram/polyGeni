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


def test_resample_builds_complete_4h_bars():
    from src.solana.strategies import resample
    hourly = [{"t": 3600 * i, "open": i, "high": i + 0.5, "low": i - 0.5, "close": i + 0.1, "volume": 1} for i in range(1, 10)]
    bars = resample(hourly, 4)                     # hours 1-3 are a partial bucket; 4-7 complete; 8-9 partial
    assert len(bars) == 1 and bars[0]["open"] == 4 and bars[0]["close"] == 7.1 and bars[0]["high"] == 7.5 and bars[0]["volume"] == 4


def test_whale_follower_buys_only_on_strong_whale_buying_in_an_uptrend():
    from src.solana.strategies import WhaleFollow
    up = [{"t": 3600 * i, "open": 100 * 1.002 ** i, "high": 100 * 1.002 ** i * 1.002, "low": 100 * 1.002 ** i * 0.998,
           "close": 100 * 1.002 ** i, "volume": 1} for i in range(300)]
    buying = {"pressure": "comprando", "threshold_usd": 25_000, "1h": {"net_usd": 80_000}}
    w = WhaleFollow()
    assert w.decide(up, {"symbol": "X", "whale_flow": buying}) is not None
    assert w.decide(up, {"symbol": "X", "whale_flow": {**buying, "1h": {"net_usd": 30_000}}}) is None   # not 2x size
    down = [dict(c, close=c["close"] * (0.99 ** i)) for i, c in enumerate(up)]
    assert w.decide(down, {"symbol": "X", "whale_flow": buying}) is None                            # no uptrend


def test_atr_stop_adapts_within_half_and_one_and_a_half_times_the_strategy_stop():
    from src.solana.strategies import atr_stop
    bar = lambda rng: {"open": 100, "high": 100 + rng, "low": 100 - rng, "close": 100, "volume": 1}
    calm, mid, wild = [bar(0.1)] * 30, [bar(2)] * 30, [bar(5)] * 30     # ATR 0.2 / 4 / 10 -> 1.5 ATR = 0.3% / 6% / 15%
    assert [round(atr_stop(c, 0.05), 6) for c in (calm, mid, wild)] == [0.025, 0.06, 0.075]
    assert atr_stop(mid[:10], 0.05) == 0.05                              # too few candles: the strategy's stop


def test_short_break_sells_a_breakdown_in_a_downtrend_and_ignores_uptrends_and_flat_markets():
    from src.solana.strategies import SHORT_BOTS, ShortBreak
    assert "short_break" in SHORT_BOTS                                      # lab only: the real account never shorts
    down = [dict(t=i, open=100 - 0.25 * i, high=100 - 0.25 * i + 0.6, low=100 - 0.25 * i - 0.6, close=100 - 0.25 * i + (0.3 if i % 2 else -0.3), volume=100) for i in range(70)]
    px = down[-1]["close"] - 1.5                                             # closes under the lowest low of the last 20 bars
    down.append(dict(t=71, open=down[-1]["close"], high=down[-1]["close"], low=px - 0.2, close=px, volume=300))
    sig = ShortBreak().signal(down)
    assert sig and sig.indicators["side"] == "short" and sig.stop_price < sig.ref_price < sig.tp_price      # mirror convention, like ShortFade
    up = [dict(c, close=200 - c["close"], low=200 - c["high"], high=200 - c["low"], open=200 - c["open"]) for c in down]
    assert ShortBreak().signal(up) is None and ShortBreak().signal(flat_candles(80)) is None


def _pause_then_breakdown():
    """Downtrend, a flush to ~89.7, then 8 bars pausing near 92 and a close at 90.2: under the last 8 bars' low (90.3 after the
    pause) but still above the lowest low of the last 20 bars (89.7)."""
    bar = lambda i, c: dict(t=i, open=c, high=c + 0.3, low=c - 0.3, close=c, volume=100)
    closes = [120 - 0.5 * i for i in range(52)] + [93.0, 92.0, 91.0, 90.0] + [90.8, 91.6, 91.2, 92.0, 91.4, 92.2, 91.6, 92.4] + [90.2]
    return [bar(i, c) for i, c in enumerate(closes)]


def test_bear_mode_makes_short_break_sell_a_short_range_breakdown_and_default_stays_strict(monkeypatch):
    from src.solana import config as cfg
    from src.solana.strategies import ShortBreak
    candles = _pause_then_breakdown()
    monkeypatch.setattr(cfg, "BEAR_MODE", False)
    assert ShortBreak().signal(candles) is None                       # strict rule: needs a new 20-bar low
    monkeypatch.setattr(cfg, "BEAR_MODE", True)
    monkeypatch.setattr(cfg, "BEAR_BREAK_BARS", 8)
    bear = ShortBreak().signal(candles)
    assert bear and bear.indicators["side"] == "short" and bear.stop_price < bear.ref_price < bear.tp_price
    monkeypatch.setattr(cfg, "BEAR_BREAK_BARS", 20)
    assert ShortBreak().signal(candles) is None                       # the knob really is the lookback


def test_bear_mode_lowers_the_short_fade_rsi_but_never_the_downtrend_requirement(monkeypatch):
    from src.solana import config as cfg
    from src.solana.strategies import ShortFade
    monkeypatch.setattr(cfg, "BEAR_MODE", True)
    up = [dict(t=i, open=100 + 0.5 * i, high=100 + 0.5 * i + 0.3, low=100 + 0.5 * i - 0.3, close=100 + 0.5 * i, volume=100) for i in range(260)]
    assert ShortFade().signal(up) is None            # above its EMA200 (an uptrend): no short, whatever the mode


def test_rebound_buys_the_first_green_bar_after_an_oversold_flush_and_nothing_else():
    from src.solana.strategies import Rebound, RULE_BOTS, FAST_BOTS
    from src.solana import config as cfg
    assert "rebound_15m" in RULE_BOTS and "rebound_15m" in FAST_BOTS and "rebound_15m" not in cfg.LIVE_FAST_BOTS      # lab only: no real money by default
    px = 100.0
    c = []
    for i in range(50):                                                      # a slide that accelerates into a flush: every bar red
        step = 0.996 if i < 46 else 0.985
        px *= step
        c.append(dict(t=i, open=px / step, high=px / step + 0.05, low=px - 0.05, close=px, volume=100))
    c.append(dict(t=51, open=px, high=px * 1.004, low=px * 0.9985, close=px * 1.003, volume=200))      # the first green bar
    sig = Rebound().signal(c)
    assert sig and sig.indicators["rsi"] <= 30 and sig.stop_price < c[-1]["close"] < sig.tp_price
    red = c[:-1] + [dict(c[-1], close=c[-1]["open"] * 0.998)]                # still falling: no entry
    assert Rebound().signal(red) is None and Rebound().signal(flat_candles(60)) is None
