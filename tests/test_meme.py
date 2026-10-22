"""Meme-coin trench: safety gate, signals, paper fills with fees and impact, rug alarm."""
from datetime import datetime, timedelta, timezone

from src.solana import meme
from src.solana.ledger import Ledger


def _token(**kw):
    now = datetime.now(timezone.utc)
    t = {"id": "MINT1", "symbol": "FROG", "holderCount": 900, "liquidity": 80_000, "organicScore": 70, "launchpad": "pump.fun",
         "graduatedAt": (now - timedelta(hours=1)).isoformat(), "firstPool": {"createdAt": (now - timedelta(hours=5)).isoformat()},
         "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 18, "devBalancePercentage": 1},
         "stats1h": {"priceChange": 30, "numBuys": 300, "numSells": 150, "numNetBuyers": 50, "buyOrganicVolume": 40_000},
         "stats5m": {"priceChange": 4}}
    t.update(kw)
    return t


def test_trench_buys_only_safe_tokens_and_runs_from_a_rug(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    for led in meme._ledgers.values():
        led.set_state("model", "x")
    good, bad = _token(), _token(id="MINT2", audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True,
                                                    "topHoldersPercentage": 18, "devBalancePercentage": 1})
    assert all(ok for ok, _ in meme.safety(good).values()) and not meme.safety(bad)["mint/freeze revogados"][0]
    assert meme.signal("meme_grad", good) and meme.signal("meme_momo", good)
    prices = {"MINT1": {"price": 0.01, "liq": 80_000}, "MINT2": {"price": 0.01, "liq": 80_000}}
    meme.enter("meme_grad", [good, bad], prices)
    led = meme.ledger("meme_grad")
    [pos] = led.positions()
    assert pos["mint"] == "MINT1" and abs(pos["cost_usd"] - 10) < 1e-9          # $10 in, fee and impact included
    meme.manage("meme_grad", {"MINT1": {"price": 0.01, "liq": 30_000}})          # liquidity collapsed: rug alarm
    assert led.positions() == [] and "rug" in led.closed_trades()[0]["reason"]
    assert led.closed_trades()[0]["pnl_usd"] < 0                                 # fees + impact on the way out
    meme.enter("meme_grad", [good], prices)
    assert led.positions() == []                                                 # no re-entry right after an exit


def test_trench_target_and_rising_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    meme.enter("meme_momo", [_token()], {"MINT1": {"price": 1.0, "liq": 1_000_000}})
    led = meme.ledger("meme_momo")
    meme.manage("meme_momo", {"MINT1": {"price": 1.35, "liq": 1_000_000}})       # +35%: stop now locks 60% of the gain
    assert led.positions()
    meme.manage("meme_momo", {"MINT1": {"price": 1.15, "liq": 1_000_000}})       # back below the raised stop (+21%)
    t = led.closed_trades()[0]
    assert "stop" in t["reason"] and t["pnl_usd"] > 0
