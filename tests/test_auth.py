from fastapi.testclient import TestClient

from src.api import auth
from src.main import app


def remote():
    # TestClient's host is "testserver" — not loopback, so it behaves like a request through the tunnel
    return TestClient(app, base_url="https://kairos.example")


def test_remote_needs_login_and_is_read_only(monkeypatch):
    monkeypatch.setattr(auth, "PASS_HASH", auth.hash_password("1914"))
    monkeypatch.setattr(auth, "_locked_until", 0.0)
    auth._fails.clear()
    c = remote()
    assert c.get("/api/paper/arena").status_code == 401
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"user": "root", "password": "nope"}).status_code == 401
    r = c.post("/login", data={"user": "root", "password": "1914"}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    assert c.get("/api/paper/health").status_code == 200
    assert c.post("/api/paper/account/start", json={"kind": "sim"}).status_code == 403   # read-only


def test_lockout_after_five_wrong(monkeypatch):
    monkeypatch.setattr(auth, "PASS_HASH", auth.hash_password("1914"))
    monkeypatch.setattr(auth, "_locked_until", 0.0)
    auth._fails.clear()
    assert [auth.check_login("root", "x", now=100) for _ in range(5)][-1] == "locked"
    assert auth.check_login("root", "1914", now=200) == "locked"
    assert auth.check_login("root", "1914", now=100 + 15 * 60 + 1) == "ok"


def test_session_signature_and_expiry():
    tok = auth.make_session("root", now=1000)
    assert auth.valid_session(tok, now=1001)
    assert not auth.valid_session(tok, now=1000 + auth.SESSION_S + 1)
    assert not auth.valid_session(tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1001)
