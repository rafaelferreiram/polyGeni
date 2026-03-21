"""
Short-term market scanner — targets markets resolving within N days.
Cross-references Polymarket prices against bookmaker consensus odds.
Matches by finding BOTH teams from the Polymarket question in a single bookmaker event.
"""
import json
import re
import httpx
from datetime import datetime, timedelta
from src.config import GAMMA_HOST
from src.polymarket.gamma import get_market_tokens, get_market_price
from src.analysis.kelly import edge, compute_bet_size
from src.feeds.espn_odds import get_bookmaker_events

MIN_EDGE = 0.04
MIN_VOL  = 500

_SKIP_KEYWORDS = [
    "exact score", "over/under", "o/u", "spread:", "handicap",
    "total corners", "total goals", "first half", "half time",
    "will trump", "will bitcoin", "will the fed", "will elon",
]


def _fetch_poly_short_term(days: int) -> list[dict]:
    cutoff = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{GAMMA_HOST}/markets", params={
            "active": "true", "closed": "false",
            "end_date_max": cutoff,
            "order": "volume1wkClob", "ascending": "false",
            "limit": 100,
        })
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def _fetch_bookmaker_events() -> list[dict]:
    """Returns list of {home, away, home_prob, away_prob} via ESPN free API."""
    return get_bookmaker_events()


def _match_event(q_lower: str, yes_team: str, no_team: str, events: list[dict]) -> tuple[float | None, str]:
    """
    Find a bookmaker event where BOTH teams appear in the question.
    Returns (yes_win_probability, matched_info) or (None, "").
    """
    for ev in events:
        home, away = ev["home"], ev["away"]

        # Both teams must appear in the question (whole-word match to avoid substrings)
        def word_in(part: str, text: str) -> bool:
            return bool(re.search(r'\b' + re.escape(part) + r'\b', text))

        home_in_q = any(word_in(part, q_lower) for part in home.split() if len(part) > 4)
        away_in_q = any(word_in(part, q_lower) for part in away.split() if len(part) > 4)

        if not (home_in_q and away_in_q):
            continue

        # Determine which bookmaker team is the YES team
        home_in_yes = any(word_in(part, yes_team) for part in home.split() if len(part) > 4)
        away_in_yes = any(word_in(part, yes_team) for part in away.split() if len(part) > 4)

        if home_in_yes:
            yes_prob = ev["home_prob"]
            info = f"{ev['home']} vs {ev['away']}"
        elif away_in_yes:
            yes_prob = ev["away_prob"]
            info = f"{ev['away']} vs {ev['home']}"
        else:
            # Can't determine orientation — use the first team as YES
            yes_prob = ev["home_prob"]
            info = f"{ev['home']} vs {ev['away']}"

        return yes_prob, info

    return None, ""


def scan_short_term_markets(bankroll: float, days: int = 7) -> list[dict]:
    markets = _fetch_poly_short_term(days)
    events  = _fetch_bookmaker_events()

    opportunities = []
    for m in markets:
        q = m.get("question", "")
        q_lower = q.lower()

        if any(kw in q_lower for kw in _SKIP_KEYWORDS):
            continue

        vol = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
        if vol < MIN_VOL:
            continue

        yes_price, _ = get_market_price(m)
        if yes_price is None or not (0.04 < yes_price < 0.96):
            continue

        yes_token, no_token = get_market_tokens(m)
        if not yes_token:
            continue

        # Parse YES and NO teams from "Team A vs. Team B" format
        yes_team, no_team = "", ""
        for sep in [" vs. ", " vs "]:
            if sep in q_lower:
                parts = q_lower.split(sep, 1)
                yes_team = parts[0].strip()
                no_team  = parts[1].strip()
                break

        if not yes_team:
            continue

        book_prob_yes, match_info = _match_event(q_lower, yes_team, no_team, events)
        if book_prob_yes is None:
            continue

        yes_edg = book_prob_yes - yes_price
        # no_edg is always the negative of yes_edg in a binary market
        # Pick the positive side — that's where the edge lies
        if yes_edg >= 0:
            side, calc_edge, our_prob, price = "YES", yes_edg, book_prob_yes, yes_price
        else:
            no_edg = -yes_edg
            side, calc_edge, our_prob, price = "NO", no_edg, 1 - book_prob_yes, 1 - yes_price

        if calc_edge < MIN_EDGE:
            continue

        kelly_size = compute_bet_size(our_prob, price, bankroll)
        if kelly_size <= 0:
            continue

        end = m.get("endDateIso", m.get("endDate", ""))[:10]
        reasoning = (
            f"Bookmaker ({match_info}): YES={book_prob_yes:.1%}. "
            f"Polymarket: {yes_price:.1%}. Edge: {calc_edge:.1%}. Resolves: {end}."
        )

        opportunities.append({
            "market_id": m.get("conditionId", ""),
            "question": q,
            "category": "sports",
            "recommended_side": side,
            "market_prob": round(yes_price, 4),
            "estimated_prob": round(our_prob, 4),
            "edge": round(calc_edge, 4),
            "kelly_size_usdc": kelly_size,
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "reasoning": reasoning,
            "resolves": end,
        })

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
