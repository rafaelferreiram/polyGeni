"""
Bitcoin market analyzer.
Fetches BTC price + indicators and estimates probabilities for Polymarket markets.
"""
import re
from datetime import datetime
from src.feeds.bitcoin import fetch_klines, fetch_current_price, compute_indicators, estimate_probability_above, estimate_probability_below, get_signal
from src.analysis.kelly import edge, compute_bet_size
from src.polymarket.client import get_midpoint
from src.polymarket.gamma import get_market_tokens
from src.config import BOT_MIN_EDGE


def _extract_price_target(question: str) -> float | None:
    """Try to extract a USD price target from the market question."""
    patterns = [
        r"\$([0-9,]+(?:\.[0-9]+)?)[kK]?",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(?:USD|USDC|dollars?)",
    ]
    for pat in patterns:
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "")
            val = float(raw)
            if "k" in m.group(0).lower():
                val *= 1000
            if val > 1000:  # sanity: BTC price is always >1000
                return val
    return None


def _estimate_days_to_resolution(end_date_str: str | None) -> int:
    if not end_date_str:
        return 14
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta = (end.replace(tzinfo=None) - datetime.utcnow()).days
        return max(1, delta)
    except Exception:
        return 14


def analyze_market(market: dict, indicators: dict | None = None) -> dict | None:
    """
    Analyze a single Bitcoin Polymarket market.
    Returns opportunity dict or None if no edge.
    """
    question = market.get("question", "")
    condition_id = market.get("conditionId") or market.get("id")

    if indicators is None:
        try:
            df = fetch_klines()
            indicators = compute_indicators(df)
        except Exception:
            return None

    target = _extract_price_target(question)
    if target is None:
        return None

    end_date = market.get("endDate") or market.get("end_date_iso")
    days = _estimate_days_to_resolution(end_date)

    # Determine direction from question
    question_lower = question.lower()
    is_above = any(w in question_lower for w in ["above", "over", "exceed", "reach", "hit", "at least"])
    is_below = any(w in question_lower for w in ["below", "under", "less than", "drop", "fall"])

    if is_above:
        our_prob = estimate_probability_above(target, days, indicators)
        side = "YES"
    elif is_below:
        our_prob = estimate_probability_below(target, days, indicators)
        side = "YES"
    else:
        # Default: assume "above" framing
        our_prob = estimate_probability_above(target, days, indicators)
        side = "YES"

    # Get market implied probability
    yes_token, _ = get_market_tokens(market)
    if not yes_token:
        return None

    market_prob = get_midpoint(yes_token)
    if market_prob is None or market_prob <= 0:
        return None

    # If our probability says NO is the edge, flip
    no_edge = (1 - our_prob) - (1 - market_prob)
    yes_edge = our_prob - market_prob

    if abs(no_edge) > abs(yes_edge) and no_edge > yes_edge:
        side = "NO"
        final_our_prob = 1 - our_prob
        final_market_prob = 1 - market_prob
    else:
        final_our_prob = our_prob
        final_market_prob = market_prob

    calc_edge = edge(final_our_prob, final_market_prob)
    if calc_edge < BOT_MIN_EDGE:
        return None

    signal = get_signal(indicators)
    reasoning = (
        f"BTC at ${indicators['current_price']:,.0f}. "
        f"Target: ${target:,.0f} in {days}d. "
        f"Model prob: {final_our_prob:.1%}, market: {final_market_prob:.1%}. "
        f"RSI={indicators['rsi']:.1f}, signal={signal}."
    )

    return {
        "market_id": condition_id,
        "question": question,
        "category": "bitcoin",
        "recommended_side": side,
        "market_prob": round(final_market_prob, 4),
        "estimated_prob": round(final_our_prob, 4),
        "edge": round(calc_edge, 4),
        "yes_token_id": yes_token,
        "reasoning": reasoning,
    }


def scan_bitcoin_markets(markets: list[dict], bankroll: float) -> list[dict]:
    """Scan all Bitcoin markets and return opportunities sorted by edge."""
    try:
        df = fetch_klines()
        indicators = compute_indicators(df)
    except Exception:
        return []

    opportunities = []
    for market in markets:
        result = analyze_market(market, indicators)
        if result:
            price = result["market_prob"] if result["recommended_side"] == "YES" else (1 - result["market_prob"])
            result["kelly_size_usdc"] = compute_bet_size(result["estimated_prob"], price, bankroll)
            opportunities.append(result)

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
