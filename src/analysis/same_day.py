"""
Same-day market scanner — targets markets resolving today.
Focuses on Bitcoin price markets using intraday volatility model.
"""
import re
import json
import math
import httpx
from datetime import datetime, timezone
from src.config import GAMMA_HOST, BINANCE_BASE
from src.polymarket.gamma import get_market_price, get_market_tokens
from src.analysis.kelly import edge, compute_bet_size

MIN_EDGE = 0.04
MIN_VOL = 5_000  # same-day markets tend to have high volume


def _fetch_btc_price() -> float | None:
    try:
        resp = httpx.get(f"{BINANCE_BASE}/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=8)
        return float(resp.json()["price"])
    except Exception:
        return None


def _hours_until_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # next midnight
    from datetime import timedelta
    next_midnight = midnight + timedelta(days=1)
    return (next_midnight - now).total_seconds() / 3600


def _intraday_prob_above(current: float, target: float, hours_left: float,
                          annual_vol: float = 0.80) -> float:
    """
    Log-normal probability that BTC closes above `target` given `hours_left`.
    Uses Black-Scholes d2 calculation. annual_vol ~ 80% for BTC.
    """
    if hours_left <= 0:
        return 1.0 if current >= target else 0.0
    t = hours_left / (365 * 24)  # fraction of year
    sigma_t = annual_vol * math.sqrt(t)
    if sigma_t == 0:
        return 1.0 if current >= target else 0.0
    log_ratio = math.log(current / target)
    d = log_ratio / sigma_t  # simplified (no drift for short horizons)
    # CDF approximation
    return _norm_cdf(d)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _fetch_same_day_markets() -> list[dict]:
    """Fetch markets resolving today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Use end_date_max = end of today
    cutoff = today + "T23:59:59Z"
    resp = httpx.get(f"{GAMMA_HOST}/markets", params={
        "active": "true", "closed": "false",
        "end_date_max": cutoff,
        "order": "volume24hr", "ascending": "false",
        "limit": 50,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    markets = data if isinstance(data, list) else data.get("data", [])
    # Keep only those ending today
    return [m for m in markets
            if (m.get("endDateIso") or m.get("endDate", ""))[:10] == today]


def scan_same_day_markets(bankroll: float) -> list[dict]:
    """
    Scan same-day Bitcoin price markets for edges vs intraday model.
    Returns list of opportunity dicts in the same format as other scanners.
    """
    btc_price = _fetch_btc_price()
    if btc_price is None:
        return []

    hours_left = _hours_until_midnight_utc()
    markets = _fetch_same_day_markets()
    opportunities = []

    for m in markets:
        q = m.get("question", "")
        q_lower = q.lower()

        # Only Bitcoin price markets
        match = re.search(r"bitcoin.*above\s+\$?([\d,]+)", q_lower)
        if not match:
            continue

        target_str = match.group(1).replace(",", "")
        try:
            target = float(target_str)
        except ValueError:
            continue

        vol = float(m.get("volume24hr") or m.get("volume24hrClob") or 0)
        if vol < MIN_VOL:
            continue

        yes_price, _ = get_market_price(m)
        if yes_price is None or not (0.02 < yes_price < 0.98):
            continue

        yes_token, _ = get_market_tokens(m)
        if not yes_token:
            continue

        our_prob = _intraday_prob_above(btc_price, target, hours_left)
        yes_edg = our_prob - yes_price

        if yes_edg >= 0:
            side, calc_edge, side_prob, price = "YES", yes_edg, our_prob, yes_price
        else:
            no_edg = -yes_edg
            side, calc_edge, side_prob, price = "NO", no_edg, 1 - our_prob, 1 - yes_price

        if calc_edge < MIN_EDGE:
            continue

        kelly_size = compute_bet_size(side_prob, price, bankroll)
        if kelly_size <= 0:
            continue

        end = m.get("endDateIso", m.get("endDate", ""))[:10]
        reasoning = (
            f"BTC now ${btc_price:,.0f}. Target: ${target:,.0f}. "
            f"Model prob: {our_prob:.1%}. Polymarket: {yes_price:.1%}. "
            f"Edge: {calc_edge:.1%}. {hours_left:.1f}h left today."
        )

        opportunities.append({
            "market_id": m.get("conditionId", ""),
            "question": q,
            "category": "bitcoin",
            "recommended_side": side,
            "market_prob": round(yes_price, 4),
            "estimated_prob": round(side_prob, 4),
            "edge": round(calc_edge, 4),
            "kelly_size_usdc": kelly_size,
            "yes_token_id": yes_token,
            "reasoning": reasoning,
            "resolves": end,
        })

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
