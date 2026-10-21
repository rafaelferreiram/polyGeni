"""
Connection check for every third party the bots depend on. Read-only, cheap calls only (no orders, no LLM tokens):
each check returns ok / detail / latency so the dashboard can show what is connected and what is not.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from src.solana import config as cfg

_cache: dict = {"ts": 0.0, "rows": []}


def _hl(url: str, body: dict):
    from src.solana import hyperliquid as hl
    return hl.info(body, url)           # same throttled, retrying client the bots use (no 429 from the health check)


def _hyperliquid():
    from src.solana import hyperliquid as hl
    listed = {name for name, a in hl.asset_ctxs().items() if not a.get("isDelisted")}   # cached 60s, shared with the bots
    missing = [m for m in cfg.HL_LIVE_MARKETS if m not in listed]
    return not missing, f"{len(listed)} mercados listados" + (f" · faltam {', '.join(missing)}" if missing else f" · os {len(cfg.HL_LIVE_MARKETS)} dos bots OK")


def _hl_account():
    from src.solana import hl_live
    st = hl_live.config_status()
    if st["problems"]:
        return False, "; ".join(st["problems"])
    base = hl_live.API_URLS[hl_live.network()] + "/info"
    agents = _hl(base, {"type": "extraAgents", "user": cfg.HL_ACCOUNT_ADDRESS}) or []
    if st["api_wallet"].lower() not in {a["address"].lower() for a in agents}:
        return False, f"API wallet {st['api_wallet'][:8]}… não está autorizada em {hl_live.network()}"
    spot = _hl(base, {"type": "spotClearinghouseState", "user": cfg.HL_ACCOUNT_ADDRESS})
    usdc = next((float(b["total"]) for b in spot.get("balances", []) if b["coin"] == "USDC"), 0.0)
    return True, f"{hl_live.network()} · API wallet autorizada · saldo ${usdc:.2f}"


def _anthropic():
    if not cfg.ANTHROPIC_API_KEY:
        return False, "ANTHROPIC_API_KEY em falta no .env"
    r = httpx.get("https://api.anthropic.com/v1/models", timeout=10,
                  headers={"x-api-key": cfg.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    ids = {m["id"] for m in r.json().get("data", [])}
    llm = sorted({cfg.DEBATE_MODEL, *(m for m in cfg.ARENA_MODELS if m.startswith("claude"))})
    missing = [m for m in llm if m not in ids]
    return not missing, "chave válida" + (f" · modelo indisponível: {', '.join(missing)}" if missing else f" · {', '.join(llm)} OK")


def _whales():
    from src.solana import whales
    st = whales.status()
    if st["connected"]:
        return True, f"WebSocket ligado · {st['messages']} lotes de trades recebidos"
    return (None if not st["since"] and not st["error"] else False), st["error"] or "a ligar…"


# (name, what it is used for, check). None as ok = not used / optional, shown grey.
CHECKS = [
    ("Hyperliquid (dados)", "preços, candles e order books dos 29 mercados", _hyperliquid),
    ("Hyperliquid (conta real)", "ordens reais com stop e alvo", _hl_account),
    ("Hyperliquid (baleias)", "feed ao vivo de ordens grandes nas 29 moedas", _whales),
    ("Notícias cripto", "CoinDesk, Cointelegraph, Decrypt, The Block → bots e painel",
     lambda: (lambda st: (st["ok"], ", ".join(f"{k} {v}" for k, v in st["sources"].items()) or "a carregar…"))(
         __import__("src.solana.news", fromlist=["status"]).status())),
    ("Top traders Hyperliquid", "ranking público + posições dos 10 melhores a cada 2 min",
     lambda: (lambda st, n: (bool(n) if st["polled_at"] or st["error"] else None,
                             f"{n} traders seguidos" + (f" · {st['error']}" if st["error"] else "") if n else (st["error"] or "a carregar ranking…")))(
         __import__("src.solana.toptraders", fromlist=["x"])._status, len(__import__("src.solana.toptraders", fromlist=["x"])._traders))),
    ("Anthropic Claude", "revisor das compras dos titulares (Haiku)", _anthropic),
]


def check_all(max_age: float = 60) -> list[dict]:
    if time.time() - _cache["ts"] < max_age:
        return _cache["rows"]

    def run(item):
        name, role, fn = item
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:140]}"
        return {"name": name, "role": role, "ok": ok, "detail": detail, "ms": round((time.time() - t0) * 1000)}

    with ThreadPoolExecutor(len(CHECKS)) as ex:
        rows = list(ex.map(run, CHECKS))
    _cache.update(ts=time.time(), rows=rows)
    return rows
