"""
ESPN free odds feed — replaces the exhausted Odds API.
Uses the undocumented but stable ESPN core API.
Covers NBA, NHL, NCAAB, MLB.
Parallel fetching via ThreadPoolExecutor for speed.
"""
import httpx
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

_CACHE: list[dict] = []
_CACHE_TIME: datetime | None = None
_CACHE_TTL_MIN = 90  # refresh at most once per 90 minutes


def _ml2p(ml: float) -> float:
    if ml > 0:
        return 100 / (ml + 100)
    return abs(ml) / (abs(ml) + 100)


def _fetch_game_odds(sport: str, league: str, game_id: str, home: str, away: str) -> dict | None:
    """Fetch moneyline odds for a single game. Returns None if unavailable."""
    try:
        r = httpx.get(
            f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}"
            f"/events/{game_id}/competitions/{game_id}/odds",
            timeout=8,
        )
        items = r.json().get("items", [])
        if not items:
            return None
        od = httpx.get(items[0].get("$ref", ""), timeout=8).json()
        hml = od.get("homeTeamOdds", {}).get("moneyLine")
        aml = od.get("awayTeamOdds", {}).get("moneyLine")
        if not hml or not aml:
            return None
        hp, ap = _ml2p(hml), _ml2p(aml)
        tot = hp + ap
        return {"home": home, "away": away, "home_prob": round(hp / tot, 4), "away_prob": round(ap / tot, 4)}
    except Exception:
        return None


def _collect_game_stubs(sport: str, league: str, dates: list[str]) -> list[tuple]:
    """Return (sport, league, game_id, home, away) tuples for upcoming games."""
    stubs = []
    for date in dates:
        try:
            resp = httpx.get(
                f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard",
                params={"dates": date}, timeout=10,
            )
            for ev in resp.json().get("events", []):
                if ev.get("status", {}).get("type", {}).get("description") == "Final":
                    continue
                game_id = ev["id"]
                comps = ev.get("competitions", [{}])[0]
                teams = comps.get("competitors", [])
                home = next(
                    (t["team"]["displayName"] for t in teams if t.get("homeAway") == "home"), ""
                ).lower()
                away = next(
                    (t["team"]["displayName"] for t in teams if t.get("homeAway") == "away"), ""
                ).lower()
                if home and away:
                    stubs.append((sport, league, game_id, home, away))
        except Exception:
            continue
    return stubs


def get_bookmaker_events() -> list[dict]:
    """Return cached list of {home, away, home_prob, away_prob} for upcoming events."""
    global _CACHE, _CACHE_TIME

    now = datetime.utcnow()
    if _CACHE_TIME and (now - _CACHE_TIME).total_seconds() < _CACHE_TTL_MIN * 60:
        return _CACHE

    dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]

    leagues = [
        ("basketball", "mens-college-basketball"),
        ("basketball", "nba"),
        ("hockey", "nhl"),
        ("baseball", "mlb"),
    ]

    # Step 1: collect all game stubs (fast — one request per league per date)
    all_stubs: list[tuple] = []
    for sport, league in leagues:
        all_stubs.extend(_collect_game_stubs(sport, league, dates))

    # Step 2: fetch odds in parallel (2 requests per game, but concurrent)
    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(_fetch_game_odds, sport, league, gid, home, away): (home, away)
            for sport, league, gid, home, away in all_stubs
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                events.append(result)

    _CACHE = events
    _CACHE_TIME = now
    return events
