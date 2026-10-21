"""
Deterministic StrategyAgents. Pure functions of closed candles (oldest→newest);
the same code runs live and in the backtest. Each strategy owns a slice of capital.
Candles: dicts with open/high/low/close/volume.
"""
from dataclasses import dataclass, field
from statistics import mean, pstdev


@dataclass
class Signal:
    strategy: str
    ref_price: float
    stop_pct: float
    tp_pct: float
    reason: str
    indicators: dict = field(default_factory=dict)

    @property
    def stop_price(self) -> float:
        return self.ref_price * (1 - self.stop_pct)

    @property
    def tp_price(self) -> float:
        return self.ref_price * (1 + self.tp_pct)


def rsi(closes: list[float], period: int = 14) -> float:
    diffs = [b - a for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    gains = sum(d for d in diffs if d > 0) / period
    losses = -sum(d for d in diffs if d < 0) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


@dataclass
class MomentumBreakout:
    """Close breaks the prior N-bar high with volume confirmation."""
    lookback: int = 24
    vol_mult: float = 1.5
    stop_pct: float = 0.06
    tp_pct: float = 0.12
    capital_pct: float = 0.5
    name: str = "momentum"

    def signal(self, candles: list[dict]) -> Signal | None:
        if len(candles) < self.lookback + 1:
            return None
        last, prior = candles[-1], candles[-self.lookback - 1:-1]
        hi = max(c["high"] for c in prior)
        avg_vol = mean(c["volume"] for c in prior)
        if last["close"] > hi and avg_vol > 0 and last["volume"] > self.vol_mult * avg_vol:
            return Signal(self.name, last["close"], self.stop_pct, self.tp_pct,
                          f"close {last['close']:.6g} > {self.lookback}-bar high {hi:.6g}, "
                          f"vol x{last['volume'] / avg_vol:.1f}",
                          {"prior_high": hi, "vol_ratio": last["volume"] / avg_vol})
        return None


@dataclass
class MeanReversion:
    """Oversold dip: close below lower Bollinger band and RSI low."""
    period: int = 20
    k: float = 2.0
    rsi_period: int = 14
    rsi_max: float = 30
    stop_pct: float = 0.05
    tp_pct: float = 0.06
    capital_pct: float = 0.5
    name: str = "mean_reversion"

    def signal(self, candles: list[dict]) -> Signal | None:
        if len(candles) < max(self.period, self.rsi_period + 1) + 1:
            return None
        closes = [c["close"] for c in candles]
        window = closes[-self.period:]
        lower = mean(window) - self.k * pstdev(window)
        r = rsi(closes, self.rsi_period)
        if closes[-1] < lower and r < self.rsi_max:
            return Signal(self.name, closes[-1], self.stop_pct, self.tp_pct,
                          f"close {closes[-1]:.6g} < lower band {lower:.6g}, RSI {r:.0f}",
                          {"lower_band": lower, "rsi": r})
        return None


def default_strategies(names: list[str] | None = None) -> list:
    """Enabled by evidence: walk-forward OOS (reports/, 2026-10-21) — mean-reversion +11.2% on Hyperliquid 1h and
    +4.1% on Solana tokens; momentum lost on every venue/timeframe, so it is off unless PAPER_STRATEGIES enables it."""
    from src.solana import config as cfg
    names = names or cfg.PAPER_STRATEGIES
    available = {"mean_reversion": MeanReversion, "momentum": MomentumBreakout,
                 "mean_reversion_atr": MeanReversionATR, "trend": TrendFollow}
    share = 1.0 / len(names)
    return [available[n](capital_pct=share) for n in names]


@dataclass
class RegimeFiltered:
    """Deterministic regime gate: only take the inner strategy's longs while close > SMA(n)."""
    inner: object
    n: int = 200

    @property
    def name(self):
        return self.inner.name

    @property
    def capital_pct(self):
        return self.inner.capital_pct

    def signal(self, candles: list[dict]) -> Signal | None:
        if len(candles) < self.n:
            return None
        if candles[-1]["close"] <= mean(c["close"] for c in candles[-self.n:]):
            return None
        return self.inner.signal(candles)


# ─── volatility-aware candidates (v2) ─────────────────────────────────────────

def ema(values: list[float], n: int) -> float:
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles: list[dict], n: int = 14) -> float:
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(candles[-n - 1:-1], candles[-n:])]
    return sum(trs) / len(trs) if trs else 0.0


def _atr_stops(candles, stop_atr, tp_atr) -> tuple[float, float, float]:
    a, close = atr(candles), candles[-1]["close"]
    stop = min(max(stop_atr * a / close, 0.005), 0.15)
    tp = min(max(tp_atr * a / close, 0.005), 0.30)
    return stop, tp, a


def _crowded(candles, max_funding) -> bool:
    """Skip longs when hourly funding says the long side is crowded (unknown funding = not crowded)."""
    f = candles[-1].get("funding")
    return f is not None and f > max_funding


@dataclass
class MeanReversionATR:
    """Oversold dip (below lower Bollinger + low RSI), optionally only in an uptrend (close > EMA),
    with stop/target sized by ATR so they fit each coin's volatility; skips crowded longs."""
    period: int = 20
    k: float = 2.0
    rsi_period: int = 14
    rsi_max: float = 30
    trend_ema: int = 200
    trend: bool = True
    stop_atr: float = 2.0
    tp_atr: float = 2.5
    max_funding: float = 0.0001
    capital_pct: float = 1.0
    name: str = "mean_reversion_atr"

    def signal(self, candles: list[dict]) -> Signal | None:
        need = max(self.period, self.rsi_period + 1, self.trend_ema if self.trend else 0) + 1
        if len(candles) < need:
            return None
        closes = [c["close"] for c in candles]
        window = closes[-self.period:]
        lower = mean(window) - self.k * pstdev(window)
        r = rsi(closes, self.rsi_period)
        if not (closes[-1] < lower and r < self.rsi_max) or _crowded(candles, self.max_funding):
            return None
        trend = ema(closes[-3 * self.trend_ema:], self.trend_ema) if self.trend else None
        if trend is not None and closes[-1] <= trend:
            return None
        stop, tp, a = _atr_stops(candles, self.stop_atr, self.tp_atr)
        return Signal(self.name, closes[-1], stop, tp,
                      f"dip below band {lower:.6g}, RSI {r:.0f}" + (f", uptrend (EMA{self.trend_ema} {trend:.6g})" if trend else ""),
                      {"rsi": r, "atr": a, "lower_band": lower, "ema_trend": trend})


@dataclass
class TrendFollow:
    """Time-series momentum: close > EMA fast > EMA slow and a fresh N-bar breakout; ATR stop/target."""
    fast: int = 50
    slow: int = 200
    breakout: int = 24
    stop_atr: float = 2.0
    tp_atr: float = 4.0
    max_funding: float = 0.0001
    capital_pct: float = 1.0
    name: str = "trend"

    def signal(self, candles: list[dict]) -> Signal | None:
        if len(candles) < self.slow + 1:
            return None
        closes = [c["close"] for c in candles]
        f, s = ema(closes[-3 * self.fast:], self.fast), ema(closes[-3 * self.slow:], self.slow)
        prior_high = max(c["high"] for c in candles[-self.breakout - 1:-1])
        if not (closes[-1] > f > s and closes[-1] > prior_high) or _crowded(candles, self.max_funding):
            return None
        stop, tp, a = _atr_stops(candles, self.stop_atr, self.tp_atr)
        return Signal(self.name, closes[-1], stop, tp,
                      f"uptrend (EMA{self.fast} {f:.6g} > EMA{self.slow} {s:.6g}) and {self.breakout}h breakout",
                      {"ema_fast": f, "ema_slow": s, "atr": a, "prior_high": prior_high})


class TrendStrong(TrendFollow):
    """Day-trade trend following: a 24h breakout in a strong trend (50h EMA up > min_slope in 24h, price above its
    level of 3 days ago), target 3 ATR, stop 2 ATR, and the position is closed after max_hold_h (24h) at the latest.
    Per-coin backtest on 29 coins, 208 days (2026-10-21): target 3 ATR + 24h exit + 3-day filter beat the old
    4 ATR / 72h version in both halves (+$23.9 vs +$2.2 and +$45.7 vs +$42.6 per $20 positions; win rate 49% vs 36–42%);
    24h also beat 12h, 36h and 48h, so it is not a lucky parameter."""
    min_slope: float = 0.01
    name: str = "trend"
    max_hold_h: float = 24

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if "tp_atr" not in kw:
            self.tp_atr = 3.0

    def signal(self, candles: list[dict]) -> Signal | None:
        closes = [c["close"] for c in candles]
        if (len(closes) < self.slow + 25 or ema(closes[-150:], self.fast) / ema(closes[-174:-24], self.fast) - 1 < self.min_slope
                or closes[-1] <= closes[-73]):
            return None
        return super().signal(candles)


# deterministic bots (no LLM, no API cost): model name -> strategy
RULE_BOTS = {"rules": MeanReversionATR, "trend": TrendStrong}


def features(candles: list[dict]) -> dict:
    """Indicators handed to the LLM bots so they reason on the same facts as the rules (no raw-candle guessing)."""
    if len(candles) < 30:
        return {}
    closes = [c["close"] for c in candles]
    a = atr(candles)
    e50 = ema(closes[-150:], 50)
    e200 = ema(closes[-600:], 200) if len(closes) >= 200 else None
    window = closes[-20:]
    lower = mean(window) - 2 * pstdev(window)
    rules = MeanReversionATR().signal(candles)
    return {
        "price": closes[-1], "rsi14": round(rsi(closes), 1), "atr_pct": round(a / closes[-1] * 100, 3),
        "ema50": e50, "ema200": e200, "uptrend": bool(e200 and closes[-1] > e200 and e50 > e200),
        "below_lower_band": closes[-1] < lower, "change_24h_pct": round((closes[-1] / closes[-25] - 1) * 100, 2) if len(closes) > 25 else None,
        "funding_1h": candles[-1].get("funding"),
        "suggested_stop_pct": round(min(max(2 * a / closes[-1], 0.005), 0.15), 4),
        "suggested_tp_pct": round(min(max(2.5 * a / closes[-1], 0.005), 0.30), 4),
        "rules_signal": rules.reason if rules else None,
    }
