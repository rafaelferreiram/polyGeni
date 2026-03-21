"""
Bitcoin price feed and technical analysis.
Uses Binance public API — no key required.
"""
import httpx
import pandas as pd
import numpy as np
from src.config import BINANCE_BASE


def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 90) -> pd.DataFrame:
    """Fetch OHLCV candles from Binance."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        resp.raise_for_status()
        raw = resp.json()

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df


def fetch_current_price(symbol: str = "BTCUSDT") -> float:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol})
        resp.raise_for_status()
        return float(resp.json()["price"])


def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute RSI, MACD, Bollinger Bands on daily closes."""
    close = df["close"]

    # RSI (14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])

    # MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = float((macd_line - signal_line).iloc[-1])

    # Bollinger Bands (20, 2σ)
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = float((sma20 + 2 * std20).iloc[-1])
    bb_lower = float((sma20 - 2 * std20).iloc[-1])
    bb_mid = float(sma20.iloc[-1])

    # Daily drift and volatility (log returns)
    log_returns = np.log(close / close.shift(1)).dropna()
    daily_drift = float(log_returns.mean())
    daily_vol = float(log_returns.std())

    return {
        "rsi": rsi,
        "macd_hist": macd_hist,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_mid": bb_mid,
        "daily_drift": daily_drift,
        "daily_vol": daily_vol,
        "current_price": float(close.iloc[-1]),
    }


def estimate_probability_above(target_price: float, days: int, indicators: dict) -> float:
    """
    Estimate P(BTC price > target_price in `days` days).
    Uses log-normal distribution parameterised from recent history.
    """
    from scipy.stats import norm

    S = indicators["current_price"]
    mu = indicators["daily_drift"]
    sigma = indicators["daily_vol"]

    if sigma <= 0 or S <= 0 or target_price <= 0:
        return 0.5

    # Log-normal: P(S_T > X) = 1 - N(d)
    # d = (ln(X/S) - mu*T) / (sigma * sqrt(T))
    log_ratio = np.log(target_price / S)
    d = (log_ratio - mu * days) / (sigma * np.sqrt(days))
    prob = float(1 - norm.cdf(d))
    return max(0.01, min(0.99, prob))


def estimate_probability_below(target_price: float, days: int, indicators: dict) -> float:
    return 1.0 - estimate_probability_above(target_price, days, indicators)


def get_signal(indicators: dict) -> str:
    """Returns 'bullish', 'bearish', or 'neutral' based on indicators."""
    score = 0
    rsi = indicators["rsi"]
    if rsi < 35:
        score += 2
    elif rsi < 45:
        score += 1
    elif rsi > 65:
        score -= 2
    elif rsi > 55:
        score -= 1

    if indicators["macd_hist"] > 0:
        score += 1
    else:
        score -= 1

    current = indicators["current_price"]
    if current < indicators["bb_lower"]:
        score += 2
    elif current > indicators["bb_upper"]:
        score -= 2

    if score >= 2:
        return "bullish"
    elif score <= -2:
        return "bearish"
    return "neutral"
