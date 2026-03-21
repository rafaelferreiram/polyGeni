"""
Main analysis engine — orchestrates all analyzers.
"""
from src.polymarket.gamma import fetch_bitcoin_markets, fetch_sports_markets, fetch_events_markets
from src.analysis.bitcoin import scan_bitcoin_markets
from src.analysis.sports import scan_sports_markets
from src.analysis.events import scan_events_markets


def run_full_scan(bankroll: float) -> list[dict]:
    """
    Scan all categories and return all opportunities sorted by edge.
    """
    opportunities = []

    # Bitcoin markets
    try:
        btc_markets = fetch_bitcoin_markets(limit=30)
        opportunities.extend(scan_bitcoin_markets(btc_markets, bankroll))
    except Exception as e:
        print(f"[engine] Bitcoin scan error: {e}")

    # Sports markets
    try:
        sports_markets = fetch_sports_markets(limit=30)
        opportunities.extend(scan_sports_markets(sports_markets, bankroll))
    except Exception as e:
        print(f"[engine] Sports scan error: {e}")

    # Events markets
    try:
        events_markets = fetch_events_markets(limit=30)
        opportunities.extend(scan_events_markets(events_markets, bankroll))
    except Exception as e:
        print(f"[engine] Events scan error: {e}")

    # Sort by edge descending
    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
