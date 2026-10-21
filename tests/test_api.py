"""/api/paper endpoints: local-only + token guard, paper funds, TWR excluding cash flows."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import paper_routes as pr
from src.solana import engine as eng_mod
from tests.test_engine import Price, build


@pytest.fixture
def client(ledger, monkeypatch):
    monkeypatch.setattr(eng_mod, "_engine", build(ledger, Price(1.0)))
    from src.main import app
    return TestClient(app)


@pytest.fixture
def local(monkeypatch):
    # TestClient connects as host "testclient" to "testserver"; treat them as loopback for these tests
    monkeypatch.setattr(pr, "LOCAL_CLIENTS", pr.LOCAL_CLIENTS | {"testclient"})
    monkeypatch.setattr(pr, "LOCAL_HOSTNAMES", pr.LOCAL_HOSTNAMES | {"testserver"})
    return {"X-Paper-Token": pr.API_TOKEN}


def test_status_is_paper(client):
    s = client.get("/api/paper/status").json()
    assert s["mode"] == "PAPER" and s["live_trading"] is False and s["equity"] == 100


def test_mutations_rejected_from_non_loopback(client):
    r = client.post("/api/paper/funds/deposit", json={"amount_usd": 10}, headers={"X-Paper-Token": pr.API_TOKEN})
    assert r.status_code == 403
    assert client.get("/api/paper/token").status_code == 403
    assert client.post("/api/paper/start").status_code == 403


def test_mutations_need_token(client, local):
    assert client.post("/api/paper/funds/deposit", json={"amount_usd": 10}).status_code == 403
    assert client.post("/api/paper/funds/deposit", json={"amount_usd": 10},
                       headers={"X-Paper-Token": "wrong"}).status_code == 403
    assert client.get("/api/paper/token").json()["token"] == pr.API_TOKEN


def test_deposit_withdraw_validation(client, local):
    assert client.post("/api/paper/funds/deposit", json={"amount_usd": 50}, headers=local).status_code == 200
    assert client.post("/api/paper/funds/deposit", json={"amount_usd": -5}, headers=local).status_code == 422
    assert client.post("/api/paper/funds/withdraw", json={"amount_usd": 1000}, headers=local).status_code == 400
    r = client.post("/api/paper/funds/withdraw", json={"amount_usd": 30}, headers=local)
    assert r.status_code == 200 and r.json()["cash"] == pytest.approx(120)
    s = client.get("/api/paper/status").json()
    assert s["contributed"] == 120 and s["total_pnl"] == pytest.approx(0)


def test_withdraw_cannot_touch_cash_backing_positions(client, local, ledger):
    import time
    assert client.post("/api/paper/cycle", headers=local).json()["started"]   # scan runs in the background
    for _ in range(50):
        if ledger.positions():
            break
        time.sleep(0.05)
    assert ledger.positions()                              # a $10 position (1x, fully paid)
    assert client.post("/api/paper/funds/withdraw", json={"amount_usd": 95}, headers=local).status_code == 400


def test_pause_market_and_readiness(client, local):
    r = client.post("/api/paper/markets/pause", json={"market": "hyperliquid", "paused": True}, headers=local)
    assert r.json()["paused"] == ["hyperliquid"]
    rd = client.get("/api/paper/readiness").json()
    assert rd["ready"] is False and len(rd["checks"]) >= 6


def _row(h, equity, contributed, sol=None):
    ts = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=h)).isoformat()
    return {"ts": ts, "equity": equity, "contributed": contributed, "sol_price": sol, "btc_price": None}


def test_twr_ignores_deposits_and_withdrawals():
    # +10% on 100, then a 100 deposit, then +0% => TWR 10%, not 120%
    rows = [_row(0, 100, 100), _row(1, 110, 100), _row(2, 210, 200), _row(3, 210, 200)]
    assert pr.twr(rows) == pytest.approx(0.10)
    # withdrawal of 50 after a -10% move: 100 -> 90, withdraw 50 -> 40, then +10% -> 44
    rows = [_row(0, 100, 100), _row(1, 90, 100), _row(2, 40, 50), _row(3, 44, 50)]
    assert pr.twr(rows) == pytest.approx(0.9 * 1.1 - 1)


def test_performance_vs_hold():
    rows = [_row(0, 100, 100, sol=100), _row(1, 105, 100, sol=110)]
    perf = pr.performance(rows, now=datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
    assert perf["all"]["gain_usd"] == pytest.approx(5)
    assert perf["all"]["twr"] == pytest.approx(0.05)
    assert perf["all"]["hold_sol"] == pytest.approx(0.10)


def test_wallets_accept_public_evm_and_solana_only(client, local, monkeypatch):
    from src.solana import market_data as md
    monkeypatch.setattr(md, "evm_wallet_balances", lambda a: {"address": a, "chain": "evm", "arbitrum_usdc": 7.0})
    monkeypatch.setattr(md, "wallet_balances", lambda a: {"address": a, "sol": 1.0, "tokens": []})
    for addr in ("0x374c90fC8Deb979D084e9799117700e908632240", "6ZeDkAdaEd6L4Ruq9PXQwnNS7tzVFCuTowLppr4iYx7D"):
        assert client.post("/api/paper/wallets", json={"address": addr}, headers=local).status_code == 200
    for bad in ("word " * 12, "0x" + "a" * 64, "5" * 88):   # seed phrase / EVM private key / Solana secret key
        assert client.post("/api/paper/wallets", json={"address": bad}, headers=local).status_code == 400
    ws = client.get("/api/paper/wallets").json()["wallets"]
    assert {w.get("chain", "sol") for w in ws} == {"evm", "sol"}
    assert next(w for w in ws if w.get("chain") == "evm")["arbitrum_usdc"] == 7.0


def test_account_sim_and_real_share_shape_and_controls(client, local, monkeypatch, tmp_path):
    from src.solana import config as scfg
    from src.solana import hl_live
    monkeypatch.setattr(scfg, "SHADOW_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setattr(scfg, "LIVE_DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.setattr(hl_live, "_shadow", None)
    monkeypatch.setattr(hl_live, "_live", None)
    monkeypatch.setattr(hl_live, "shadow_cycle", lambda: None)
    monkeypatch.delenv("HL_API_WALLET_KEY", raising=False)
    sim = client.get("/api/paper/account?kind=sim").json()
    real = client.get("/api/paper/account?kind=real").json()
    shared = {"equity", "pnl_usd", "positions", "trades", "decisions", "agents", "live", "limits", "decision_mode"}
    assert shared <= set(sim) and shared <= set(real)
    assert sim["money"] == "fake" and real["money"] == "real" and sim["equity"] == 20.0
    assert client.post("/api/paper/account/stop", json={"kind": "sim"}, headers=local).json()["running"] is False
    assert client.post("/api/paper/account/scan", json={"kind": "sim"}, headers=local).status_code == 400
    r = client.post("/api/paper/account/start", json={"kind": "sim"}, headers=local).json()
    assert r["running"] is True
    # real cannot start without the API wallet configured
    assert client.post("/api/paper/account/start", json={"kind": "real"}, headers=local).status_code == 400
    assert client.post("/api/paper/account/start", json={"kind": "sim"}).status_code == 403   # token required
