"""Hyperliquid paper broker — fixtures only, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from src.solana import hyperliquid as hl

BOOK = {"bids": [(100.0, 1.0), (99.0, 2.0), (98.0, 10.0)],
        "asks": [(101.0, 1.0), (102.0, 2.0), (103.0, 10.0)]}
CTX = {"BTC": {"name": "BTC", "markPx": "100.5", "funding": "0.0001"},
       "xyz:GOLD": {"name": "xyz:GOLD", "markPx": "100.5", "funding": "0.0001", "deployerFeeScale": "1.0"},
       "xyz:SILVER": {"name": "xyz:SILVER", "markPx": "100.5", "funding": "0.0", "deployerFeeScale": "1.0",
                      "growthMode": "enabled"}}


def broker(ledger, now=None):
    return hl.HyperliquidPaperBroker(ledger, book_fn=lambda coin: BOOK, ctx_fn=lambda coin: CTX[coin],
                                     now_fn=now or (lambda: datetime.now(timezone.utc).timestamp()))


def test_xyz_prefix_handling():
    assert hl.split_coin("xyz:GOLD") == ("xyz", "xyz:GOLD")
    assert hl.split_coin("BTC") == ("", "BTC")
    assert hl.coin_of(hl.instrument_id("xyz:SILVER")) == "xyz:SILVER"
    with pytest.raises(ValueError):
        hl.coin_of("So11111111111111111111111111111111111111112")


def test_fees_per_market():
    assert hl.taker_fee_bps(CTX["BTC"]) == 4.5
    assert hl.taker_fee_bps(CTX["xyz:GOLD"]) == 9.0          # HIP-3 deployer scale 1 => 2x
    assert hl.taker_fee_bps(CTX["xyz:SILVER"]) == pytest.approx(0.9)   # growth mode => 90% off


def test_buy_walks_the_book(ledger):
    ledger.move_cash("deposit", 1000)
    fill = broker(ledger).buy("o1", "hl:BTC", "BTC", 305.0, strategy="m", stop_price=90, tp_price=120)
    # 1 @101 (=101) + 2 @102 (=204) => 305 USD for exactly 3 BTC
    assert fill["qty"] == pytest.approx(3.0)
    assert fill["price"] == pytest.approx(305 / 3)
    assert fill["quote_price"] == 100.5 and fill["slippage_bps"] > 0
    assert fill["fee_usd"] == pytest.approx(305 * 4.5e-4)
    assert fill["venue"] == "hyperliquid"
    assert ledger.cash() == pytest.approx(1100 - 305 - 305 * 4.5e-4)


def test_sell_walks_bids_and_charges_fee(ledger):
    ledger.move_cash("deposit", 1000)
    b = broker(ledger)
    b.buy("o1", "hl:xyz:GOLD", "xyz:GOLD", 101.0, strategy="m", stop_price=90, tp_price=120)
    sell = b.sell("o2", "hl:xyz:GOLD", "xyz:GOLD", 1.0, strategy="m")
    assert sell["usd_gross"] == pytest.approx(100.0)
    assert sell["fee_usd"] == pytest.approx(100 * 9e-4)
    [t] = ledger.closed_trades()
    assert t["pnl_usd"] == pytest.approx(100 - 0.09 - 101 - 101 * 9e-4)
    assert t["venue"] == "hyperliquid"


def test_leverage_is_capped_and_sets_buying_power(ledger):
    with pytest.raises(hl.LeverageError):
        hl.HyperliquidPaperBroker(ledger, leverage=4.0)
    with pytest.raises(hl.LeverageError):      # $100 cash cannot carry a $150 notional at 1x
        broker(ledger).buy("o1", "hl:BTC", "BTC", 150.0, strategy="m", stop_price=90, tp_price=120)
    assert ledger.fills() == []
    b2 = broker(ledger)
    b2.leverage = 2.0                          # 2x isolated: $150 fits in $200 of buying power, another $60 does not
    b2.buy("o2", "hl:BTC", "BTC", 150.0, strategy="m", stop_price=90, tp_price=120)
    with pytest.raises(hl.LeverageError):
        b2.buy("o3", "hl:xyz:GOLD", "xyz:GOLD", 60.0, strategy="m", stop_price=90, tp_price=120)


def test_refuses_orders_deeper_than_book(ledger):
    ledger.move_cash("deposit", 10_000)
    with pytest.raises(RuntimeError):
        broker(ledger).buy("o1", "hl:BTC", "BTC", 5000.0, strategy="m", stop_price=90, tp_price=120)


def test_funding_accrues_on_open_longs(ledger):
    ledger.move_cash("deposit", 1000)
    t0 = datetime.now(timezone.utc)
    b = broker(ledger, now=lambda: (t0 + timedelta(hours=3)).timestamp())
    fill = b.buy("o1", "hl:BTC", "BTC", 101.0, strategy="m", stop_price=90, tp_price=120)
    pos = {**ledger.positions_from_fills()["hl:BTC"], "opened_at": t0.isoformat()}
    paid = b.accrue([pos])
    assert paid == pytest.approx(1.0 * 100.5 * 0.0001 * 3, rel=1e-3)   # qty * mark * rate * hours
    cash_before_sell = ledger.cash()
    assert cash_before_sell == pytest.approx(1100 - 101 - fill["fee_usd"] - paid)
    b.sell("o2", "hl:BTC", "BTC", fill["qty"], strategy="m")
    assert ledger.closed_trades()[0]["funding_usd"] == pytest.approx(paid)


def test_scout_lists_fixed_markets():
    scout = hl.HyperliquidScout(markets=["BTC", "xyz:GOLD"], ctx_fn=lambda c: {**CTX[c], "openInterest": "10",
                                                                                 "dayNtlVlm": "1e6"})
    cs = scout.candidates()
    assert [c.mint for c in cs] == ["hl:BTC", "hl:xyz:GOLD"]
    assert all(c.venue == "hyperliquid" and c.price_usd == 100.5 for c in cs)
