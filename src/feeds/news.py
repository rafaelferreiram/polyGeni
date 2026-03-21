"""
News feed via NewsAPI (free: 100 req/day).
Used for global events sentiment analysis.
"""
import httpx
from src.config import NEWSAPI_BASE, NEWS_API_KEY


def fetch_headlines(query: str, language: str = "en", page_size: int = 20) -> list[dict]:
    if not NEWS_API_KEY:
        return []
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{NEWSAPI_BASE}/everything",
            params={
                "q": query,
                "language": language,
                "pageSize": page_size,
                "sortBy": "publishedAt",
                "apiKey": NEWS_API_KEY,
            },
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("articles", [])


def simple_sentiment(text: str) -> float:
    """
    Very lightweight keyword-based sentiment scorer.
    Returns score in [-1, 1]. Negative = bearish, Positive = bullish.
    """
    text_lower = text.lower()

    bullish_words = [
        "wins", "win", "victory", "leads", "gains", "rises", "surges",
        "beats", "strong", "positive", "approved", "confirmed", "success",
        "advances", "increases", "higher", "up", "record", "breakthrough",
    ]
    bearish_words = [
        "loses", "loss", "falls", "drops", "crash", "fails", "rejected",
        "defeated", "weak", "negative", "denied", "collapse", "lower",
        "down", "decline", "cut", "crisis", "concern", "risk",
    ]

    bull_score = sum(1 for w in bullish_words if w in text_lower)
    bear_score = sum(1 for w in bearish_words if w in text_lower)
    total = bull_score + bear_score
    if total == 0:
        return 0.0
    return (bull_score - bear_score) / total


def analyze_news_for_market(question: str) -> dict:
    """
    Fetch and score news relevant to a market question.
    Returns sentiment summary.
    """
    # Extract key terms from the question (first 5 words)
    query = " ".join(question.split()[:5])
    articles = fetch_headlines(query)

    if not articles:
        return {"sentiment": 0.0, "article_count": 0, "signal": "neutral"}

    scores = []
    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}"
        scores.append(simple_sentiment(text))

    avg = sum(scores) / len(scores)
    signal = "bullish" if avg > 0.1 else ("bearish" if avg < -0.1 else "neutral")

    return {
        "sentiment": round(avg, 3),
        "article_count": len(articles),
        "signal": signal,
    }
