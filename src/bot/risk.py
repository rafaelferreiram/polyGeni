"""
Risk management rules applied before every trade.
"""
from src.config import BOT_MAX_POSITION_PCT, BOT_BUDGET_USDC


MAX_OPEN_POSITIONS = 3
MIN_BET_USDC = 1.0


def check_trade(
    opportunity: dict,
    bankroll: float,
    open_position_count: int,
) -> tuple[bool, str]:
    """
    Returns (approved, reason).
    Checks all risk rules before allowing a trade.
    """
    size = opportunity.get("kelly_size_usdc", 0)

    if open_position_count >= MAX_OPEN_POSITIONS:
        return False, f"Max open positions reached ({MAX_OPEN_POSITIONS})"

    if size < MIN_BET_USDC:
        return False, f"Bet size ${size:.2f} below minimum ${MIN_BET_USDC}"

    if bankroll < MIN_BET_USDC:
        return False, f"Bankroll ${bankroll:.2f} too low"

    max_allowed = bankroll * BOT_MAX_POSITION_PCT
    if size > max_allowed:
        opportunity["kelly_size_usdc"] = round(max_allowed, 2)

    if opportunity["edge"] < 0.03:
        return False, f"Edge {opportunity['edge']:.1%} too thin after recheck"

    return True, "approved"
