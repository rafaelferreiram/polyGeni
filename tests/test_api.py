"""/api/paper endpoints: local-only + token guard, paper funds, TWR excluding cash flows."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import paper_routes as pr


@pytest.fixture
def client(monkeypatch):
    from src.api import auth
    from src.main import app
    monkeypatch.setattr(auth, "is_local", lambda request: True)   # remote login is tested in test_auth.py
    return TestClient(app)


@pytest.fixture
def local(monkeypatch):
    # TestClient connects as host "testclient" to "testserver"; treat them as loopback for these tests
    monkeypatch.setattr(pr, "LOCAL_CLIENTS", pr.LOCAL_CLIENTS | {"testclient"})
    monkeypatch.setattr(pr, "LOCAL_HOSTNAMES", pr.LOCAL_HOSTNAMES | {"testserver"})
    return {"X-Paper-Token": pr.API_TOKEN}


def test_mutations_rejected_from_non_loopback(client):
    r = client.post("/api/paper/arena/turbo", json={"bot": "trend"}, headers={"X-Paper-Token": pr.API_TOKEN})
    assert r.status_code == 403
    assert client.get("/api/paper/token").status_code == 403
    assert client.post("/api/paper/account/start", json={"kind": "sim"}).status_code == 403


def test_mutations_need_token(client, local):
    assert client.post("/api/paper/arena/turbo", json={"bot": "trend"}).status_code == 403
    assert client.post("/api/paper/arena/turbo", json={"bot": "trend"},
                       headers={"X-Paper-Token": "wrong"}).status_code == 403
    assert client.get("/api/paper/token").json()["token"] == pr.API_TOKEN


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


def test_account_sim_and_real_share_shape_and_controls(client, local, monkeypatch, tmp_path):
    from src.solana import config as scfg
    from src.solana import hl_live
    monkeypatch.setattr(scfg, "SHADOW_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setattr(scfg, "LIVE_DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.setattr(hl_live, "_shadow", None)
    monkeypatch.setattr(hl_live, "_live", None)
    monkeypatch.setattr(hl_live, "shadow_cycle", lambda: None)
    from src.solana import arena                   # never start/stop the real lab bots from a test
    monkeypatch.setattr(arena, "set_running", lambda on: {"running": on, "llm_bots_need_key": False})
    monkeypatch.setattr(arena, "run_round", lambda: {})
    for var in ("HL_API_WALLET_KEY", "HL_API_WALLET_KEY_TESTNET", "HL_API_WALLET_KEY_MAINNET"):
        monkeypatch.delenv(var, raising=False)
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


def test_admin_metrics_page_data(client, monkeypatch, tmp_path):
    from src.solana import arena, config as scfg, costs, hl_live
    monkeypatch.setattr(scfg, "SHADOW_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setattr(scfg, "LIVE_DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.setattr(hl_live, "_shadow", None)
    monkeypatch.setattr(hl_live, "_live", None)
    monkeypatch.setattr(arena, "_bots", {})
    book = costs.CostBook(str(tmp_path / "costs.db"))
    book.record("claude-haiku-5-5", "debate", {"input_tokens": 1000, "output_tokens": 100})
    monkeypatch.setattr(costs, "_book", book)
    d = client.get("/api/paper/admin").json()
    assert d["ai"]["by_who"][0]["key"] == "debate" and d["ai"]["calls_30d"] == 1
    assert {"real", "sim", "lab", "scale"} <= set(d) and len(d["scale"]["scenarios"]) == 4
    assert client.get("/admin").status_code == 200


def test_dashboard_script_does_not_redeclare_globals_that_wallet_extensions_define():
    """Phantom/MetaMask inject window.phantom, window.solana, window.ethereum...: a top-level const/let/class of the
    same name is a SyntaxError for the whole script (blank dashboard in any browser with the extension)."""
    import re
    src = open("frontend/savoia.js").read()
    taken = {"phantom", "solana", "ethereum", "solflare", "backpack", "coinbaseSolana", "okxwallet", "trustwallet", "web3", "keplr"}
    declared = set(re.findall(r"^(?:const|let|var|class|function)\s+([A-Za-z_$][\w$]*)", src, re.M))
    assert not declared & taken, f"global name collides with a wallet extension: {declared & taken}"
