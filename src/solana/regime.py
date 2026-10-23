"""
Market regime classifier — P1, OBSERVATIONAL first. Deterministic and free: BTC 1h candles the engines already
fetch (shared hourly cache) plus the market breadth already computed each cycle. The BTC-50h filter remains the
only gate; this records what a richer classifier says and tags every entry with it, so its efficacy can be
compared on real trades per regime BEFORE any gate is replaced (the master plan forbids swapping the filter
without that comparison).
"""
import logging
from datetime import datetime, timezone

from src.solana import config as cfg

logger = logging.getLogger("solana.regime")

VERSION = 1
# how much ATR(14) must move against its own recent base to call a volatility regime
VOL_EXPANSION = 1.5
VOL_CONTRACTION = 0.67
SIDEWAYS_BAND = 0.01          # price within ±1% of EMA200 and a flat EMA50 slope = ranging
STRONG_SLOPE = 0.004          # same bar-over-bar EMA50 slope TrendStrong uses


def _true_ranges(candles: list[dict]) -> list[float]:
    out, prev = [], None
    for c in candles:
        hi, lo, cl = c.get("high", c["close"]), c.get("low", c["close"]), c["close"]
        tr = max(hi - lo, abs(hi - prev), abs(lo - prev)) if prev is not None else hi - lo
        out.append(tr)
        prev = cl
    return out


def classify(candles: list[dict], breadth: dict | None = None) -> dict:
    """Regime from CLOSED 1h BTC candles (oldest first) + optional market breadth ({'n', 'uptrend'}).
    Returns state, vol_state, a heuristic confidence (NOT calibrated on outcomes yet — that is the point of the
    observational phase), horizon, and the timestamp of the data it saw."""
    from src.solana.strategies import ema
    closes = [c["close"] for c in candles]
    asof = datetime.fromtimestamp(candles[-1]["t"], timezone.utc).isoformat(timespec="seconds") if candles else None
    out = {"version": VERSION, "horizon": "1h", "asof": asof, "confidence": None, "calibrated": False}
    if len(closes) < 60:
        return {**out, "state": "sem dados", "vol_state": "sem dados", "detail": {"bars": len(closes)}}
    px = closes[-1]
    e50 = ema(closes[-150:], 50)
    e200 = ema(closes[-600:], 200) if len(closes) >= 200 else None
    slope = e50 / ema(closes[-174:-24], 50) - 1 if len(closes) >= 174 else 0.0
    trs = _true_ranges(candles[-120:])
    atr_now = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    atr_ref = sum(trs[:-14]) / len(trs[:-14]) if len(trs) > 28 else None
    vol_ratio = atr_now / atr_ref if atr_now and atr_ref else None
    vol_state = ("sem dados" if vol_ratio is None else
                 "expansão" if vol_ratio > VOL_EXPANSION else
                 "contração" if vol_ratio < VOL_CONTRACTION else "normal")
    frac = breadth["uptrend"] / breadth["n"] if breadth and breadth.get("n") else None
    votes = 0                                    # how many independent reads agree with the called direction
    if e200 is None:
        state = "sem dados"
    elif abs(px / e200 - 1) < SIDEWAYS_BAND and abs(slope) < STRONG_SLOPE / 4:
        state, votes = "lateral", 2
    elif px > e200:
        strong = e50 > e200 and slope >= STRONG_SLOPE and (frac is None or frac >= 0.5)
        state = "alta forte" if strong else "alta fraca"
        votes = 1 + (e50 > e200) + (slope >= STRONG_SLOPE) + (frac is not None and frac >= 0.5)
    else:
        strong = e50 < e200 and slope <= -STRONG_SLOPE and (frac is None or frac <= 0.3)
        state = "baixa forte" if strong else "baixa fraca"
        votes = 1 + (e50 < e200) + (slope <= -STRONG_SLOPE) + (frac is not None and frac <= 0.3)
    detail = {"px": px, "ema50": round(e50, 2), "ema200": round(e200, 2) if e200 else None,
              "slope_50": round(slope, 5), "vol_ratio": round(vol_ratio, 3) if vol_ratio else None,
              "breadth_uptrend": frac if frac is None else round(frac, 2)}
    return {**out, "state": state, "vol_state": vol_state, "btc_above_50h": px > e50,
            "confidence": round(min(0.5 + 0.1 * votes, 0.9), 2) if state != "sem dados" else None, "detail": detail}


def current(breadth: dict | None = None) -> dict | None:
    """The regime right now from live BTC candles (shared hourly cache). None when off or data unavailable:
    the classifier must never block trading or the risk engine."""
    if not cfg.REGIME_ENABLED:
        return None
    try:
        from src.solana import hyperliquid as hl
        return classify(hl.candles("BTC")[:-1], breadth)
    except Exception as e:
        logger.warning(f"regime unavailable: {e}")
        return None
