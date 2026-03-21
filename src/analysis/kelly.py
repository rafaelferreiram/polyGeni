"""
Kelly Criterion position sizing.
Uses fractional Kelly (conservative) to protect the small bankroll.
"""
from src.config import BOT_KELLY_FRACTION, BOT_MAX_POSITION_PCT, BOT_BUDGET_USDC


def kelly_fraction(prob: float, price: float) -> float:
    """
    Standard Kelly formula for binary bet.
    prob:  our estimated probability of winning
    price: cost per share (= implied market probability)
    b:     net odds on a $1 bet = (1 - price) / price
    f* = (b*p - q) / b  where q = 1-p
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - prob
    f = (b * prob - q) / b
    return max(0.0, f)


def compute_bet_size(
    prob: float,
    price: float,
    bankroll: float,
    kelly_fraction_override: float | None = None,
    max_pct_override: float | None = None,
) -> float:
    """
    Returns recommended USDC bet size.
    Applies fractional Kelly and hard position cap.
    """
    kf = kelly_fraction_override if kelly_fraction_override is not None else BOT_KELLY_FRACTION
    max_pct = max_pct_override if max_pct_override is not None else BOT_MAX_POSITION_PCT

    raw_fraction = kelly_fraction(prob, price)
    fractional = raw_fraction * kf  # e.g. 1/4 Kelly

    max_usdc = bankroll * max_pct
    bet_usdc = min(bankroll * fractional, max_usdc)

    return round(max(0.0, bet_usdc), 2)


def expected_value(prob: float, price: float) -> float:
    """EV per dollar bet = prob * (1/price) - 1"""
    if price <= 0:
        return -1.0
    return prob * (1.0 / price) - 1.0


def edge(prob: float, price: float) -> float:
    """Edge = our probability - market probability."""
    return prob - price
