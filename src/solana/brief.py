"""AI read of the account (owner, 2026-10-23: "use the LLMs" on the dashboard): a short market/position summary in Portuguese.

Informational only: it never places, vetoes or sizes a trade. It is generated in a background thread (never inside a request,
see the arena/watchdog incident), cached BRIEF_TTL_S per account, and goes through anthropic_llm, so the daily/monthly AI budget
(costs.check_budget) and the cost book apply. Without a key or over budget it keeps the last text and says why.
"""
import threading
import time

from src.solana import config as cfg

BRIEF_TTL_S = 20 * 60
SYSTEM = (
    "Você é o analista de mesa de uma conta pequena de trading na Hyperliquid. Escreva em português do Brasil, no máximo 90 palavras, "
    "em 3 tópicos curtos: (1) como estão as posições abertas (lado compra/short, resultado, o que as ameaça), (2) o que o mercado e os "
    "bots estão fazendo, (3) um ponto de atenção. Use só os dados fornecidos, não invente preços nem notícias, não prometa lucro e "
    "nunca sugira aumentar alavancagem, tamanho ou remover stops. Você apenas informa; não dá ordens.")
_cache: dict[str, dict] = {}
_lock = threading.Lock()


def _snapshot(eng, kind: str) -> dict:
    from src.api.paper_routes import _live_marks, _market_temp, _regime
    p = eng.portfolio()
    pos = []
    for x in _live_marks(eng.ledger.positions()):
        short = x["mint"].endswith("~short")
        value = x.get("mark_value") or x["cost_usd"]
        pos.append({"ativo": x["symbol"], "lado": "short" if short else "compra", "bot": x["strategy"], "usd": round(x["cost_usd"], 2),
                    "resultado_usd": round(value - x["cost_usd"], 3), "aberta_em": x["opened_at"]})
    day = (eng.ledger.get_state("last_cycle") or {})
    temp = _market_temp() or {}
    decisions = [d for d in eng.ledger.decisions(40) if d["kind"] in ("entry", "exit", "veto")][:8]
    return {"conta": "real" if kind == "real" else "simulação", "saldo_usd": round(p.equity, 2), "ganho_do_dia_usd": round(p.equity - p.day_start_equity, 3),
            "posicoes": pos, "mercado": {k: temp.get(k) for k in ("avg_rsi", "avg_ch24", "uptrend", "n")}, "regime": _regime(),
            "btc_acima_da_media_50h": eng.ledger.get_state("btc_ok"), "ultimo_ciclo": day,
            "decisoes_recentes": [{"tipo": d["kind"], "ativo": d["symbol"], "bot": d["strategy"], "motivo": (d["reason"] or "")[:110]} for d in decisions]}


def _generate(eng, kind: str):
    from src.solana.agents import anthropic_llm
    entry = _cache.setdefault(kind, {})
    try:
        snap = _snapshot(eng, kind)
        text = anthropic_llm(f"Dados da conta (JSON):\n{snap}", system=SYSTEM, max_searches=0, model=cfg.DEBATE_MODEL, who="brief")
        entry.update(text=text.strip(), ts=time.time(), model=cfg.DEBATE_MODEL, error=None)
    except Exception as e:                      # over budget, no key, API down: keep the previous text
        entry.update(error=str(e)[:160], tried=time.time())
    finally:
        entry["busy"] = False


def get(eng, kind: str, force: bool = False) -> dict:
    """The cached read plus a flag saying whether a refresh is running; starts one in the background when it is stale."""
    if not cfg.ANTHROPIC_API_KEY:
        return {"text": None, "error": "sem ANTHROPIC_API_KEY", "busy": False}
    with _lock:
        entry = _cache.setdefault(kind, {})
        age = time.time() - max(entry.get("ts", 0), entry.get("tried", 0))
        if not entry.get("busy") and (force and age > 60 or age > BRIEF_TTL_S):
            entry["busy"] = True
            threading.Thread(target=_generate, args=(eng, kind), daemon=True, name=f"brief-{kind}").start()
        return {k: entry.get(k) for k in ("text", "ts", "model", "error", "busy")}
