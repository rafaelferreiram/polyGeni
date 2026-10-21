"""Polymarket bot: real orders impossible unless LIVE_TRADING=1."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot import trader
from src.database import Base


def test_real_order_blocked_without_live_flag(monkeypatch):
    assert trader.LIVE_TRADING is False
    monkeypatch.setattr(trader, "check_trade", lambda *a: (True, "ok"))
    monkeypatch.setattr(trader.poly, "place_order", lambda **kw: (_ for _ in ()).throw(AssertionError("REAL ORDER")))
    eng = create_engine("sqlite://")
    import src.models  # noqa: F401
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    opp = dict(recommended_side="YES", yes_token_id="t", market_prob=0.5, kelly_size_usdc=1.0, market_id="m",
               question="q", category="c", estimated_prob=0.6, edge=0.1, reasoning="r")
    res = trader.execute_opportunity(opp, 10.0, 0, db, dry_run=False)
    assert res["success"] and res["dry_run"]
