"""
E-mail alerts when a position is opened or closed (Gmail SMTP with an app password; nothing else is sent).
Configure in .env: NOTIFY_EMAIL_TO, NOTIFY_EMAIL_FROM (the Gmail account that sends) and NOTIFY_EMAIL_APP_PASSWORD
(Google Account → Security → App passwords). NOTIFY_ACCOUNTS chooses which accounts alert: "real" (default), "real,sim".
Sending runs in a background thread so a slow mail server never delays a trade.
"""
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

logger = logging.getLogger("solana.notify")


def configured() -> bool:
    return bool(os.getenv("NOTIFY_EMAIL_TO") and os.getenv("NOTIFY_EMAIL_FROM") and os.getenv("NOTIFY_EMAIL_APP_PASSWORD"))


def accounts() -> set[str]:
    return {a.strip() for a in os.getenv("NOTIFY_ACCOUNTS", "real").split(",") if a.strip()}


def _send(subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.getenv("NOTIFY_EMAIL_FROM"), os.getenv("NOTIFY_EMAIL_TO")
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(os.getenv("NOTIFY_EMAIL_FROM"), os.getenv("NOTIFY_EMAIL_APP_PASSWORD"))
            s.send_message(msg)
    except Exception as e:
        logger.warning(f"e-mail alert failed: {e}")


def trade_event(account: str, kind: str, symbol: str, strategy: str | None, reason: str, trade: dict | None = None):
    """kind: entry (position opened) or exit (closed); trade = the closed round trip, for the P&L line."""
    if kind not in ("entry", "exit") or account not in accounts() or not configured():
        return
    where = "CONTA REAL" if account.startswith("real") else "simulação"
    if kind == "entry":
        subject = f"Kairos · 🟢 comprou {symbol} ({where})"
        lines = [f"Posição aberta em {symbol} na {where}.", f"Bot: {strategy or '–'}", f"Detalhe: {reason}"]
    else:
        pnl = (trade or {}).get("pnl_usd")
        tag = "" if pnl is None else (f" +${pnl:.2f}" if pnl >= 0 else f" −${abs(pnl):.2f}")
        subject = f"Kairos · {'✅' if (pnl or 0) >= 0 else '🔻'} fechou {symbol}{tag} ({where})"
        lines = [f"Posição fechada em {symbol} na {where}.", f"Bot: {strategy or '–'}", f"Motivo: {reason}"]
        if trade:
            lines += [f"Entrada {trade['entry_price']:.6g} → saída {trade['exit_price']:.6g}",
                      f"Resultado: ${trade['pnl_usd']:+.4f} ({trade['pnl_pct'] * 100:+.2f}%, já com taxas e funding)"]
    lines += ["", "Painel: http://127.0.0.1:8000"]
    threading.Thread(target=_send, args=(subject, "\n".join(lines)), daemon=True).start()
