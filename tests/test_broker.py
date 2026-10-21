import sqlite3

import pytest

from src.solana.broker import PaperBroker
from tests.conftest import TOKEN, make_quote


def broker(ledger, price=2.0, slippage_bps=50, fee_lamports=1_000_000, sol=100.0):
    return PaperBroker(ledger, quote_fn=make_quote(price), sol_usd_fn=lambda: sol,
                       slippage_bps=slippage_bps, fee_lamports=fee_lamports)


def test_buy_applies_slippage_and_fees(ledger):
    fill = broker(ledger).buy("o1", TOKEN, "TKN", 10.0, 6, strategy="s", stop_price=1.8, tp_price=2.4)
    assert fill["qty"] == pytest.approx(5.0 * (1 - 0.005), rel=1e-6)   # 10 USD / 2.0, minus 50 bps
    assert fill["fee_usd"] == pytest.approx(0.1)                        # 0.001 SOL * $100
    assert fill["price"] > fill["quote_price"]                          # paid worse than quote
    assert fill["route"] == "FakeAMM"
    assert ledger.cash() == pytest.approx(100 - 10 - 0.1)


def test_sell_round_trip_loses_costs(ledger):
    b = broker(ledger)
    buy = b.buy("o1", TOKEN, "TKN", 10.0, 6, strategy="s", stop_price=1.8, tp_price=2.4)
    sell = b.sell("o2", TOKEN, "TKN", buy["qty"], 6, strategy="s")
    assert sell["usd_gross"] == pytest.approx(buy["qty"] * 2.0 * (1 - 0.005), rel=1e-6)
    assert ledger.cash() < 100 - 0.2   # slippage both ways + two fees
    assert ledger.positions_from_fills() == {}
    [t] = ledger.closed_trades()
    assert t["pnl_usd"] == pytest.approx(ledger.cash() - 100, abs=1e-9)


def test_idempotent_by_order_id(ledger):
    calls = []
    q = make_quote(2.0)
    b = PaperBroker(ledger, quote_fn=lambda *a: calls.append(a) or q(*a), sol_usd_fn=lambda: 100.0)
    f1 = b.buy("same", TOKEN, "TKN", 10.0, 6, strategy="s", stop_price=1, tp_price=3)
    f2 = b.buy("same", TOKEN, "TKN", 10.0, 6, strategy="s", stop_price=1, tp_price=3)
    assert f1 == f2 and len(ledger.fills()) == 1 and len(calls) == 1


def test_ledger_is_append_only(ledger):
    broker(ledger).buy("o1", TOKEN, "TKN", 10.0, 6, strategy="s", stop_price=1, tp_price=3)
    ledger.log_decision("entry", reason="x")
    for sql in ("UPDATE fills SET qty=0", "DELETE FROM fills", "UPDATE decisions SET reason='y'", "DELETE FROM decisions"):
        with pytest.raises(sqlite3.IntegrityError):
            ledger.conn.execute(sql)
    assert len(ledger.fills()) == 1


def test_paper_deposit_withdraw(ledger):
    ledger.move_cash("deposit", 50)
    assert ledger.cash() == 150 and ledger.contributed() == 150
    ledger.move_cash("withdraw", 30)
    assert ledger.cash() == 120
    with pytest.raises(ValueError):
        ledger.move_cash("withdraw", 1000)
    with pytest.raises(ValueError):
        ledger.move_cash("deposit", -5)
    assert ledger.realized_pnl() == pytest.approx(0)
