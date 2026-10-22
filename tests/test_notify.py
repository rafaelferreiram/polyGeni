from src.solana import notify


def test_one_html_email_per_operation_on_chosen_accounts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda subject, body, html=None: sent.append((subject, body, html)))
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    for k, v in {"NOTIFY_EMAIL_TO": "me@example.com", "NOTIFY_EMAIL_FROM": "bot@example.com",
                 "NOTIFY_EMAIL_APP_PASSWORD": "x", "NOTIFY_ACCOUNTS": "real"}.items():
        monkeypatch.setenv(k, v)
    buy = {"ts": "2026-10-22T01:30:00+00:00", "qty": 0.12, "price": 100.0, "usd_gross": 12.0, "fee_usd": 0.005, "strategy": "rules"}
    notify.trade_event("real", "entry", "BTC", "rules", "dip below band; size $12", fill=buy)
    notify.trade_event("sim", "entry", "ETH", "rules", "sim trades are not e-mailed by default", fill=buy)
    notify.trade_event("real", "veto", "SOL", "rules", "vetoes are never e-mailed")
    notify.trade_event("real", "exit", "BTC", "rules", "take-profit",
                       {"entry_price": 100, "exit_price": 104, "pnl_usd": 0.42, "pnl_pct": 0.035, "entry_ts": "2026-10-22T01:30:00+00:00"},
                       fill={**buy, "ts": "2026-10-22T03:00:00+00:00", "price": 104.0, "usd_gross": 12.48})
    notify.trade_event("meme_real", "entry", "FROG", "meme_whale", "whales", fill={**buy, "usd_gross": 5.0})
    assert [s for s, _, _ in sent] == ["Savoia · 🟢 COMPRA BTC $12.00 · Conta real",
                                       "Savoia · ✅ VENDA BTC +$0.42 · Conta real",
                                       "Savoia · 🟢 COMPRA FROG $5.00 · Conta real de meme coins"]
    html = sent[0][2]
    assert "22/10/2026 às 02:30:00 (Lisboa)" in html and "0.12" in html and "$100" in html and "rules" in html   # Lisbon = UTC+1 in October
    assert "+3.50%" in sent[1][1] and "<html" in sent[1][2]
    assert "ENSAIO" in sent[2][2]                                                        # dry run is labelled as such
    monkeypatch.delenv("NOTIFY_EMAIL_APP_PASSWORD")
    notify.trade_event("real", "entry", "BTC", "rules", "not configured -> silent", fill=buy)
    assert len(sent) == 3
