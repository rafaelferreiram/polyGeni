"""
Agents. Rules decide; the LLM only analyses and may VETO.

Scout -> Safety -> Strategy (deterministic signal) -> Debate (optional LLM veto)
      -> Risk (deterministic, final veto, sizes the trade) -> Executor (only one touching the broker)
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from src.solana import config as cfg
from src.solana import market_data as md
from src.solana.strategies import Signal

logger = logging.getLogger("solana.agents")


@dataclass
class Candidate:
    mint: str
    symbol: str
    pair_address: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_h24: float
    pair_created_at_ms: int | None
    price_change: dict = field(default_factory=dict)
    txns_h24: dict = field(default_factory=dict)
    decimals: int | None = None   # filled by SafetyAgent from the mint account
    venue: str = "jupiter"        # which paper broker fills it; agents don't care

    def age_hours(self, now_ms: float | None = None) -> float | None:
        if not self.pair_created_at_ms:
            return None
        return ((now_ms or time.time() * 1000) - self.pair_created_at_ms) / 3.6e6

    def summary(self) -> dict:
        return {"symbol": self.symbol, "price_usd": self.price_usd, "liquidity_usd": self.liquidity_usd,
                "volume_h24": self.volume_h24, "age_h": round(self.age_hours() or 0, 1),
                "price_change": self.price_change, "txns_h24": self.txns_h24, "dex": self.dex}


@dataclass
class Vote:
    agent: str
    approve: bool
    reason: str
    detail: dict = field(default_factory=dict)


# ─── Scout ───────────────────────────────────────────────────────────────────

class ScoutAgent:
    """Trending + watchlist tokens, best pair per token, minimum liquidity/volume."""

    def __init__(self, boosted_fn=md.dexscreener_boosted_solana, pairs_fn=md.dexscreener_pairs,
                 watchlist=cfg.SCOUT_WATCHLIST, min_liq=cfg.SCOUT_MIN_LIQUIDITY_USD,
                 min_vol=cfg.SCOUT_MIN_VOLUME_24H_USD, max_n=cfg.SCOUT_MAX_CANDIDATES):
        self.boosted_fn, self.pairs_fn = boosted_fn, pairs_fn
        self.watchlist, self.min_liq, self.min_vol, self.max_n = watchlist, min_liq, min_vol, max_n

    def candidates(self) -> list[Candidate]:
        tokens = list(dict.fromkeys(self.watchlist + self.boosted_fn()))
        best: dict[str, dict] = {}
        for p in self.pairs_fn(tokens):
            if p.get("chainId") != "solana" or p.get("quoteToken", {}).get("address") not in (cfg.USDC_MINT, cfg.SOL_MINT):
                continue
            mint = p["baseToken"]["address"]
            if mint not in tokens:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if liq > ((best.get(mint) or {}).get("liquidity") or {}).get("usd", 0):
                best[mint] = p
        out = []
        for mint, p in best.items():
            c = Candidate(
                mint=mint, symbol=p["baseToken"].get("symbol", "?"), pair_address=p["pairAddress"],
                dex=p.get("dexId", "?"), price_usd=float(p.get("priceUsd") or 0),
                liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
                volume_h24=float((p.get("volume") or {}).get("h24") or 0),
                pair_created_at_ms=p.get("pairCreatedAt"), price_change=p.get("priceChange") or {},
                txns_h24=(p.get("txns") or {}).get("h24") or {},
            )
            if c.liquidity_usd >= self.min_liq and c.volume_h24 >= self.min_vol:
                out.append(c)
        out.sort(key=lambda c: c.volume_h24, reverse=True)
        return out[: self.max_n]


# ─── Safety ──────────────────────────────────────────────────────────────────

SAFE_TOKEN2022_EXTENSIONS = {"metadataPointer", "tokenMetadata"}


class SafetyAgent:
    """Anti-rug. Rejects by default: anything not positively verified is a rejection.
    No launch sniping: pair must be older than SAFETY_MIN_PAIR_AGE_H."""

    def __init__(self, mint_info_fn=md.mint_info, holders_fn=md.top_holders_pct,
                 min_age_h=cfg.SAFETY_MIN_PAIR_AGE_H, min_liq=cfg.SAFETY_MIN_LIQUIDITY_USD,
                 max_top10=cfg.SAFETY_MAX_TOP10_PCT, cache_ttl_s: float = 3600):
        self.mint_info_fn, self.holders_fn = mint_info_fn, holders_fn
        self.min_age_h, self.min_liq, self.max_top10 = min_age_h, min_liq, max_top10
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, Vote]] = {}

    def check(self, c: Candidate, now_ms: float | None = None) -> Vote:
        hit = self._cache.get(c.mint)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            if hit[1].approve and hit[1].detail.get("decimals") is not None:
                c.decimals = hit[1].detail["decimals"]
            return hit[1]
        vote = self._check(c, now_ms)
        self._cache[c.mint] = (time.time(), vote)
        return vote

    def _check(self, c: Candidate, now_ms) -> Vote:
        d: dict = {}
        age = c.age_hours(now_ms)
        d["age_h"] = age
        if age is None:
            return Vote("safety", False, "pair age unknown", d)
        if age < self.min_age_h:
            return Vote("safety", False, f"pair too new ({age:.1f}h < {self.min_age_h}h) — no launch sniping", d)
        d["liquidity_usd"] = c.liquidity_usd
        if not c.liquidity_usd or c.liquidity_usd < self.min_liq:
            return Vote("safety", False, f"liquidity ${c.liquidity_usd:,.0f} < ${self.min_liq:,.0f}", d)
        try:
            info = self.mint_info_fn(c.mint)
        except Exception as e:
            return Vote("safety", False, f"mint info unverifiable: {e}", d)
        d.update(mint_authority=info.get("mintAuthority"), freeze_authority=info.get("freezeAuthority"),
                 program=info.get("program"), extensions=info.get("extensions"), decimals=info.get("decimals"))
        if "mintAuthority" not in info or "freezeAuthority" not in info or info.get("decimals") is None:
            return Vote("safety", False, "mint data incomplete", d)
        if info["mintAuthority"]:
            return Vote("safety", False, "mint authority active (supply can be inflated)", d)
        if info["freezeAuthority"]:
            return Vote("safety", False, "freeze authority active (holders can be frozen)", d)
        risky_ext = set(info.get("extensions") or []) - SAFE_TOKEN2022_EXTENSIONS
        if risky_ext:
            return Vote("safety", False, f"risky Token-2022 extensions: {sorted(risky_ext)}", d)
        try:
            top = self.holders_fn(c.mint)
        except Exception as e:
            return Vote("safety", False, f"holder concentration unverifiable: {e}", d)
        if top is None or not 0 <= top <= 1:
            return Vote("safety", False, "holder data missing", d)
        d["top_holders_pct"] = round(top, 4)
        if top > self.max_top10:
            return Vote("safety", False, f"top holders own {top:.0%} > {self.max_top10:.0%}", d)
        c.decimals = info["decimals"]
        return Vote("safety", True, "all checks passed", d)


# ─── Debate (LLM, veto-only) ─────────────────────────────────────────────────

DEBATE_SYSTEM = (
    "You review a proposed SPOT long entry on a Solana token for a paper-trading bot. "
    "Deterministic rules already produced the signal and will size the trade; you cannot open, "
    "resize or redirect trades. Your only power is to VETO. Use web search to check very recent news "
    "about the token (exploits, unlocks, delistings, team issues, hacks). Argue the bull case and the "
    "bear case briefly, then decide. Veto only for a concrete, material risk. Reply with ONLY a JSON "
    'object: {"bull": str, "bear": str, "veto": bool, "reason": str}.'
)


def anthropic_llm(prompt: str, system: str = DEBATE_SYSTEM, max_searches: int = 3, model: str | None = None,
                  who: str = "?") -> str:
    """Claude via the official SDK with server-side web search (+ optional remote MCP servers).
    Refuses to call once the monthly AI budget is spent; records the real cost of every call."""
    import anthropic
    from src.solana import costs
    costs.check_budget()

    model = model or cfg.DEBATE_MODEL
    # dynamic-filtering web search only on Opus/Sonnet; server-side refusal fallback not on Haiku
    dynamic_search = model.startswith(("claude-opus", "claude-sonnet"))
    tools = [{"type": "web_search_20260209" if dynamic_search else "web_search_20250305", "name": "web_search",
              "max_uses": max_searches}] if max_searches > 0 else []
    kwargs, betas = {}, []
    if model in ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"):
        kwargs["fallbacks"] = "default"
        betas.append("server-side-fallback-2026-07-01")
    mcp = json.loads(cfg.DEBATE_MCP_SERVERS or "[]")  # [{"url":..., "name":..., "authorization_token":...}]
    if mcp:
        kwargs["mcp_servers"] = [{"type": "url", **s} for s in mcp]
        tools += [{"type": "mcp_toolset", "mcp_server_name": s["name"]} for s in mcp]
        betas.append("mcp-client-2025-11-20")
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY, timeout=120)
    resp = client.beta.messages.create(
        model=model, max_tokens=4000, system=system, betas=betas, output_config={"effort": "low"}, tools=tools,
        messages=[{"role": "user", "content": prompt}], **kwargs,
    )
    u = resp.usage
    stu = getattr(u, "server_tool_use", None)
    costs.book().record(model, who, {
        "input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "searches": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0})
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused")
    return "".join(b.text for b in resp.content if b.type == "text")


class DebateAgent:
    """Bull vs bear LLM review. Returns a Vote whose only effect can be a veto.
    Disabled (approve, abstained) without an API key. LLM errors or unparsable output => veto."""

    def __init__(self, llm_fn=None, enabled: bool | None = None, model_for=None):
        self.enabled = bool(cfg.ANTHROPIC_API_KEY) if enabled is None else enabled
        self.llm_fn = llm_fn or (lambda prompt, model=None: anthropic_llm(prompt, model=model, who="debate",
                                                                           max_searches=cfg.LLM_WEB_SEARCHES))
        self.model_for = model_for          # strategy -> model the user picked for its reviews (None = default)

    def review(self, c: Candidate, sig: Signal, context: dict) -> Vote:
        if not self.enabled:
            return Vote("debate", True, "disabled (no ANTHROPIC_API_KEY)", {"abstained": True})
        prompt = json.dumps({"token": c.summary(), "mint": c.mint, "strategy": sig.strategy,
                             "signal": sig.reason, "stop_pct": sig.stop_pct, "tp_pct": sig.tp_pct,
                             "context": context}, default=str)
        try:
            model = self.model_for(sig.strategy) if self.model_for else None
            text = self.llm_fn(prompt, model) if model else self.llm_fn(prompt)
            m = re.search(r"\{.*\}", text, re.S)
            out = json.loads(m.group(0)) if m else None
            if not isinstance(out, dict) or not isinstance(out.get("veto"), bool):
                return Vote("debate", False, "unparsable LLM verdict → veto", {"raw": text[:500]})
        except Exception as e:
            return Vote("debate", False, f"LLM error → veto: {e}")
        detail = {"bull": str(out.get("bull", ""))[:600], "bear": str(out.get("bear", ""))[:600]}
        return Vote("debate", not out["veto"], str(out.get("reason", ""))[:400], detail)


# ─── Risk (deterministic, final veto) ────────────────────────────────────────

@dataclass
class Portfolio:
    equity: float
    cash: float
    exposure: float                    # marked value of open positions
    open_mints: set
    strategy_exposure: dict            # strategy -> marked value
    day_start_equity: float
    kill_switch: bool
    entries_today: int = 0


class RiskAgent:
    """No LLM. Sizes the trade and has the final word."""

    def __init__(self, max_pos_pct=cfg.RISK_MAX_POSITION_PCT, max_exposure_pct=cfg.RISK_MAX_EXPOSURE_PCT,
                 max_positions=cfg.RISK_MAX_POSITIONS, max_daily_loss_pct=cfg.RISK_MAX_DAILY_LOSS_PCT,
                 min_trade_usd=cfg.RISK_MIN_TRADE_USD, max_entries_per_day=cfg.RISK_MAX_ENTRIES_PER_DAY,
                 max_position_usd: float | None = None, max_capital_usd: float | None = None, leverage: float = 1.0):
        self.max_pos_pct, self.max_exposure_pct = max_pos_pct, max_exposure_pct
        self.max_positions, self.max_daily_loss_pct = max_positions, max_daily_loss_pct
        self.min_trade_usd = min_trade_usd
        self.max_entries_per_day = max_entries_per_day
        # hard USD caps (live trading): never size off more than max_capital_usd, never a bigger position
        self.max_position_usd, self.max_capital_usd = max_position_usd, max_capital_usd
        self.leverage = leverage

    def daily_loss_breached(self, p: Portfolio) -> bool:
        return p.day_start_equity > 0 and (p.day_start_equity - p.equity) / p.day_start_equity >= self.max_daily_loss_pct

    def evaluate(self, sig: Signal, mint: str, capital_pct: float, p: Portfolio,
                 max_pos_pct: float | None = None, entry_cap_mult: int = 1, max_usd: float | None = None) -> Vote:
        d = {"equity": round(p.equity, 2), "cash": round(p.cash, 2), "exposure": round(p.exposure, 2)}
        if p.kill_switch:
            return Vote("risk", False, "kill switch active", d)
        if self.daily_loss_breached(p):
            return Vote("risk", False, "daily loss limit reached", d)
        if not (0 < sig.stop_pct < 1) or not (sig.tp_pct > 0):
            return Vote("risk", False, "stop-loss and take-profit are mandatory", d)
        if mint in p.open_mints:
            return Vote("risk", False, "already holding this token", d)
        cap = self.max_entries_per_day * entry_cap_mult        # ☕ coffee doubles it for active day trading
        if p.entries_today >= cap:
            return Vote("risk", False, f"daily entry cap ({cap}) reached", d)
        max_pos = self.max_positions
        if not max_pos and self.max_position_usd:      # 0 = auto: one slot per max_position_usd of capital
            cap = min(p.equity, self.max_capital_usd) if self.max_capital_usd else p.equity
            max_pos = max(1, min(6, int(cap * self.leverage // self.max_position_usd)))
        d["max_positions"] = max_pos
        if len(p.open_mints) >= max_pos:
            return Vote("risk", False, f"max positions ({max_pos}) reached", d)
        eq = min(p.equity, self.max_capital_usd) if self.max_capital_usd else p.equity
        lev = self.leverage
        strategy_room = eq * lev * capital_pct - p.strategy_exposure.get(sig.strategy, 0)
        exposure_room = eq * lev * self.max_exposure_pct - p.exposure
        # margin: at 1x notional is capped by cash; with isolated leverage by equity x leverage, 3% kept for fees/slippage
        margin_room = p.cash * 0.98 if lev == 1 else eq * lev * 0.97 - p.exposure
        size = min(eq * (max_pos_pct or self.max_pos_pct), strategy_room, exposure_room, margin_room,
                   self.max_position_usd or float("inf"), max_usd or float("inf"))   # max_usd: the bot's reward size
        d.update(size_usd=round(size, 2), strategy_room=round(strategy_room, 2), exposure_room=round(exposure_room, 2))
        if size < self.min_trade_usd:
            return Vote("risk", False, f"size ${size:.2f} < min ${self.min_trade_usd:.2f} "
                                       f"(position/exposure/strategy capital/cash limits)", d)
        d["fingerprint"] = order_fingerprint(sig.strategy, mint, round(size, 2), sig.stop_pct, sig.tp_pct)
        return Vote("risk", True, f"approved ${size:.2f}", d)


def order_fingerprint(strategy: str, mint: str, size_usd: float, stop_pct: float, tp_pct: float) -> str:
    """SHA-256 of exactly what Risk approved; Executor refuses anything that doesn't match."""
    raw = json.dumps([strategy, mint, round(size_usd, 2), round(stop_pct, 6), round(tp_pct, 6)])
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Executor ────────────────────────────────────────────────────────────────

class ExecutorAgent:
    """The only component holding broker references; routes by venue so other agents stay venue-agnostic."""

    def __init__(self, *brokers):
        self._brokers = {b.venue: b for b in brokers}

    @property
    def venues(self) -> list[str]:
        return list(self._brokers)

    def _b(self, venue: str):
        if venue not in self._brokers:
            raise RuntimeError(f"no paper broker for venue {venue}")
        return self._brokers[venue]

    def open(self, order_id: str, c: Candidate, sig: Signal, size_usd: float, fingerprint: str) -> dict:
        if order_fingerprint(sig.strategy, c.mint, round(size_usd, 2), sig.stop_pct, sig.tp_pct) != fingerprint:
            raise RuntimeError("order differs from what RiskAgent approved (fingerprint mismatch) — refused")
        return self._b(c.venue).buy(order_id, c.mint, c.symbol, size_usd, c.decimals, strategy=sig.strategy,
                                    stop_price=sig.stop_price, tp_price=sig.tp_price,
                                    meta={"pool_address": c.pair_address, "signal": sig.reason})

    def close(self, order_id: str, pos: dict, reason: str) -> dict:
        return self._b(pos["venue"]).sell(order_id, pos["mint"], pos["symbol"], pos["qty"], pos["decimals"],
                                          strategy=pos["strategy"], meta={"reason": reason})

    def mark(self, pos: dict) -> float:
        return self._b(pos["venue"]).quote_sell_value(pos["mint"], pos["qty"], pos["decimals"])[0]

    def accrue(self, positions: list[dict]) -> float:
        return sum(b.accrue(positions) for b in self._brokers.values())

    def sync(self) -> list[str]:
        """Reconcile brokers against their venue; returns problems that must stop new entries."""
        return [p for b in self._brokers.values() for p in b.sync()]
