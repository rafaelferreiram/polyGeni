"""
Hyperliquid PAPER venue: public Info API only (POST /info). No Exchange API, no signing, no keys.

Markets: native perps BTC/ETH/SOL and HIP-3 trade.xyz perps `xyz:GOLD`, `xyz:SILVER` (`dex:ticker`;
`metaAndAssetCtxs`/`allMids` need {"dex": "xyz"}; l2Book/candles/funding take the prefixed coin).
Paper model: 1x long only, isolated margin == notional (fully collateralised in USDC), taker fills by
walking the real L2 book, taker fee per market, hourly funding accrued on open positions.
Verified against docs + live API 2026-10-21.
"""
import json
import time
from datetime import datetime

from src.solana import config as cfg
from src.solana.agents import Candidate
from src.solana.broker import Broker
from src.solana.ledger import Ledger, now_iso
from src.solana.market_data import request_json

VENUE = "hyperliquid"
BASE_TAKER_BPS = 4.5   # tier-0 perps taker fee


def split_coin(coin: str) -> tuple[str, str]:
    """'xyz:GOLD' -> ('xyz', 'xyz:GOLD'); 'BTC' -> ('', 'BTC'). The API wants the full prefixed coin."""
    return (coin.split(":", 1)[0], coin) if ":" in coin else ("", coin)


def instrument_id(coin: str) -> str:
    return f"hl:{coin}"


def coin_of(instrument: str) -> str:
    if not instrument.startswith("hl:"):
        raise ValueError(f"not a Hyperliquid instrument: {instrument}")
    return instrument[3:]


def info(body: dict, url: str | None = None):
    return request_json("POST", url or cfg.HYPERLIQUID_INFO_URL, json=body)


def l2_book(coin: str) -> dict:
    """{'bids': [(px, sz)...] best first, 'asks': [...]} — levels[0] are bids, levels[1] asks."""
    raw = info({"type": "l2Book", "coin": split_coin(coin)[1]})
    bids, asks = raw["levels"]
    return {"bids": [(float(l["px"]), float(l["sz"])) for l in bids],
            "asks": [(float(l["px"]), float(l["sz"])) for l in asks]}


_ctx_cache: dict[tuple, tuple[float, dict]] = {}


def asset_ctxs(dex: str = "", url: str | None = None) -> dict[str, dict]:
    """coin -> {meta..., ctx...} for one perp dex, cached 60s."""
    hit = _ctx_cache.get((url, dex))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    body = {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}
    meta, ctxs = info(body, url)
    out = {a["name"]: {**a, **c} for a, c in zip(meta["universe"], ctxs)}
    _ctx_cache[(url, dex)] = (time.time(), out)
    return out


def asset_ctx(coin: str, url: str | None = None) -> dict:
    dex, full = split_coin(coin)
    ctx = asset_ctxs(dex, url).get(full)
    if ctx is None:
        raise RuntimeError(f"unknown Hyperliquid market {full}")
    return ctx


def taker_fee_bps(ctx: dict) -> float:
    """Tier-0 taker; HIP-3: x(scale*2) if scale>=1 else x(scale+1); growth mode x0.1 (docs: trading/fees)."""
    if ":" not in ctx.get("name", ""):
        return BASE_TAKER_BPS
    scale = float(ctx.get("deployerFeeScale") or 1.0)
    mult = scale * 2 if scale >= 1 else scale + 1
    if ctx.get("growthMode") == "enabled":
        mult *= 0.1
    return BASE_TAKER_BPS * mult


def candles(coin: str, interval: str = "1h", start_ms: int | None = None, end_ms: int | None = None,
            url: str | None = None) -> list[dict]:
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = start_ms or end_ms - 400 * 3600 * 1000      # ≥ 200 closed bars for EMA200
    rows = info({"type": "candleSnapshot", "req": {"coin": split_coin(coin)[1], "interval": interval,
                                                  "startTime": start_ms, "endTime": end_ms}}, url)
    return [dict(t=r["t"] // 1000, open=float(r["o"]), high=float(r["h"]), low=float(r["l"]),
                 close=float(r["c"]), volume=float(r["v"]) * float(r["c"])) for r in rows]


def funding_history(coin: str, start_ms: int, end_ms: int | None = None) -> dict[int, float]:
    """hour-epoch-seconds -> hourly funding rate (paged, 500 rows per call)."""
    out, cursor = {}, start_ms
    end_ms = end_ms or int(time.time() * 1000)
    while cursor < end_ms:
        rows = info({"type": "fundingHistory", "coin": split_coin(coin)[1], "startTime": cursor, "endTime": end_ms})
        if not rows:
            break
        for r in rows:
            out[r["time"] // 3_600_000 * 3600] = float(r["fundingRate"])
        if len(rows) < 500:
            break
        cursor = rows[-1]["time"] + 1
    return out


def walk(levels: list[tuple[float, float]], *, usd: float | None = None, qty: float | None = None) -> tuple[float, float]:
    """Consume book levels for a USD notional (buy) or a coin quantity (sell). Returns (qty, usd)."""
    got_qty, got_usd = 0.0, 0.0
    for px, sz in levels:
        if usd is not None:
            take = min(sz, (usd - got_usd) / px)
        else:
            take = min(sz, qty - got_qty)
        got_qty += take
        got_usd += take * px
        if (usd is not None and got_usd >= usd - 1e-9) or (qty is not None and got_qty >= qty - 1e-12):
            return got_qty, got_usd
    raise RuntimeError("order larger than visible book depth (20 levels) — refusing paper fill")


class LeverageError(ValueError):
    pass


class HyperliquidPaperBroker(Broker):
    venue = VENUE

    def __init__(self, ledger: Ledger, book_fn=l2_book, ctx_fn=asset_ctx, leverage: float = 1.0,
                 now_fn=time.time):
        if not 1.0 <= leverage <= 3.0:
            raise LeverageError("paper leverage must be between 1x and 3x")
        self.ledger, self.book_fn, self.ctx_fn, self.now_fn = ledger, book_fn, ctx_fn, now_fn
        self.leverage = leverage

    def buy(self, order_id, instrument, symbol, usd, decimals=0, *, strategy, stop_price, tp_price, meta=None):
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        coin = coin_of(instrument)
        ctx = self.ctx_fn(coin)
        fee = usd * taker_fee_bps(ctx) / 1e4
        # isolated margin: open notional may reach (equity at cost) x leverage; at 1x that is just the free cash
        open_cost = sum(p["cost_usd"] for p in self.ledger.positions_from_fills().values())
        room = (self.ledger.cash() + open_cost) * self.leverage - open_cost
        if usd + fee > room + 1e-9:
            raise LeverageError(f"notional ${usd:.2f} + fee exceeds buying power ${room:.2f} at {self.leverage:g}x")
        book = self.book_fn(coin)
        mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
        qty, _ = walk(book["asks"], usd=usd)
        vwap = usd / qty
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="buy", mint=instrument, symbol=symbol,
            strategy=strategy, qty=qty, decimals=0, usd_gross=usd, fee_usd=fee, price=vwap, quote_price=mid,
            price_impact_pct=(vwap / mid - 1) * 100, slippage_bps=round((vwap / mid - 1) * 1e4),
            route=f"L2 book {coin} (taker {taker_fee_bps(ctx):.2f} bps)", stop_price=stop_price, tp_price=tp_price,
            meta=json.dumps({**(meta or {}), "leverage": 1, "margin": "isolated"}),
        ))
        return fill

    def quote_sell_value(self, instrument, qty, decimals=0):
        book = self.book_fn(coin_of(instrument))
        _, usd = walk(book["bids"], qty=qty)
        return usd, book

    def sell(self, order_id, instrument, symbol, qty, decimals=0, *, strategy, meta=None):
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        coin = coin_of(instrument)
        book = self.book_fn(coin)
        mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
        _, usd = walk(book["bids"], qty=qty)
        vwap = usd / qty
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="sell", mint=instrument, symbol=symbol,
            strategy=strategy, qty=qty, decimals=0, usd_gross=usd, fee_usd=usd * taker_fee_bps(self.ctx_fn(coin)) / 1e4,
            price=vwap, quote_price=mid, price_impact_pct=(1 - vwap / mid) * 100,
            slippage_bps=round((1 - vwap / mid) * 1e4), route=f"L2 book {coin}", stop_price=None, tp_price=None,
            meta=json.dumps(meta or {}),
        ))
        return fill

    def accrue(self, positions: list[dict]) -> float:
        """Hourly funding on open longs since the last accrual (or entry). Positive rate => longs pay."""
        total = 0.0
        for p in positions:
            if p.get("venue") != self.venue:
                continue
            last = self.ledger.last_funding_ts(p["entry_order_id"]) or p["opened_at"]
            hours = (self.now_fn() - datetime.fromisoformat(last).timestamp()) / 3600
            if hours < 1 / 60:
                continue
            ctx = self.ctx_fn(coin_of(p["mint"]))
            notional = p["qty"] * float(ctx["markPx"])
            total += self.ledger.add_funding(self.venue, p["mint"], p["entry_order_id"], float(ctx["funding"]),
                                             hours, notional)
        return total


class HyperliquidScout:
    """Fixed list of liquid markets; perps don't need the Solana SafetyAgent."""

    def __init__(self, markets=None, ctx_fn=asset_ctx):
        self.markets = markets or cfg.HL_MARKETS
        self.ctx_fn = ctx_fn

    def candidates(self) -> list[Candidate]:
        out = []
        for coin in self.markets:
            try:
                ctx = self.ctx_fn(coin)
            except RuntimeError:          # market not listed on this network (e.g. XRP on testnet)
                continue
            mark = float(ctx["markPx"])
            out.append(Candidate(
                mint=instrument_id(coin), symbol=coin, pair_address=coin, dex="hyperliquid", price_usd=mark,
                liquidity_usd=float(ctx.get("openInterest") or 0) * mark,
                volume_h24=float(ctx.get("dayNtlVlm") or 0), pair_created_at_ms=None,
                price_change={"funding_1h": float(ctx.get("funding") or 0)}, decimals=0, venue=VENUE,
            ))
        return out
