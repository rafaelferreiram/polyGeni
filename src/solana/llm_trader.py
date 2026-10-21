"""
LLM decision mode: an LLM decides BUY / HOLD for entries and SELL / HOLD for open positions.

The deterministic RiskAgent still owns position size, the hard USD caps, daily loss / kill switch, and the
stop-loss is mandatory. The LLM's stop/take-profit are clamped to safe bounds and its output is validated:
anything malformed is treated as HOLD. One call per market per closed candle, to bound cost.
"""
import json
import logging
import re

from src.solana import config as cfg
from src.solana.agents import anthropic_llm
from src.solana.strategies import Signal

logger = logging.getLogger("solana.llm_trader")

TRADER_SYSTEM = (
    "You are the trading decision agent of a small, risk-averse spot/1x-perp trading bot. For the market given, "
    "decide whether to open a LONG now (BUY) or do nothing (HOLD). No shorts, no leverage. Use the candles, "
    "market data and, if useful, web search for very recent news. Prefer HOLD unless the setup is clearly "
    "favourable after fees. A separate risk engine sets the position size and can refuse your trade. "
    'Reply with ONLY JSON: {"action": "BUY"|"HOLD", "stop_pct": number, "tp_pct": number, '
    '"confidence": number 0-1, "reason": string, "next_check_min": number}. stop_pct/tp_pct are fractions '
    "(0.03 = 3%). next_check_min: if HOLD, how many minutes until this market is worth looking at again "
    "(15-240; quiet markets deserve longer)."
)
EXIT_SYSTEM = (
    "You manage an open LONG position for a small, risk-averse trading bot. A stop-loss and take-profit already "
    "exist on the exchange. Decide whether to close it now (SELL) or keep it (HOLD). Reply with ONLY JSON: "
    '{"action": "SELL"|"HOLD", "reason": string}.'
)
STOP_BOUNDS, TP_BOUNDS = (0.01, 0.10), (0.01, 0.20)


def _clamp(x, lo, hi):
    return max(lo, min(hi, float(x)))


def _json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def _candles_brief(candles: list[dict], n: int = 48) -> list[list]:
    return [[c["t"], round(c["open"], 8), round(c["high"], 8), round(c["low"], 8), round(c["close"], 8),
             round(c["volume"], 2)] for c in candles[-n:]]


class LLMDecider:
    """Drop-in strategy: the engine calls decide(candles, context) instead of signal(candles)."""
    capital_pct = 1.0

    def __init__(self, llm_fn=None, entry_system=TRADER_SYSTEM, exit_system=EXIT_SYSTEM, model: str | None = None,
                 router=None):
        self.model = model or cfg.DEBATE_MODEL
        self.router = router            # market -> model chosen by the coordinator (None = always self.model)
        self.name = "llm"
        self.llm_fn = llm_fn or (lambda prompt, system: anthropic_llm(prompt, system=system, max_searches=2,
                                                                       model=self.model))
        self.entry_system, self.exit_system = entry_system, exit_system
        self._seen: dict[tuple, object] = {}     # (kind, market) -> last candle t already decided on
        self.last: dict[str, dict] = {}          # market -> last decision, for the UI

    @property
    def enabled(self) -> bool:
        return bool(cfg.ANTHROPIC_API_KEY)

    def _once_per_candle(self, kind: str, market: str, candles: list[dict]) -> bool:
        t = candles[-1]["t"] if candles else None
        if self._seen.get((kind, market)) == t:
            return False
        self._seen[(kind, market)] = t
        return True

    def signal(self, candles):            # rules-mode interface: the LLM needs context, so never via this path
        return None

    def decide(self, candles: list[dict], context: dict) -> Signal | None:
        market = context.get("symbol", "?")
        if not candles or not self._once_per_candle("entry", market, candles):
            return None
        if self.router:
            self.model = self.router(market) or self.model
        try:
            out = _json(self.llm_fn(json.dumps({"market": context, "candles_1h_tohlcv": _candles_brief(candles)},
                                               default=str), self.entry_system))
        except Exception as e:
            self.last[market] = {"action": "HOLD", "reason": f"LLM error: {e}"[:200]}
            return None
        if not isinstance(out, dict) or out.get("action") not in ("BUY", "HOLD"):
            self.last[market] = {"action": "HOLD", "reason": "invalid LLM answer → HOLD"}
            return None
        self.last[market] = {"action": out["action"], "reason": str(out.get("reason", ""))[:300], "model": self.model,
                             "confidence": out.get("confidence"), "next_check_min": out.get("next_check_min"),
                             "t": candles[-1]["t"]}
        if out["action"] != "BUY":
            return None
        try:
            stop, tp = _clamp(out.get("stop_pct", 0.05), *STOP_BOUNDS), _clamp(out.get("tp_pct", 0.08), *TP_BOUNDS)
        except (TypeError, ValueError):
            return None
        self.last[market].update(stop=stop, tp=tp)
        name = f"llm:{self.model}" if self.router else self.name
        return Signal(name, candles[-1]["close"], stop, tp, f"{self.model}: {self.last[market]['reason']}"[:300],
                      {"model": self.model, "confidence": out.get("confidence"), "llm_stop_pct": out.get("stop_pct"),
                       "llm_tp_pct": out.get("tp_pct")})

    def review_exit(self, position: dict, candles: list[dict], mark_price: float) -> str | None:
        """Returns a reason to close now, or None to hold (stops on the exchange keep protecting it)."""
        market = position.get("symbol", "?")
        if not candles or not self._once_per_candle("exit", market, candles):
            return None
        ctx = {"symbol": market, "entry_price": position["entry_price"], "mark_price": mark_price,
               "pnl_pct": mark_price / position["entry_price"] - 1, "stop_price": position["stop_price"],
               "tp_price": position["tp_price"], "opened_at": position["opened_at"]}
        try:
            out = _json(self.llm_fn(json.dumps({"position": ctx, "candles_1h_tohlcv": _candles_brief(candles, 24)},
                                               default=str), self.exit_system))
        except Exception as e:
            logger.warning(f"LLM exit review {market}: {e}")
            return None
        if isinstance(out, dict) and out.get("action") == "SELL":
            return f"LLM sell: {str(out.get('reason', ''))[:200]}"
        return None


class TeamMember:
    """One bot of the team proposing trades for the SHARED wallet. LLM bots reuse the decision their lab bot
    already made for the same candle (one LLM call feeds both wallets); 'rules' is the deterministic baseline."""
    capital_pct = 1.0

    def __init__(self, model: str, lab_lookup=None, llm_fn=None):
        from src.solana.strategies import MeanReversion
        self.model = model
        self.name = "rules" if model == "rules" else f"llm:{model}"
        self._rules = MeanReversion() if model == "rules" else None
        self._llm = None if model == "rules" else LLMDecider(model=model, llm_fn=llm_fn)
        self.lab_lookup = lab_lookup          # model -> lab bot's LLMDecider (or None)
        self.last: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return self._rules is not None or self._llm.enabled

    def decide(self, candles: list[dict], context: dict) -> Signal | None:
        market = context.get("symbol", "?")
        if not candles:
            return None
        if self._rules:
            sig = self._rules.signal(candles)
            self.last[market] = {"action": "BUY" if sig else "HOLD", "reason": sig.reason if sig else "sem sinal",
                                 "model": "rules", "t": candles[-1]["t"]}
            if sig:
                sig.strategy = self.name
            return sig
        lab = self.lab_lookup(self.model) if self.lab_lookup else None
        d = lab.last.get(market) if lab else None
        if d and d.get("t") == candles[-1]["t"] and d.get("action") in ("BUY", "HOLD"):
            self.last[market] = {**d, "reused_from_lab": True}
            if d["action"] != "BUY" or "stop" not in d:
                return None
            return Signal(self.name, candles[-1]["close"], d["stop"], d["tp"], f"{self.model}: {d.get('reason', '')}"[:300],
                          {"model": self.model, "confidence": d.get("confidence"), "from_lab": True})
        sig = self._llm.decide(candles, context)
        self.last[market] = self._llm.last.get(market, {})
        if sig:
            sig.strategy = self.name
        return sig

    def review_exit(self, position: dict, candles: list[dict], mark_price: float) -> str | None:
        return None if self._rules else self._llm.review_exit(position, candles, mark_price)
