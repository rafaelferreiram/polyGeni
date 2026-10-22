"""X feed: pay-per-use reads under a hard daily dollar cap; tagged posts reach the AI bots."""
from datetime import datetime, timezone

import pytest

from src.solana import config as cfg
from src.solana import xfeed


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data or [], text

    def json(self):
        return {"data": self._data}


def _posts(n, start=100):
    ts = datetime.now(timezone.utc).isoformat()
    return [{"id": str(start + i), "text": f"$ZEC ripping, zcash is back {i}" if i % 2 == 0 else "pump.fun volume exploding", "created_at": ts,
             "public_metrics": {"like_count": 7, "retweet_count": 2}} for i in range(n)]


@pytest.fixture
def x(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "X_STATE_PATH", str(tmp_path / "x.json"))
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(cfg, "X_DAILY_BUDGET_USD", 0.25)
    monkeypatch.setattr(cfg, "X_POLL_MIN", 60)
    monkeypatch.setattr(cfg, "X_MAX_RESULTS", 10)
    calls = []
    monkeypatch.setattr(xfeed.httpx, "get", lambda url, params=None, headers=None, timeout=0: calls.append(params) or x.reply)
    x.reply, x.calls = Resp(data=_posts(10)), calls
    return x


def test_silent_without_a_token(x, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN")
    assert "X_BEARER_TOKEN" in xfeed.poll()["skipped"] and x.calls == []
    assert xfeed.status()["enabled"] is False


def test_a_read_is_charged_per_post_tagged_by_coin_and_never_re_read(x):
    r = xfeed.poll(now=1_000_000)
    assert r == {"ok": True, "posts": 10, "cost": 0.05}
    st = xfeed.status()
    assert st["spent_usd"] == 0.05 and st["remaining_usd"] == 0.2 and st["calls_today"] == 1
    assert xfeed.for_llm("ZEC") and all("ZEC" in p["coins"] for p in xfeed.recent(20, "zec"))      # cashtag and the name 'zcash'
    assert any("PUMP" in p["coins"] for p in xfeed.recent(20))                                       # 'pump.fun' is the PUMP token
    assert "since_id" not in x.calls[0]
    assert "intervalo" not in xfeed.poll(now=1_000_100)["skipped"] and len(x.calls) == 1             # too soon: no second call
    x.reply = Resp(data=_posts(3, start=500))
    xfeed.poll(now=1_000_000 + 3600 + 1)
    assert x.calls[1]["since_id"] == "109"                                                           # only newer posts are requested


def test_the_daily_dollar_cap_is_hard_and_reserves_the_worst_case(x, monkeypatch):
    monkeypatch.setattr(cfg, "X_DAILY_BUDGET_USD", 0.07)
    x.reply = Resp(data=_posts(3))                                                                    # costs $0.015
    assert xfeed.poll(now=1_000_000)["ok"]
    assert xfeed.poll(now=1_000_000 + 10 * 3600)["ok"] and len(x.calls) == 2                         # 0.015 + worst 0.05 = 0.065 <= 0.07
    r = xfeed.poll(now=1_000_000 + 20 * 3600)
    assert "teto do dia" in r["skipped"] and len(x.calls) == 2                                       # 0.03 + 0.05 > 0.07: no call at all


def test_errors_back_off_instead_of_hammering_and_cashtag_rejection_falls_back_to_words(x):
    x.reply = Resp(400, text="invalid operator")
    assert "palavras" in xfeed.poll(now=1_000_000)["skipped"] and xfeed.status()["query"] == "palavras"
    x.reply = Resp(402, text="payment required")
    assert "402" in xfeed.poll(now=1_000_000 + 4000)["error"]
    assert "pausado" in xfeed.poll(now=1_000_000 + 4000 + 600)["skipped"] and len(x.calls) == 2    # paused after a payment error...
    x.reply = Resp(data=_posts(2))
    assert xfeed.poll(now=1_000_000 + 4000 + 1900 + 7200)["ok"]                                    # ...and back 30 min later, once credits are added
    assert xfeed.status()["spent_usd"] == 0.01
