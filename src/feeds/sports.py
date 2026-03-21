"""
Sports odds feed via The Odds API (free: 500 req/month).
Converts bookmaker odds to implied probabilities.
"""
import httpx
from src.config import ODDS_API_BASE, ODDS_API_KEY


SUPPORTED_SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "baseball_mlb",
    "icehockey_nhl",
    "mma_mixed_martial_arts",
    "tennis_atp_us_open",
]


def fetch_odds(sport: str, regions: str = "us,eu", markets: str = "h2h") -> list[dict]:
    if not ODDS_API_KEY:
        return []
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{ODDS_API_BASE}/sports/{sport}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
            },
        )
        if resp.status_code != 200:
            return []
        return resp.json()


def fetch_all_odds() -> list[dict]:
    """Fetch odds across all supported sports."""
    all_events = []
    for sport in SUPPORTED_SPORTS:
        events = fetch_odds(sport)
        for e in events:
            e["sport_key"] = sport
        all_events.extend(events)
    return all_events


def decimal_to_prob(decimal_odds: float) -> float:
    """Convert decimal odds to raw implied probability."""
    if decimal_odds <= 0:
        return 0.5
    return 1.0 / decimal_odds


def get_consensus_probability(event: dict, outcome_name: str) -> float | None:
    """
    Average implied probability for an outcome across all bookmakers,
    removing the overround (margin) to get fair probability.
    """
    bookmaker_probs = []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] == "h2h":
                outcomes = market.get("outcomes", [])
                # Get all raw probs to compute overround
                raw_probs = [decimal_to_prob(o["price"]) for o in outcomes]
                overround = sum(raw_probs)
                # Find the outcome we want
                for o in outcomes:
                    if o["name"].lower() == outcome_name.lower():
                        raw = decimal_to_prob(o["price"])
                        fair = raw / overround  # remove margin
                        bookmaker_probs.append(fair)
    if not bookmaker_probs:
        return None
    return sum(bookmaker_probs) / len(bookmaker_probs)


SEASON_LONG_KEYWORDS = [
    "champion", "championship", "cup", "finals", "final", "title",
    "season", "series", "playoff", "qualify", "world cup",
    "stanley cup", "nba finals", "super bowl", "world series",
    "finish in", "finish top", "finish last", "relegated", "promotion",
    "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
    "win the", "nba champion", "nfl champion",
]


def _is_season_long(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in SEASON_LONG_KEYWORDS)


def match_event_to_market(event: dict, market_question: str) -> float | None:
    """
    Match a bookmaker H2H event to a Polymarket question.
    Only matches single-game questions — ignores season-long markets
    (championships, cups, finals) to avoid comparing game odds to futures.
    """
    if _is_season_long(market_question):
        return None  # Don't match H2H odds against season-long markets

    home = event.get("home_team", "").lower()
    away = event.get("away_team", "").lower()
    question_lower = market_question.lower()

    if home in question_lower or away in question_lower:
        for team in [event.get("home_team"), event.get("away_team")]:
            if team and team.lower() in question_lower:
                prob = get_consensus_probability(event, team)
                if prob is not None:
                    return prob
    return None
