"""
The team's group chat ("Vestiário"): the bots tell each other what they do and learn — buys, sells with results,
opportunities passed to a teammate, situations they now avoid, team lessons, cuts and newborn successors. It is the
team's shared memory and history (persisted in team_chat.db), and what the dashboard shows as a group conversation.
Repeated messages of the same kind about the same coin are throttled so the chat stays readable.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from src.solana import config as cfg

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_recent: dict[tuple, float] = {}
THROTTLE_S = 1800            # the same author/kind/coin at most once per 30 min (exits and passes are never throttled)


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(cfg.TEAM_CHAT_DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.execute("""CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, author TEXT,
            kind TEXT, symbol TEXT, text TEXT, data TEXT)""")
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return _conn


def post(author: str, kind: str, text: str, symbol: str | None = None, data: dict | None = None, throttle: bool = True) -> bool:
    key = (author, kind, symbol)
    now = time.time()
    if throttle and now - _recent.get(key, 0) < THROTTLE_S:
        return False
    _recent[key] = now
    with _lock:
        db().execute("INSERT INTO chat (ts, author, kind, symbol, text, data) VALUES (?,?,?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"), author, kind, symbol, text[:400],
                      json.dumps(data or {}, default=str)))
    return True


def recent(limit: int = 80, since: int = 0) -> list[dict]:
    with _lock:
        rows = db().execute("SELECT id, ts, author, kind, symbol, text FROM chat WHERE id > ? ORDER BY id DESC LIMIT ?",
                            (since, limit)).fetchall()
    return [{"id": i, "ts": ts, "author": a, "kind": k, "symbol": s, "text": t} for i, ts, a, k, s, t in reversed(rows)]


def meta(k: str, v: str | None = None) -> str | None:
    with _lock:
        if v is not None:
            db().execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
            return v
        row = db().execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else None


def _exit_pt(reason: str) -> str:
    r = reason or ""
    return ("bateu o alvo 🎯" if "take-profit" in r else "parou no stop 🛑" if "stop-loss" in r
            else "tempo máximo ⏱" if "max holding" in r else "venda manual ✋" if "manual" in r else r[:60])


def from_decision(source: str, model: str | None, kind: str, symbol: str | None, reason: str, signals: dict):
    """Turn a bot's decision into a chat message (called by the ledger for every decision)."""
    try:
        author = model or source
        where = "💰 conta real" if source == "real" else "🧪 simulação" if source == "sim" else None
        sig = signals or {}
        if kind == "entry" and symbol:
            setup = (reason.split(";")[0] if any(w in reason for w in ("baleias", "top", "recebida", "queda", "rompeu", "adotada"))
                     else ", ".join((sig.get("tags") or [])[:3]) or reason[:60])
            post(author, "entry", f"Comprei {symbol}{f' ({where})' if where else ''}: {setup}", symbol, throttle=False)
        elif kind == "exit" and symbol:
            pnl = sig.get("pnl_usd")
            res = f" · {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}" if isinstance(pnl, (int, float)) else ""
            post(author, "exit", f"Vendi {symbol}{f' ({where})' if where else ''}: {_exit_pt(reason)}{res}", symbol, throttle=False)
        elif kind == "handoff" and symbol:
            post(author, "handoff", reason.replace("📨 ", ""), symbol, throttle=False)
        elif kind == "veto" and symbol and ("aprendizado" in reason or "rede do time" in reason):
            post(author, "lesson", f"Pulei {symbol}: {reason.replace('aprendizado: ', '')}", symbol)
        elif kind == "evolution":
            post(author, "evolution", reason, throttle=False)
        elif kind == "quota" and reason.startswith("⏱"):
            post(author, "quota", reason)
    except Exception:
        pass        # the chat must never break trading


def team_lessons(avoid: list[dict]):
    """When the shared network learns (or forgets) a situation to avoid, say it once in the chat."""
    now = sorted(a["tag"] for a in avoid)
    before = json.loads(meta("team_avoid") or "[]")
    if now == before:
        return
    for a in avoid:
        if a["tag"] not in before:
            post("rede", "network", f"📚 Aprendemos juntos: '{a['tag']}' tem dado errado ({a['wins']}/{a['n']} ganhos, "
                                    f"{'+' if a['pnl'] >= 0 else '−'}${abs(a['pnl']):.2f}) — todo mundo evita, menos quem vai bem nela", throttle=False)
    for tag in before:
        if tag not in now:
            post("rede", "network", f"✅ '{tag}' voltou a funcionar para o time — liberado de novo", throttle=False)
    meta("team_avoid", json.dumps(now))
