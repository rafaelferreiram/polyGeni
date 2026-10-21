"""
ResearchAgent — the "analyst desk". For each token it builds a brief:
  quant: deterministic metrics + red flags from Jupiter's token API (organic score, flows, holders, liquidity)
  llm:   optional Claude analysis with live web search (+ MCP servers): thesis, risks, catalysts, sentiment
Briefs inform the DebateAgent and the dashboard. Research can only ever block (veto) an entry,
never open one or change its size.
"""
import json
import logging
import re
import time

from src.solana import config as cfg
from src.solana import market_data as md
from src.solana.agents import anthropic_llm

logger = logging.getLogger("solana.research")

RESEARCH_SYSTEM = (
    "You are a crypto research analyst supporting a risk-averse Solana spot trading desk. Use web search "
    "to find the most recent, concrete information about the token: news, exploits/hacks, unlocks and "
    "emissions, listings/delistings, team or treasury moves, regulatory issues, social sentiment. Be "
    "factual and date your sources. You do not make trade decisions. Reply with ONLY a JSON object: "
    '{"summary": str, "bull": str, "bear": str, "risks": [str], "catalysts": [str], '
    '"sentiment": "bullish"|"neutral"|"bearish", "risk_level": "low"|"medium"|"high"|"critical", '
    '"sources": [str]}'
)


def quant_brief(info: dict | None) -> dict:
    """Deterministic metrics and red flags. Missing data is itself a flag."""
    if not info:
        return {"risk_level": "high", "flags": ["no Jupiter token data"], "metrics": {}}
    s24 = info.get("stats24h") or {}
    audit = info.get("audit") or {}
    buy, sell = s24.get("buyVolume") or 0, s24.get("sellVolume") or 0
    m = {
        "organic_score": info.get("organicScore"),
        "verified": info.get("isVerified"),
        "holders": info.get("holderCount"),
        "liquidity_usd": info.get("liquidity"),
        "mcap": info.get("mcap"),
        "price_change_24h": s24.get("priceChange"),
        "holder_change_24h": s24.get("holderChange"),
        "liquidity_change_24h": s24.get("liquidityChange"),
        "buy_sell_ratio_24h": round(buy / sell, 3) if sell else None,
        "organic_buyers_24h": s24.get("numOrganicBuyers"),
        "top_holders_pct": audit.get("topHoldersPercentage"),
        "dev_mints": audit.get("devMints"),
        "tags": info.get("tags"),
    }
    flags = []
    if m["organic_score"] is None or m["organic_score"] < 50:
        flags.append(f"low organic activity (score {m['organic_score']}) — possible wash trading")
    if not m["verified"]:
        flags.append("token not verified on Jupiter")
    if m["buy_sell_ratio_24h"] is not None and m["buy_sell_ratio_24h"] < 0.7:
        flags.append(f"heavy selling (buy/sell {m['buy_sell_ratio_24h']})")
    if (m["liquidity_change_24h"] or 0) < -20:
        flags.append(f"liquidity draining ({m['liquidity_change_24h']:.0f}% 24h)")
    if (m["holder_change_24h"] or 0) < -2:
        flags.append(f"holders leaving ({m['holder_change_24h']:.1f}% 24h)")
    if (m["top_holders_pct"] or 0) > 40:
        flags.append(f"concentrated supply (top holders {m['top_holders_pct']:.0f}%)")
    if (m["dev_mints"] or 0) > 5:
        flags.append(f"serial token deployer (dev minted {m['dev_mints']} tokens)")
    level = "low" if not flags else "medium" if len(flags) <= 2 else "high"
    return {"risk_level": level, "flags": flags, "metrics": m}


class ResearchAgent:
    def __init__(self, ledger, token_info_fn=md.jupiter_token_info, llm_fn=None, llm_enabled: bool | None = None,
                 ttl_h: float = 6):
        self.ledger = ledger
        self.token_info_fn = token_info_fn
        self.llm_enabled = bool(cfg.ANTHROPIC_API_KEY) if llm_enabled is None else llm_enabled
        self.llm_fn = llm_fn or (lambda p: anthropic_llm(p, system=RESEARCH_SYSTEM, max_searches=5))
        self.ttl_h = ttl_h

    def brief(self, mint: str, symbol: str = "", context: dict | None = None, force: bool = False) -> dict:
        cached = self.ledger.latest_research(mint)
        if cached and not force and time.time() - cached["epoch"] < self.ttl_h * 3600:
            return cached["brief"]
        try:
            info = self.token_info_fn(mint)
        except Exception as e:
            info = None
            logger.warning(f"token info {mint}: {e}")
        brief = {"mint": mint, "symbol": symbol or (info or {}).get("symbol", ""), "quant": quant_brief(info),
                 "llm": None, "model": None}
        if self.llm_enabled:
            try:
                text = self.llm_fn(json.dumps({"mint": mint, "symbol": brief["symbol"], "name": (info or {}).get("name"),
                                               "quant": brief["quant"], "context": context or {}}, default=str))
                m = re.search(r"\{.*\}", text, re.S)
                brief["llm"] = json.loads(m.group(0)) if m else {"error": "unparsable", "raw": text[:400]}
                brief["model"] = cfg.DEBATE_MODEL
            except Exception as e:
                brief["llm"] = {"error": str(e)[:300]}
        brief["risk_level"] = self._combined_level(brief)
        self.ledger.add_research(mint, brief["symbol"], brief["risk_level"], brief)
        return brief

    @staticmethod
    def _combined_level(brief: dict) -> str:
        order = ["low", "medium", "high", "critical"]
        levels = [brief["quant"]["risk_level"]]
        if isinstance(brief.get("llm"), dict) and brief["llm"].get("risk_level") in order:
            levels.append(brief["llm"]["risk_level"])
        return max(levels, key=order.index)

    @staticmethod
    def vote(brief: dict | None):
        """Research can veto (high/critical risk) but never approve a trade on its own."""
        from src.solana.agents import Vote
        if brief is None:
            return Vote("research", False, "research unavailable")
        level = brief.get("risk_level")
        flags = brief["quant"]["flags"]
        if level in ("high", "critical"):
            return Vote("research", False, f"risk {level}: {'; '.join(flags) or (brief.get('llm') or {}).get('summary', '')}"[:300],
                        {"risk_level": level})
        return Vote("research", True, f"risk {level}" + (f": {'; '.join(flags)}" if flags else ""), {"risk_level": level})
