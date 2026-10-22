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


def atr_stop(candles: list[dict], stop_pct: float) -> float:
    """Stop distance from the coin's own volatility: ATR_STOP_MULT × ATR(14) of the signal's candles (1h for the
    accounts, 15m for the 15m twins), bounded to [0.5×, 1.5×] the strategy's stop so it adapts but never runs away.
    Sizing stays $10: a risk budget of $0.60 / stop distance would allow ≥ $10 for any stop ≤ 6%, and Hyperliquid's
    minimum order is $10 anyway — so only the stop adapts and the risk per trade is $10 × stop."""
    from src.solana import config as cfg
    if not cfg.ATR_STOP_MULT or len(candles) < 15 or not candles[-1]["close"]:
        return stop_pct
    return min(max(cfg.ATR_STOP_MULT * atr(candles) / candles[-1]["close"], stop_pct * 0.5), stop_pct * 1.5)


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
        relax = getattr(self, "relax", 0)                 # daily quota: titular behind its minimum of trades
        lower = mean(window) - (self.k - 0.25 * relax) * pstdev(window)
        r = rsi(closes, self.rsi_period)
        if not (closes[-1] < lower and r < self.rsi_max + 5 * relax) or _crowded(candles, self.max_funding):
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
        lookback = max(6, self.breakout - 6 * getattr(self, "relax", 0))     # daily quota: shorter breakout window
        prior_high = max(c["high"] for c in candles[-lookback - 1:-1])
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
        if (len(closes) < self.slow + 25 or ema(closes[-150:], self.fast) / ema(closes[-174:-24], self.fast) - 1 < self.min_slope * (1 - 0.4 * getattr(self, "relax", 0))
                or closes[-1] <= closes[-73]):
            return None
        return super().signal(candles)


def resample(candles: list[dict], hours: int = 4) -> list[dict]:
    """1h candles -> N-hour candles (aligned to UTC multiples of N hours; a partial first bucket is dropped)."""
    out, cur = [], None
    for c in candles:
        bucket = c["t"] // (hours * 3600)
        if cur is None or cur["b"] != bucket:
            if cur and cur["n"] == hours:
                out.append({k: cur[k] for k in ("t", "open", "high", "low", "close", "volume")})
            cur = {"b": bucket, "n": 0, "t": bucket * hours * 3600, "open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "volume": 0.0}
        cur.update(high=max(cur["high"], c["high"]), low=min(cur["low"], c["low"]), close=c["close"],
                   volume=cur["volume"] + c.get("volume", 0), n=cur["n"] + 1)
    if cur and cur["n"] == hours:
        out.append({k: cur[k] for k in ("t", "open", "high", "low", "close", "volume")})
    return out


class Trend4h:
    """Bigger swings: trend following on 4h candles built from the 1h feed (EMA20 > EMA50, 24h breakout, stop 2 ATR,
    target 3 ATR of the 4h bars, closed within 48h). Lab bot until it proves itself; an earlier 4h study showed ~7x
    the profit per trade of 1h with far fewer trades."""
    name = "trend4h"
    capital_pct = 1.0
    max_hold_h = 48

    def signal(self, candles: list[dict]) -> Signal | None:
        bars = resample(candles, 4)
        sig = TrendFollow(fast=20, slow=50, breakout=6, stop_atr=2.0, tp_atr=3.0).signal(bars)
        if sig:
            sig.strategy = self.name
            sig.reason = "4h: " + sig.reason.replace("24h breakout", "6-bar (24h) breakout")
        return sig


class WhaleFollow:
    """Follows the money: in an uptrend (price above EMA200 and EMA50), buy when whales were clearly net buyers in
    the last hour (≥ 2x the coin's whale size, pressure 'comprando'). Stop 2 ATR, target 3 ATR, closed within 24h.
    No history exists to backtest whale flow, so this bot lives in the lab until it has a record."""
    name = "whales"
    capital_pct = 1.0
    max_hold_h = 24
    enabled = True

    def __init__(self):
        self.last: dict[str, dict] = {}

    def signal(self, candles):                       # needs the whale context: only via decide()
        return None

    def decide(self, candles: list[dict], context: dict) -> Signal | None:
        market, wf = context.get("symbol", "?"), context.get("whale_flow") or {}
        h = wf.get("1h") or {}
        closes = [c["close"] for c in candles]
        if len(closes) < 210:
            return None
        up = closes[-1] > ema(closes[-600:], 200) and closes[-1] > ema(closes[-150:], 50)
        need = 2 - 0.5 * getattr(self, "relax", 0)          # daily quota: a bit less whale buying is enough
        strong = wf.get("pressure") == "comprando" and h.get("net_usd", 0) >= need * wf.get("threshold_usd", 1e12)
        self.last[market] = {"action": "BUY" if up and strong else "HOLD", "model": self.name, "t": candles[-1]["t"],
                             "reason": (f"baleias compraram ${h.get('net_usd', 0):,.0f} líquidos em 1h numa alta" if up and strong
                                        else "sem compra forte de baleias numa alta")}
        if not (up and strong) or _crowded(candles, 0.0001):
            return None
        stop, tp, a = _atr_stops(candles, 2.0, 3.0)
        return Signal(self.name, closes[-1], stop, tp, self.last[market]["reason"], {"atr": a, "whale_net_1h": h.get("net_usd")})


class WhaleFollowClone(WhaleFollow):
    """'Dudu': an identical clone of Evair (same rules), at the user's request — two bots on the same signal show how
    much of a record is luck. On the real account one position per coin still applies (no doubled bet)."""
    name = "whales2"


class CopyTop(WhaleFollow):
    """Copies the best traders of Hyperliquid, at our own (small, proportional) size: buy a coin when at least 2 of
    the top 10 are long, at least one of them entered in the last hour, more of them are long than short, and the coin
    is in an uptrend. Stop 2 ATR, target 3 ATR, closed within 24h. Lab bot until it proves itself."""
    name = "copy"

    def decide(self, candles: list[dict], context: dict) -> Signal | None:
        market, t = context.get("symbol", "?"), context.get("top_traders") or {}
        closes = [c["close"] for c in candles]
        if len(closes) < 210:
            return None
        up = closes[-1] > ema(closes[-600:], 200)
        fresh = t.get("new_longs_1h", 0) >= 1 or getattr(self, "relax", 0) >= 1      # daily quota: no fresh entry needed
        crowd = t.get("long", 0) >= 2 and fresh and t.get("long", 0) > t.get("short", 0)
        self.last[market] = {"action": "BUY" if up and crowd else "HOLD", "model": self.name, "t": candles[-1]["t"],
                             "reason": (f"{t.get('long')} dos top {t.get('of')} traders comprados, {t.get('new_longs_1h')} entraram na última hora"
                                        if up and crowd else f"top traders: {t.get('long', 0)} comprados, {t.get('new_longs_1h', 0)} novas entradas")}
        if not (up and crowd) or _crowded(candles, 0.0001):
            return None
        stop, tp, a = _atr_stops(candles, 2.0, 3.0)
        return Signal(self.name, closes[-1], stop, tp, self.last[market]["reason"], {"atr": a, "top_longs": t.get("long")})


class ShortFade:
    """Lab-only short: in a downtrend (price below EMA200), sell a rally that overshoots (above the upper Bollinger
    band with RSI > 70); stop 2 ATR above, target 3 ATR below, closed within 12h. Per-coin backtest on 29 coins,
    208 days (2026-10-21): about break-even (+$6.9 then −$0.02 per $20 positions) — every other short variant lost,
    because crypto drifted up — so it only trades paper money in the lab, where it is measured live."""
    name = "short"
    capital_pct = 1.0
    max_hold_h = 12

    def signal(self, candles: list[dict]) -> Signal | None:
        closes = [c["close"] for c in candles]
        if len(closes) < 230:
            return None
        window = closes[-20:]
        upper = mean(window) + 2 * pstdev(window)
        r = rsi(closes)
        if not (closes[-1] < ema(closes[-600:], 200) and closes[-1] > upper and r > 70):
            return None
        stop, tp, a = _atr_stops(candles, 2.0, 3.0)
        # the engine treats a short as a long on the mirror price, so stop/target stay "below/above" the entry
        return Signal(self.name, closes[-1], stop, tp, f"short: alta exagerada numa baixa (RSI {r:.0f}, acima da banda {upper:.6g})",
                      {"rsi": r, "atr": a, "upper_band": upper, "side": "short"})


def fast(cls, label: str, hold_h: float = 6):
    """A 15-minute twin of a rule bot for the lab: same rules counted in 15m bars (so EMAs, breakouts and ATR stops
    are 4x shorter), positions closed within hold_h. Same green light as every bot to reach real money."""
    def init(self, *a, **kw):
        cls.__init__(self, *a, **kw)
        self.name, self.max_hold_h = label, hold_h          # dataclass parents set their own name on the instance
    return type(f"{cls.__name__}15m", (cls,), {"__init__": init, "name": label, "max_hold_h": hold_h, "timeframe": "15m"})


Trend4h15 = fast(type("Trend15", (TrendFollow,), {"__init__": lambda s: TrendFollow.__init__(s, fast=20, slow=50, breakout=6, stop_atr=2.0, tp_atr=3.0)}),
                 "trend4h_15m")
WhaleFollow15 = fast(WhaleFollow, "whales_15m")
CopyTop15 = fast(CopyTop, "copy_15m")


# deterministic bots (no LLM, no API cost): model name -> strategy
RULE_BOTS = {"rules": MeanReversionATR, "trend": TrendStrong, "trend4h": Trend4h, "whales": WhaleFollow, "whales2": WhaleFollowClone, "copy": CopyTop,
             "short": ShortFade, "trend4h_15m": Trend4h15, "whales_15m": WhaleFollow15, "copy_15m": CopyTop15}
SHORT_BOTS = {"short"}          # lab only: the real account (and its simulation) never short
FAST_BOTS = {"trend4h_15m", "whales_15m", "copy_15m"}      # 15-minute twins: quick trades, they decide on 15m candles
LAB_ONLY_BOTS = SHORT_BOTS


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
