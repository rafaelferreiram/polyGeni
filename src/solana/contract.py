"""
TradeIntent — the typed, versioned contract of a trade intention (master plan §20). Built from a strategy's
Signal and VALIDATED BEFORE the Risk Engine sees it; the validated snapshot is attached to the entry decision.
It never replaces Risk (deterministic sizing and the final veto stay there): it guarantees semantics — sides,
price ordering, data freshness, expiry — so no component downstream ever receives a malformed intention, and
the LLM path cannot smuggle arbitrary instructions to the exchange.

Price convention: shorts are held on the mirror price 2*entry − price (see hyperliquid.SHORT), so the invariant
stop < entry < target holds for BOTH sides here; `side` still reports the real economic direction.
Cost convention: the historical payoff behind expected_net_edge already includes fees, slippage and funding
(trades are booked net), so only the AI cost is subtracted on top — estimated fees/slippage/funding are
reported for transparency, not double-counted.
"""
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from src.solana import config as cfg

VERSION = 1


@dataclass
class TradeIntent:
    signal_id: str
    bot_id: str
    strategy_id: str
    strategy_version: str
    exchange: str                     # venue: hyperliquid | jupiter
    instrument: str                   # mint / hl:<coin>[~short]
    market_type: str                  # perp | spot
    side: str                         # spot: buy | perp: open_long / open_short
    timeframe: str                    # 1h | 15m | ...
    market_regime: str | None
    signal_timestamp: str
    data_asof_timestamp: float        # epoch s of the last closed candle the signal saw
    expiration_timestamp: float       # epoch s: the signal dies with its candle
    entry_policy: str                 # market_ioc (the only execution the club does today)
    expected_entry_price: float
    stop_price: float
    take_profit_policy: dict
    max_holding_time_h: float
    estimated_fees_bps: float
    estimated_slippage_bps: float
    estimated_funding_1h: float | None
    estimated_ai_cost_usd: float
    expected_net_edge_usd: float | None   # edge estimate (net-of-costs history) minus AI cost; None = no evidence yet
    estimated_loss_at_stop_usd: float | None   # filled once Risk sizes the trade
    confidence: float | None
    evidence_references: list = field(default_factory=list)
    risk_budget_request_usd: float | None = None
    decision_id: str | None = None
    version: int = VERSION

    def validate(self):
        """Raises ValueError on any semantic violation. Freshness/expiry use the wall clock."""
        if self.market_type not in ("perp", "spot"):
            raise ValueError(f"market_type inválido: {self.market_type}")
        if self.market_type == "spot" and self.side != "buy":
            raise ValueError(f"spot só compra/vende à vista: side {self.side}")
        if self.market_type == "perp" and self.side not in ("open_long", "open_short"):
            raise ValueError(f"perp abre long/short, nunca 'buy' ambíguo: side {self.side}")
        if self.side == "open_short" and not self.instrument.endswith("~short"):
            raise ValueError("short sem instrumento ~short (semântica de espelho)")
        if self.side != "open_short" and self.instrument.endswith("~short"):
            raise ValueError("instrumento ~short com side de compra")
        if not (0 < self.stop_price < self.expected_entry_price):
            raise ValueError(f"stop {self.stop_price} deve ficar abaixo da entrada {self.expected_entry_price} (espelho p/ short)")
        tp = (self.take_profit_policy or {}).get("price")
        if not tp or tp <= self.expected_entry_price:
            raise ValueError(f"alvo {tp} deve ficar acima da entrada {self.expected_entry_price} (espelho p/ short)")
        now = time.time()
        if now - self.data_asof_timestamp > cfg.STALE_CANDLE_H * 3600:
            raise ValueError(f"dados velhos: candle de {(now - self.data_asof_timestamp) / 3600:.1f}h atrás")
        if now >= self.expiration_timestamp:
            raise ValueError("sinal expirado: o candle que o gerou já passou")
        if not (0 < self.max_holding_time_h <= 24 * 14):
            raise ValueError(f"max_holding_time_h inválido: {self.max_holding_time_h}")
        if self.confidence is not None and not (0 <= self.confidence <= 1):
            raise ValueError(f"confiança fora de [0,1]: {self.confidence}")

    def as_dict(self) -> dict:
        return asdict(self)


def _ai_cost_estimate(strategy: str) -> float:
    """Average recent $ per paid call of this bot (0 for rule bots): the marginal AI cost of one more decision."""
    if not strategy.startswith("llm:"):
        return 0.0
    try:
        from src.solana import costs
        b = costs.book()
        total = b.total(days=7, who=strategy)
        with b._lock:
            n = b.conn.execute("SELECT COUNT(*) FROM costs WHERE who=? AND ts >= datetime('now', '-7 days')",
                               (strategy,)).fetchone()[0]
        return total / n if n else 0.0
    except Exception:
        return 0.0


def build(c, sig, strategy_name: str, candles: list[dict], max_hold_h: float, regime: dict | None,
          edge_est: dict | None, cycle: int) -> TradeIntent:
    """Assemble the intent from what the engine already knows at signal time (before Risk sizes it)."""
    step = (candles[-1]["t"] - candles[-2]["t"]) if len(candles) >= 2 else 3600
    timeframe = {3600: "1h", 900: "15m", 60: "1m", 14400: "4h", 86400: "1d"}.get(step, f"{step}s")
    is_perp = c.venue == "hyperliquid"
    short = c.mint.endswith("~short")
    ai_cost = _ai_cost_estimate(strategy_name)
    ev = (edge_est or {}).get("ev_usd")
    from src.solana import hyperliquid as hl
    return TradeIntent(
        signal_id=f"c{cycle}-{strategy_name}-{c.mint[:12]}-{candles[-1]['t']}",
        bot_id=strategy_name.removeprefix("llm:"), strategy_id=strategy_name,
        strategy_version=str((sig.indicators or {}).get("gen", 1)),
        exchange=c.venue, instrument=c.mint, market_type="perp" if is_perp else "spot",
        side=("open_short" if short else "open_long") if is_perp else "buy",
        timeframe=timeframe, market_regime=(regime or {}).get("state"),
        signal_timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        data_asof_timestamp=float(candles[-1]["t"]) + step,          # the candle CLOSED here
        expiration_timestamp=float(candles[-1]["t"]) + 2 * step,     # a signal never outlives the next candle
        entry_policy="market_ioc", expected_entry_price=sig.ref_price, stop_price=sig.stop_price,
        take_profit_policy={"type": "fixed", "price": sig.tp_price},
        max_holding_time_h=max_hold_h,
        estimated_fees_bps=2 * hl.BASE_TAKER_BPS if is_perp else cfg.PAPER_QUOTE_SLIPPAGE_BPS / 2,
        estimated_slippage_bps=2.0 if is_perp else cfg.PAPER_SLIPPAGE_BPS,
        estimated_funding_1h=(c.price_change or {}).get("funding_1h"),
        estimated_ai_cost_usd=round(ai_cost, 5),
        expected_net_edge_usd=round(ev - ai_cost, 4) if ev is not None else None,
        estimated_loss_at_stop_usd=None, confidence=(edge_est or {}).get("p"),
        evidence_references=list((edge_est or {}).get("tags", []) or []),
    )
