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


# ─── personalities: each bot talks like its player ───
NAMES = {"rules": "São Marcos", "trend": "Endrick", "trend4h": "Gabriel Jesus", "whales": "Evair", "whales2": "Dudu",
         "copy": "Veiga", "whales_15m": "Evair 15m", "copy_15m": "Veiga 15m", "trend4h_15m": "Gabriel Jesus 15m"}
VOICE = {   # family -> (buy, win, loss, tip, cheer, calm)
    "rules": ("Calma, pessoal. Esperei a queda certa e entrei em {s}.", "Paciência paga. {s} fechado no verde.",
              "Faz parte, {s} não respeitou. Sigo esperando a próxima queda limpa.", "@{to}, de olho em {s} pra você: {d}",
              "Boa, @{to}! Defesa e ataque juntos 🧤", "Respira, @{to}. Ninguém ganha todas — foco no próximo."),
    "trend": ("Rompeu, eu vou! Partiu {s} 🚀", "É gol! {s} no verde ⚡", "Tomei um drible em {s}. Bola pra frente!",
              "@{to}, olha {s}: {d}", "Que jogada, @{to}! ⚡", "Levanta a cabeça, @{to}, o jogo é longo!"),
    "trend4h": ("Tendência firme no 4h, entrei em {s}.", "Com raça! {s} fechou bem.", "{s} virou contra mim. Vou ajustar.",
                "@{to}, {s} tá com cara de ser sua: {d}", "Isso, @{to}! 💪", "Vamos juntos, @{to}, a gente vira."),
    "whales": ("As baleias entraram pesado em {s}, eu vou junto 🐋", "Matador! {s} no verde.", "As baleias me enganaram em {s}.",
               "@{to}, vi {s} no radar, é sua: {d} 🐋", "Golaço, @{to}! 🐋", "Acontece, @{to}. As baleias também erram."),
    "copy": ("Os craques do top 10 entraram em {s}, copiei.", "Camisa 10 entrega: {s} no verde.", "Os craques erraram em {s}, eu junto.",
             "@{to}, anota aí {s}: {d}", "Bonito, @{to}! 🎯", "Bola pra frente, @{to}."),
}
VOICE["whales2"] = VOICE["whales"]


def name(model: str) -> str:
    try:
        from src.solana import evolution
        lab = evolution.load()["bots"].get(model)
        if lab:
            return lab["label"]
    except Exception:
        pass
    return NAMES.get(model, model)


def _family(model: str) -> str:
    try:
        from src.solana import evolution
        model = evolution.base_of(model)
    except Exception:
        pass
    return model.removesuffix("_15m")


def _voice(model: str, i: int) -> str:
    return VOICE.get(_family(model), VOICE["trend4h"])[i]


def _pick(seed: str, options: list):
    return options[sum(map(ord, seed)) % len(options)] if options else None


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
            odds = reason.split("· chance")[-1].split(",")[0].strip() if "· chance" in reason else None
            line = _voice(author, 0).format(s=symbol)
            if "recebida de" in reason:
                giver = reason.split("recebida de ")[-1].split(":")[0]
                line = f"Valeu pelo passe, @{name(giver)}! Entrei em {symbol}."
            post(author, "entry", f"{line}{f' ({where})' if where else ''}{f' · chance {odds}' if odds else ''}", symbol, throttle=False)
        elif kind == "exit" and symbol:
            pnl = sig.get("pnl_usd")
            res = f" · {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}" if isinstance(pnl, (int, float)) else ""
            win = isinstance(pnl, (int, float)) and pnl > 0
            post(author, "exit", f"{_voice(author, 1 if win else 2).format(s=symbol)} ({_exit_pt(reason)}{res}){f' · {where}' if where else ''}",
                 symbol, throttle=False)
            _react(author, symbol, win)
        elif kind == "handoff" and symbol:
            to = (signals or {}).get("to")
            why = reason.split(": ", 1)[-1]
            post(author, "handoff", f"@{name(to) if to else '?'}, essa é mais a sua: {symbol} — {why}", symbol, throttle=False)
        elif kind == "veto" and symbol and ("aprendizado" in reason or "rede do time" in reason):
            post(author, "lesson", f"Pulei {symbol}: {reason.replace('aprendizado: ', '')}", symbol)
        elif kind == "evolution":
            post(author, "evolution", reason, throttle=False)
        elif kind == "quota" and reason.startswith("⏱"):
            post(author, "quota", reason)
    except Exception:
        pass        # the chat must never break trading


def _react(author: str, symbol: str, win: bool):
    """A teammate answers a sell now and then (one in three), cheering a win or calming a loss — by name."""
    from src.solana import arena
    mates = [m for m in (arena._bots or {}) if m != author]
    seed = f"{author}{symbol}{int(time.time() // 600)}"
    if not mates or sum(map(ord, seed)) % 3:
        return
    mate = _pick(seed, mates)
    post(mate, "chat", _voice(mate, 4 if win else 5).format(to=name(author)), symbol, throttle=False)


def chatter():
    """Scheduler job: bots tip each other about coins close to a teammate's buy rule (the 'Próximas trades' of each)."""
    from src.api.paper_routes import _market_temp, _opportunities
    from src.solana import arena
    bots = list((arena._bots or {}).items())
    watch = _market_temp().get("watch") or []
    for target, eng in bots:
        held = {p["symbol"] for p in eng.ledger.positions()}
        opp = next((o for o in _opportunities(target, watch, held) if o["status"] in ("pronto", "perto") and not o["held"]), None)
        if not opp:
            continue
        mates = [m for m, _ in bots if m != target]
        tipper = _pick(f"{target}{opp['symbol']}{int(time.time() // 3600)}", mates)
        if tipper:
            post(tipper, "tip", _voice(tipper, 3).format(to=name(target), s=opp["symbol"], d=opp["detail"]), opp["symbol"])


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
