"""
Global events market analyzer.
Uses news sentiment to estimate probabilities for event markets.
"""
from src.feeds.news import analyze_news_for_market
from src.analysis.kelly import edge, compute_bet_size
from src.polymarket.gamma import get_market_tokens, get_market_price
from src.config import BOT_MIN_EDGE

# Higher edge threshold for events (most uncertain)
EVENTS_MIN_EDGE = max(BOT_MIN_EDGE, 0.10)

# How much news sentiment can shift baseline probability
SENTIMENT_WEIGHT = 0.10


def analyze_market(market: dict) -> dict | None:
    question = market.get("question", "")
    condition_id = market.get("conditionId") or market.get("id")

    yes_token, _ = get_market_tokens(market)
    if not yes_token:
        return None

    yes_price, _ = get_market_price(market)
    market_prob = yes_price
    if market_prob is None or market_prob <= 0:
        return None

    news = analyze_news_for_market(question)
    if news["article_count"] == 0:
        return None  # no news, no signal

    # Sentiment shifts from market probability
    sentiment_shift = news["sentiment"] * SENTIMENT_WEIGHT
    our_prob = market_prob + sentiment_shift
    our_prob = max(0.05, min(0.95, our_prob))

    calc_edge = edge(our_prob, market_prob)
    if abs(calc_edge) < EVENTS_MIN_EDGE:
        return None

    if our_prob < market_prob:
        # Bet NO
        side = "NO"
        our_prob_adj = 1 - our_prob
        mkt_prob_adj = 1 - market_prob
    else:
        side = "YES"
        our_prob_adj = our_prob
        mkt_prob_adj = market_prob

    calc_edge = edge(our_prob_adj, mkt_prob_adj)
    if calc_edge < EVENTS_MIN_EDGE:
        return None

    reasoning = (
        f"News sentiment: {news['signal']} ({news['sentiment']:+.2f}) "
        f"from {news['article_count']} articles. "
        f"Market: {market_prob:.1%} → our estimate: {our_prob:.1%}. "
        f"Edge: {calc_edge:.1%}."
    )

    return {
        "market_id": condition_id,
        "question": question,
        "category": "events",
        "recommended_side": side,
        "market_prob": round(mkt_prob_adj, 4),
        "estimated_prob": round(our_prob_adj, 4),
        "edge": round(calc_edge, 4),
        "yes_token_id": yes_token,
        "reasoning": reasoning,
    }


def scan_events_markets(markets: list[dict], bankroll: float) -> list[dict]:
    if not markets:
        return []

    opportunities = []
    for market in markets:
        result = analyze_market(market)
        if result:
            price = result["market_prob"] if result["recommended_side"] == "YES" else (1 - result["market_prob"])
            result["kelly_size_usdc"] = compute_bet_size(result["estimated_prob"], price, bankroll)
            opportunities.append(result)

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
