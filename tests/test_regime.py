"""Regime classifier: deterministic, observational, never blocks trading, tags every entry."""
import time

from src.solana import regime


def _candles(closes, spread=0.001):
    now = int(time.time()) // 3600 * 3600
    n = len(closes)
    return [dict(t=now - (n - 1 - i) * 3600, open=c, high=c * (1 + spread), low=c * (1 - spread), close=c, volume=100.0)
            for i, c in enumerate(closes)]


def test_classifies_trends_sideways_and_volatility():
    bull = regime.classify(_candles([100 * 1.005 ** i for i in range(250)]))
    assert bull["state"] == "alta forte" and bull["btc_above_50h"] and 0 < bull["confidence"] <= 0.9
    assert not bull["calibrated"]                        # honest: heuristic until measured on real trades
    bear = regime.classify(_candles([100 * 0.995 ** i for i in range(250)]))
    assert bear["state"] == "baixa forte" and not bear["btc_above_50h"]
    flat = regime.classify(_candles([100.0] * 250))
    assert flat["state"] == "lateral"
    # volatility expansion: quiet for 100+ bars, then wide ranges — price still flat
    quiet = _candles([100.0] * 236, spread=0.001) + _candles([100.0] * 14, spread=0.03)[-14:]
    assert regime.classify(quiet)["vol_state"] == "expansão"
    assert regime.classify(_candles([100.0] * 30))["state"] == "sem dados"       # too little history


def test_breadth_downgrades_a_lonely_btc_rally():
    closes = [100 * 1.005 ** i for i in range(250)]
    assert regime.classify(_candles(closes), {"n": 10, "uptrend": 8})["state"] == "alta forte"
    assert regime.classify(_candles(closes), {"n": 10, "uptrend": 1})["state"] == "alta fraca"


def test_current_never_raises_and_respects_the_switch(monkeypatch):
    from src.solana import config as cfg, hyperliquid as hl
    monkeypatch.setattr(cfg, "REGIME_ENABLED", False)
    assert regime.current() is None
    monkeypatch.setattr(cfg, "REGIME_ENABLED", True)
    monkeypatch.setattr(hl, "candles", lambda coin, interval="1h": (_ for _ in ()).throw(RuntimeError("down")))
    assert regime.current() is None                      # data problems never block the cycle
    monkeypatch.setattr(hl, "candles", lambda coin, interval="1h": _candles([100 * 1.005 ** i for i in range(250)]))
    assert regime.current({"n": 4, "uptrend": 4})["state"] == "alta forte"
