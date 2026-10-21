"""
PaperBroker: simulates USDC<->token swaps using a REAL Jupiter quote.

Fill = quoted route output (already includes AMM fees + price impact)
       − extra adverse slippage (PAPER_SLIPPAGE_BPS, models latency/MEV)
       − network base fee + priority fee (converted to USD at SOL price).
There is no code path here that builds, signs or sends a transaction.
"""
import json

from src.solana import config as cfg
from src.solana import market_data as md
from src.solana.ledger import Ledger, now_iso


class Broker:
    """Venue-agnostic paper broker interface. Agents never see which venue fills them;
    ExecutorAgent routes by candidate/position venue."""
    venue = "?"

    def buy(self, order_id: str, instrument: str, symbol: str, usd: float, decimals: int, *,
            strategy: str, stop_price: float, tp_price: float, meta: dict | None = None) -> dict:
        raise NotImplementedError

    def sell(self, order_id: str, instrument: str, symbol: str, qty: float, decimals: int, *,
             strategy: str, meta: dict | None = None) -> dict:
        raise NotImplementedError

    def quote_sell_value(self, instrument: str, qty: float, decimals: int) -> tuple[float, dict]:
        """USD received if the position were closed now (mark-to-market at executable prices)."""
        raise NotImplementedError

    def accrue(self, positions: list[dict]) -> float:
        """Periodic carrying costs (e.g. perp funding). Spot venues have none."""
        return 0.0


class PaperBroker(Broker):
    """Solana spot via Jupiter quotes."""
    venue = "jupiter"

    def __init__(self, ledger: Ledger, quote_fn=md.jupiter_quote, sol_usd_fn=md.sol_usd,
                 slippage_bps: int = cfg.PAPER_SLIPPAGE_BPS,
                 fee_lamports: int = cfg.PAPER_BASE_FEE_LAMPORTS + cfg.PAPER_PRIORITY_FEE_LAMPORTS):
        self.ledger = ledger
        self.quote_fn = quote_fn
        self.sol_usd_fn = sol_usd_fn
        self.slippage_bps = slippage_bps
        self.fee_lamports = fee_lamports

    def _fee_usd(self) -> float:
        return self.fee_lamports / 1e9 * self.sol_usd_fn()

    @staticmethod
    def _route(q: dict) -> str:
        return " > ".join(r["swapInfo"].get("label", "?") for r in q.get("routePlan", []))

    def quote_sell_value(self, mint: str, qty: float, decimals: int) -> tuple[float, dict]:
        """USD you'd receive selling qty now (gross of sim slippage/fees). Used for mark-to-market."""
        q = self.quote_fn(mint, cfg.USDC_MINT, int(qty * 10**decimals))
        return int(q["outAmount"]) / 1e6, q

    def buy(self, order_id: str, mint: str, symbol: str, usd: float, decimals: int, *,
            strategy: str, stop_price: float, tp_price: float, meta: dict | None = None) -> dict:
        if (existing := self.ledger.get_fill(order_id)):
            return existing  # idempotent: never re-quote or double-fill
        q = self.quote_fn(cfg.USDC_MINT, mint, int(usd * 1e6))
        quoted_qty = int(q["outAmount"]) / 10**decimals
        qty = quoted_qty * (1 - self.slippage_bps / 1e4)
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="buy", mint=mint, symbol=symbol, strategy=strategy,
            qty=qty, decimals=decimals, usd_gross=usd, fee_usd=self._fee_usd(),
            price=usd / qty, quote_price=usd / quoted_qty,
            price_impact_pct=float(q.get("priceImpactPct") or 0), slippage_bps=self.slippage_bps,
            route=self._route(q), stop_price=stop_price, tp_price=tp_price, meta=json.dumps(meta or {}),
        ))
        return fill

    def sell(self, order_id: str, mint: str, symbol: str, qty: float, decimals: int, *,
             strategy: str, meta: dict | None = None) -> dict:
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        quoted_usd, q = self.quote_sell_value(mint, qty, decimals)
        usd = quoted_usd * (1 - self.slippage_bps / 1e4)
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="sell", mint=mint, symbol=symbol, strategy=strategy,
            qty=qty, decimals=decimals, usd_gross=usd, fee_usd=self._fee_usd(),
            price=usd / qty, quote_price=quoted_usd / qty,
            price_impact_pct=float(q.get("priceImpactPct") or 0), slippage_bps=self.slippage_bps,
            route=self._route(q), stop_price=None, tp_price=None, meta=json.dumps(meta or {}),
        ))
        return fill
