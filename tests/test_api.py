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


def test_phantom_connect_helpers_are_get_without_token_but_loopback_only(client, local, monkeypatch, tmp_path):
    """The page calls these two with GET (it only sends the anti-CSRF token on POST): they must not ask for it,
    yet still refuse anything that is not this machine."""
    from src.solana import meme_wallet as mw
    monkeypatch.setattr(mw.cfg, "MEME_WALLET_PATH", str(tmp_path / "w.json"))
    monkeypatch.setattr(mw, "_kp", None)
    monkeypatch.setattr(mw, "latest_blockhash", lambda: "HASH")
    r = client.get("/api/paper/meme/wallet/owner-message", params={"address": "ABC"})          # no token header
    assert r.status_code == 200 and "ABC" in r.json()["message"] and mw.address() in r.json()["message"]
    assert client.get("/api/paper/meme/wallet/blockhash").json() == {"blockhash": "HASH", "to": mw.address()}
    monkeypatch.setattr(pr, "LOCAL_CLIENTS", {"127.0.0.1"})                                      # not loopback any more
    assert client.get("/api/paper/meme/wallet/blockhash").status_code == 403
    assert client.post("/api/paper/meme/wallet/withdraw").status_code == 403                    # POSTs still need the token too


def test_ai_brief_is_generated_in_the_background_cached_and_never_needs_the_model_in_the_request(client, monkeypatch):
    import time
    from src.solana import agents, brief
    calls = []
    monkeypatch.setattr(brief.cfg, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(brief, "_snapshot", lambda eng, kind: {"conta": kind})
    monkeypatch.setattr(agents, "anthropic_llm", lambda prompt, **kw: calls.append(kw) or "• tudo calmo")
    brief._cache.clear()
    first = client.get("/api/paper/ai-brief", params={"kind": "sim"}).json()
    assert "busy" in first                                    # the request never waits for the model: it runs in a thread
    for _ in range(50):
        if brief._cache["sim"].get("text"):
            break
        time.sleep(0.05)
    second = client.get("/api/paper/ai-brief", params={"kind": "sim"}).json()
    assert second["text"] == "• tudo calmo" and second["busy"] is False
    client.get("/api/paper/ai-brief", params={"kind": "sim"})
    assert len(calls) == 1 and calls[0]["max_searches"] == 0   # cached for 20 min; no web search


def test_ai_brief_without_a_key_says_so_and_calls_nothing(client, monkeypatch):
    from src.solana import brief
    monkeypatch.setattr(brief.cfg, "ANTHROPIC_API_KEY", "")
    assert client.get("/api/paper/ai-brief").json()["error"] == "sem ANTHROPIC_API_KEY"


def test_wallet_balance_endpoints_validate_the_address_and_expose_only_public_data(client, monkeypatch):
    from src.solana import market_data as md
    assert client.get("/api/paper/wallet/solana-balance", params={"address": "not-an-address!"}).status_code == 400
    monkeypatch.setattr(md, "rpc", lambda method, params: {"value": 2_500_000_000})
    ok = client.get("/api/paper/wallet/solana-balance", params={"address": "So11111111111111111111111111111111111111112"})
    assert ok.json()["sol"] == 2.5
    assert set(client.get("/api/paper/wallet/hl-address").json()) == {"address"}
