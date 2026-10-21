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
    r = httpx.post(url, json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _hyperliquid():
    listed = {u["name"] for u in _hl(cfg.HYPERLIQUID_INFO_URL, {"type": "meta"})["universe"] if not u.get("isDelisted")}
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
    llm = [m for m in cfg.ARENA_MODELS if m.startswith("claude")]
    missing = [m for m in llm if m not in ids]
    return not missing, "chave válida" + (f" · modelo indisponível: {', '.join(missing)}" if missing else f" · {', '.join(llm)} OK")


def _coinapi():
    import os
    key = os.getenv("COINAPI_KEY", "")
    if not key:
        return None, "sem chave (opcional)"
    r = httpx.get("https://rest.coinapi.io/v1/exchangerate/BTC/USD", timeout=10, headers={"X-CoinAPI-Key": key})
    if r.status_code == 200:
        return True, f"BTC ${r.json().get('rate', 0):,.0f}"
    return None, "chave válida, conta sem créditos — não usada" if "Credits" in r.text else f"HTTP {r.status_code}"


def _get(url: str, ok_fn, **kw):
    r = httpx.get(url, timeout=10, **kw)
    r.raise_for_status()
    return ok_fn(r.json())


def _arbitrum():
    r = httpx.post(cfg.ARBITRUM_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=10)
    r.raise_for_status()
    return True, f"bloco {int(r.json()['result'], 16):,}"


def _solana():
    r = httpx.post(cfg.SOLANA_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=10)
    r.raise_for_status()
    return r.json().get("result") == "ok", str(r.json().get("result") or r.json().get("error"))[:80]


# (name, what it is used for, check). None as ok = not used / optional, shown grey.
CHECKS = [
    ("Hyperliquid (dados)", "preços, candles e order books dos 29 mercados", _hyperliquid),
    ("Hyperliquid (conta real)", "ordens reais com stop e alvo", _hl_account),
    ("Anthropic Claude", "decisões do bot de IA e debate", _anthropic),
    ("Arbitrum RPC", "saldos da MetaMask no painel", _arbitrum),
    ("Jupiter", "paper de tokens Solana (legado)",
     lambda: _get(cfg.JUPITER_QUOTE_URL, lambda j: (True, "cotação OK"),
                  params={"inputMint": "So11111111111111111111111111111111111111112",
                          "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "amount": 10**8})),
    ("DexScreener", "paper de tokens Solana (legado)",
     lambda: _get(f"{cfg.DEXSCREENER_BASE}/token-boosts/latest/v1", lambda j: (True, f"{len(j)} tokens"))),
    ("Solana RPC", "carteira Phantom e paper Solana", _solana),
    ("CoinAPI", "dados extra de mercado (opcional)", _coinapi),
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
