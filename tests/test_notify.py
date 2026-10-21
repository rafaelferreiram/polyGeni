from src.solana import notify


def test_alerts_only_for_trades_on_chosen_accounts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    for k, v in {"NOTIFY_EMAIL_TO": "me@example.com", "NOTIFY_EMAIL_FROM": "bot@example.com",
                 "NOTIFY_EMAIL_APP_PASSWORD": "x", "NOTIFY_ACCOUNTS": "real"}.items():
        monkeypatch.setenv(k, v)
    notify.trade_event("real", "entry", "BTC", "rules", "dip below band; size $12")
    notify.trade_event("sim", "entry", "ETH", "rules", "sim trades are not e-mailed by default")
    notify.trade_event("real", "veto", "SOL", "rules", "vetoes are never e-mailed")
    notify.trade_event("real", "exit", "BTC", "rules", "take-profit",
                       {"entry_price": 100, "exit_price": 104, "pnl_usd": 0.42, "pnl_pct": 0.035})
    assert [s for s, _ in sent] == ["Kairos · 🟢 comprou BTC (CONTA REAL)", "Kairos · ✅ fechou BTC +$0.42 (CONTA REAL)"]
    assert "+3.50%" in sent[1][1]
    monkeypatch.delenv("NOTIFY_EMAIL_APP_PASSWORD")
    notify.trade_event("real", "entry", "BTC", "rules", "not configured -> silent")
    assert len(sent) == 2
