from datetime import datetime, timedelta, timezone

from src.solana import routine


def test_projection_scales_with_position_size():
    p = routine.projection(12)
    assert p["expected_usd_per_trade"] == round(0.2085 * 12 / 100, 4)
    assert 0 < p["expected_usd_24h"] < p["expected_usd_per_trade"]      # < 1 trade/day expected
    assert p["most_likely"] == "0 trades"


def test_summarize_account_keeps_only_the_window():
    now = datetime.now(timezone.utc)
    old, new = (now - timedelta(hours=30)).isoformat(), (now - timedelta(hours=2)).isoformat()
    acc = {"running": True, "trades": [{"exit_ts": new, "pnl_usd": 0.1}, {"exit_ts": old, "pnl_usd": -1}],
           "decisions": [{"ts": new, "kind": "veto", "reason": "no dip"}, {"ts": old, "kind": "entry"}], "team": []}
    daily = [{"trades": 1, "wins": 1, "losses": 0, "pnl_usd": 0.1, "ai_cost_usd": 0.01, "net_usd": 0.09}]
    s = routine.summarize_account(acc, daily, hours=24)
    assert [t["pnl_usd"] for t in s["trades_window"]] == [0.1]
    assert s["decision_kinds_window"] == {"veto": 1}
    assert s["week"]["net_usd"] == 0.09
