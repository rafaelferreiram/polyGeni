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


def default_strategies() -> list:
    return [MomentumBreakout(), MeanReversion()]
