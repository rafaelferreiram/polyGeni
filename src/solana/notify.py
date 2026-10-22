"""
E-mail for every operation: one HTML message per buy and per sell (asset, date and time in Lisbon, value, quantity,
price, bot, account, reason and the result on a sell). Gmail SMTP with an app password; nothing else is sent.
Configure in .env: NOTIFY_EMAIL_TO, NOTIFY_EMAIL_FROM (the Gmail account that sends) and NOTIFY_EMAIL_APP_PASSWORD
(Google Account → Security → 2-Step Verification → App passwords). NOTIFY_ACCOUNTS chooses which accounts alert:
"real" (default: the Hyperliquid real account and the real meme account, its dry run included and labelled), "real,sim".
Sending runs in a background thread so a slow mail server never delays a trade.
"""
import logging
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from zoneinfo import ZoneInfo

logger = logging.getLogger("solana.notify")
TZ = ZoneInfo("Europe/Lisbon")


def configured() -> bool:
    return bool(os.getenv("NOTIFY_EMAIL_TO") and os.getenv("NOTIFY_EMAIL_FROM") and os.getenv("NOTIFY_EMAIL_APP_PASSWORD"))


def accounts() -> set[str]:
    return {a.strip() for a in os.getenv("NOTIFY_ACCOUNTS", "real").split(",") if a.strip()}


def _send(subject: str, body: str, html: str | None = None):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.getenv("NOTIFY_EMAIL_FROM"), os.getenv("NOTIFY_EMAIL_TO")
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(os.getenv("NOTIFY_EMAIL_FROM"), os.getenv("NOTIFY_EMAIL_APP_PASSWORD"))
            s.send_message(msg)
    except Exception as e:
        logger.warning(f"e-mail alert failed: {e}")


def _where(account: str) -> tuple[str, str]:
    """(bucket for NOTIFY_ACCOUNTS, label shown in the e-mail)."""
    if account == "real":
        return "real", "Conta real · Hyperliquid"
    if account.startswith("real-"):
        return "real", f"Conta real · Hyperliquid ({account[5:]})"
    if account == "meme_real":
        from src.solana import config as cfg
        return "real", "Conta real de meme coins · Solana" + ("" if cfg.MEME_REAL_MODE == "real" else " · ENSAIO (simulado, nada enviado)")
    if account == "sim":
        return "sim", "Simulação"
    return "lab", f"Laboratório · {account.removeprefix('lab:')}"


def _when(iso: str | None) -> str:
    d = datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc)
    return d.astimezone(TZ).strftime("%d/%m/%Y às %H:%M:%S") + " (Lisboa)"


def _money(v) -> str:
    return "–" if v is None else f"${v:,.2f}"


def _qty(v) -> str:
    return "–" if v is None else f"{v:,.6f}".rstrip("0").rstrip(".")


def _html(title: str, color: str, rows: list[tuple[str, str]], note: str) -> str:
    tr = "".join(f'<tr><td style="padding:6px 10px;color:#6b7280;white-space:nowrap">{escape(k)}</td>'
                 f'<td style="padding:6px 10px;font-weight:600">{escape(v)}</td></tr>' for k, v in rows)
    return f"""<!doctype html><html><body style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111827">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">
<div style="background:{color};color:#fff;padding:16px 20px;font-size:18px;font-weight:700">{escape(title)}</div>
<table style="border-collapse:collapse;width:100%;font-size:14px;margin:8px 0">{tr}</table>
<div style="padding:12px 20px 18px;font-size:12px;color:#6b7280">{escape(note)}<br>
<a href="http://127.0.0.1:8000" style="color:#059669">Painel do Savoia</a> · Savoia envia um e-mail por operação.</div>
</div></body></html>"""


def trade_event(account: str, kind: str, symbol: str, strategy: str | None, reason: str, trade: dict | None = None,
                fill: dict | None = None):
    """kind: entry (buy) or exit (sell); fill = that operation's fill (date, quantity, price, value); trade = the
    closed round trip on a sell, for the result line."""
    if kind not in ("entry", "exit") or not configured():
        return
    bucket, where = _where(account)
    if bucket not in accounts():
        return
    f = fill or {}
    qty, price = f.get("qty"), f.get("price")
    value = f.get("usd_gross")
    if value is None and qty and price:
        value = qty * price
    bot = strategy or f.get("strategy") or "–"
    rows = [("Ativo", symbol), ("Operação", "Compra" if kind == "entry" else "Venda"), ("Data e hora", _when(f.get("ts"))),
            ("Valor", _money(value)), ("Quantidade", _qty(qty)), ("Preço", "–" if price is None else f"${price:,.6g}"),
            ("Taxas", _money(f.get("fee_usd")) if f.get("fee_usd") is not None else "–"), ("Bot", bot), ("Conta", where)]
    if kind == "entry":
        subject = f"Savoia · 🟢 COMPRA {symbol} {_money(value)} · {where.split(' · ')[0]}"
        title, color = f"🟢 Compra de {symbol}", "#059669"
    else:
        pnl = (trade or {}).get("pnl_usd")
        tag = "" if pnl is None else (f" +${pnl:.2f}" if pnl >= 0 else f" −${abs(pnl):.2f}")
        subject = f"Savoia · {'✅' if (pnl or 0) >= 0 else '🔻'} VENDA {symbol}{tag} · {where.split(' · ')[0]}"
        title, color = f"{'✅' if (pnl or 0) >= 0 else '🔻'} Venda de {symbol}", "#059669" if (pnl or 0) >= 0 else "#dc2626"
        if trade:
            rows += [("Entrada → saída", f"${trade['entry_price']:,.6g} → ${trade['exit_price']:,.6g}  (aberta em {_when(trade['entry_ts'])})"),
                     ("Resultado", f"{'+' if trade['pnl_usd'] >= 0 else '−'}${abs(trade['pnl_usd']):.4f} ({trade['pnl_pct'] * 100:+.2f}%, já com taxas e funding)")]
    rows.append(("Motivo", reason))
    text = "\n".join(f"{k}: {v}" for k, v in rows) + "\n\nPainel: http://127.0.0.1:8000"
    threading.Thread(target=_send, args=(subject, text, _html(title, color, rows, where)), daemon=True).start()
