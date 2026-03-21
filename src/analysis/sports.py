"""
Sports market analyzer.
Compares bookmaker consensus odds with Polymarket implied probabilities.
"""
from src.feeds.sports import fetch_all_odds, match_event_to_market
from src.feeds.news import analyze_news_for_market
from src.analysis.kelly import edge, compute_bet_size
from src.polymarket.gamma import get_market_tokens, get_market_price
from src.config import BOT_MIN_EDGE

# Extra edge buffer for sports (less predictable)
SPORTS_MIN_EDGE = max(BOT_MIN_EDGE, 0.07)


def analyze_market(market: dict, all_odds: list[dict] | None = None) -> dict | None:
    question = market.get("question", "")
    condition_id = market.get("conditionId") or market.get("id")

    if all_odds is None:
        all_odds = fetch_all_odds()

    # Try to match to a bookmaker event
    bookmaker_prob = None
    for event in all_odds:
        prob = match_event_to_market(event, question)
        if prob is not None:
            bookmaker_prob = prob
            break

    if bookmaker_prob is None:
        return None

    yes_token, no_token = get_market_tokens(market)
    if not yes_token:
        return None

    yes_price, _ = get_market_price(market)
    market_prob = yes_price
    if market_prob is None or market_prob <= 0:
        return None

    # Check both YES and NO edges
    yes_edge = bookmaker_prob - market_prob
    no_edge = (1 - bookmaker_prob) - (1 - market_prob)

    # Enhance with news sentiment
    news = analyze_news_for_market(question)
    sentiment_boost = news["sentiment"] * 0.03  # small nudge

    if abs(no_edge) > abs(yes_edge):
        side = "NO"
        our_prob = 1 - bookmaker_prob + sentiment_boost
        mkt_prob = 1 - market_prob
    else:
        side = "YES"
        our_prob = bookmaker_prob + sentiment_boost
        mkt_prob = market_prob

    our_prob = max(0.01, min(0.99, our_prob))
    calc_edge = edge(our_prob, mkt_prob)

    if calc_edge < SPORTS_MIN_EDGE:
        return None

    reasoning = (
        f"Bookmaker consensus: {bookmaker_prob:.1%}. "
        f"Market implied: {mkt_prob:.1%}. "
        f"News sentiment: {news['signal']} ({news['sentiment']:+.2f}). "
        f"Edge: {calc_edge:.1%}."
    )

    return {
        "market_id": condition_id,
        "question": question,
        "category": "sports",
        "recommended_side": side,
        "market_prob": round(market_prob, 4),  # always YES price
        "estimated_prob": round(our_prob, 4),
        "edge": round(calc_edge, 4),
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "reasoning": reasoning,
    }


def scan_sports_markets(markets: list[dict], bankroll: float) -> list[dict]:
    all_odds = fetch_all_odds()
    opportunities = []

    for market in markets:
        result = analyze_market(market, all_odds)
        if result:
            price = result["market_prob"] if result["recommended_side"] == "YES" else (1 - result["market_prob"])
            result["kelly_size_usdc"] = compute_bet_size(result["estimated_prob"], price, bankroll)
            opportunities.append(result)

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
