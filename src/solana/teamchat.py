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
NAMES = {"short": "Gustavo Gómez", "rules": "São Marcos", "trend": "Endrick", "trend4h": "Gabriel Jesus", "whales": "Evair", "whales2": "Dudu",
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
VOICE["short"] = ("Subiu demais numa baixa: vendi {s} a descoberto 📉", "Zaga firme: {s} caiu e eu lucrei.",
                  "{s} não caiu como eu esperava. Fecho e espero a próxima.", "@{to}, cuidado com {s}: {d}",
                  "Bela, @{to}! A defesa agradece 📉", "Calma, @{to}. Quando o mercado vira, a gente segura.")
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
            _comment_entry(author, symbol)
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
        elif kind == "btc_filter" and source == "real":
            post("rede", "network", reason, throttle=False)
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


def _coin_record(model: str, symbol: str) -> dict | None:
    """This bot's own closed-trade record on one coin (the learning memory), or None."""
    from src.solana import arena, learning
    eng = (arena._bots or {}).get(model)
    return learning.record(eng.ledger).get(f"moeda {symbol}") if eng else None


def _record_txt(r: dict | None) -> str:
    if not r or not r.get("n"):
        return ""
    return f"{r['wins']}/{r['n']} ganhos, {'+' if r['pnl'] >= 0 else '−'}${abs(r['pnl']):.2f}"


def _reply_to_tip(target: str, tipper: str, symbol: str, held: set):
    """The tipped bot answers with its own context: holding it, its record on the coin, or that it will watch."""
    rec = _record_txt(_coin_record(target, symbol))
    if symbol in held:
        txt = f"Valeu, @{name(tipper)}! Já estou posicionado em {symbol}."
    elif rec:
        txt = f"Valeu, @{name(tipper)}! Meu histórico em {symbol}: {rec}. Se a regra bater, eu entro."
    else:
        txt = f"Valeu, @{name(tipper)}! Nunca operei {symbol} — fico de olho e entro só se a minha regra confirmar."
    post(target, "chat", txt, symbol, throttle=False)


def _comment_entry(author: str, symbol: str):
    """When a bot buys, a teammate with real history on that coin shares it (good or bad)."""
    from src.solana import arena
    best = None
    for m in (arena._bots or {}):
        if m == author:
            continue
        r = _coin_record(m, symbol)
        if r and r["n"] >= 3 and (best is None or r["n"] > best[1]["n"]):
            best = (m, r)
    if not best:
        return
    m, r = best
    if r["pnl"] >= 0:
        txt = f"@{name(author)}, boa escolha: em {symbol} eu tenho {_record_txt(r)}. Segura o stop que ela costuma andar."
    else:
        txt = f"@{name(author)}, cuidado com {symbol}: eu tenho {_record_txt(r)} nela. Protege o lucro cedo se subir."
    post(m, "chat", txt, symbol)


def market_update():
    """The network posts a market snapshot from real data: breadth, movers, strongest whale buying, top traders."""
    from src.api.paper_routes import _market_temp
    from src.solana import toptraders, whales
    mt = _market_temp()
    if not mt.get("n"):
        return
    up = f"{mt['uptrend']}/{mt['n']} moedas em alta, média 24h {mt.get('avg_ch24', 0):+.1f}%"
    best = ", ".join(f"{c} {v:+.1f}%" for c, v in (mt.get("best") or [])[:3])
    worst = ", ".join(f"{c} {v:+.1f}%" for c, v in (mt.get("worst") or [])[:2])
    coins = [w["symbol"] for w in mt.get("watch") or []]
    flows = sorted(((c, (whales.summary(c).get("1h") or {}).get("net_usd", 0)) for c in coins), key=lambda x: -x[1])
    whale = f"baleias comprando forte: {flows[0][0]} (${flows[0][1]:,.0f} líquidos em 1h)" if flows and flows[0][1] > 0 else "baleias sem compra forte"
    cons = sorted(((c, toptraders.consensus(c)) for c in coins), key=lambda x: -x[1].get("long", 0))
    top = f"top traders mais comprados em {cons[0][0]} ({cons[0][1]['long']} de {cons[0][1]['of']})" if cons and cons[0][1].get("long") else ""
    post("rede", "network", f"📊 Mercado agora: {up}. Sobem: {best}. Caem: {worst}. {whale}{f'; {top}' if top else ''}.", throttle=False)


def coaching():
    """A titular coaches a reserve at risk of the next cut, with the reserve's real numbers and its worst situation."""
    from src.solana import arena, learning
    tits = [m for m in (arena._bots or {}) if arena.is_titular(m)]
    for m, eng in list((arena._bots or {}).items()):
        a = arena.advise(m) or {}
        if a.get("flag") != "cut" or not tits:
            continue
        rec = learning.record(eng.ledger, m)
        total = learning._total(rec)
        worst = min(((t, r) for t, r in rec.items() if r["n"] >= 3 and r["n"] <= total / 2 and not t.startswith("moeda")),
                    key=lambda x: x[1]["pnl"], default=None)
        p = a.get("period") or {}
        coach = _pick(f"{m}{int(time.time() // 3600)}", tits)
        tip = f" O que mais te custa: '{worst[0]}' ({worst[1]['wins']}/{worst[1]['n']}, −${abs(worst[1]['pnl']):.2f}) — evita." if worst and worst[1]["pnl"] < 0 else ""
        post(coach, "chat", f"@{name(m)}, você está {p.get('wins', 0)}/{p.get('trades', 0)} neste período, precisa de 60% até o corte.{tip}", m)


def chatter():
    """Scheduler job (every 5 min): bots tip each other about coins close to a teammate's buy rule and the tipped bot
    answers with its own context; every 30 min a market snapshot; every hour titulares coach reserves at risk."""
    from src.api.paper_routes import _market_temp, _opportunities
    from src.solana import arena
    bots = list((arena._bots or {}).items())
    watch = _market_temp().get("watch") or []
    slot = int(time.time() // 300)
    for target, eng in bots:
        held = {p["symbol"] for p in eng.ledger.positions()}
        opp = next((o for o in _opportunities(target, watch, held) if o["status"] in ("pronto", "perto") and not o["held"]), None)
        if not opp:
            continue
        mates = [m for m, _ in bots if m != target]
        tipper = _pick(f"{target}{opp['symbol']}{slot}", mates)
        if tipper and post(tipper, "tip", _voice(tipper, 3).format(to=name(target), s=opp["symbol"], d=opp["detail"]), opp["symbol"]):
            _reply_to_tip(target, tipper, opp["symbol"], held)
    if slot % 6 == 0:
        market_update()
    if slot % 12 == 0:
        coaching()


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
