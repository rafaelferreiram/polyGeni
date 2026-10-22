"""
Hyperliquid PAPER venue: public Info API only (POST /info). No Exchange API, no signing, no keys.

Markets: native perps BTC/ETH/SOL and HIP-3 trade.xyz perps `xyz:GOLD`, `xyz:SILVER` (`dex:ticker`;
`metaAndAssetCtxs`/`allMids` need {"dex": "xyz"}; l2Book/candles/funding take the prefixed coin).
Paper model: 1x long only, isolated margin == notional (fully collateralised in USDC), taker fills by
walking the real L2 book, taker fee per market, hourly funding accrued on open positions.
Verified against docs + live API 2026-10-21.
"""
import json
import threading
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


SHORT = "~short"     # paper-only: a short is held as a long on the mirror price 2*entry - price (exact short P&L)


def is_short(instrument: str) -> bool:
    return instrument.endswith(SHORT)


def coin_of(instrument: str) -> str:
    if not instrument.startswith("hl:"):
        raise ValueError(f"not a Hyperliquid instrument: {instrument}")
    return instrument[3:].removesuffix(SHORT)


def info(body: dict, url: str | None = None):
    return request_json("POST", url or cfg.HYPERLIQUID_INFO_URL, json=body)


_book_cache: dict[str, tuple[float, dict]] = {}
BOOK_TTL_S = 20      # the lab's paper bots mark dozens of positions a minute: share one book per coin (avoids 429s)


def l2_book(coin: str) -> dict:
    """{'bids': [(px, sz)...] best first, 'asks': [...]} — levels[0] are bids, levels[1] asks. Cached BOOK_TTL_S per coin."""
    hit = _book_cache.get(coin)
    if hit and time.time() - hit[0] < BOOK_TTL_S:
        return hit[1]
    raw = info({"type": "l2Book", "coin": split_coin(coin)[1]})
    bids, asks = raw["levels"]
    book = {"bids": [(float(l["px"]), float(l["sz"])) for l in bids],
            "asks": [(float(l["px"]), float(l["sz"])) for l in asks]}
    _book_cache[coin] = (time.time(), book)
    return book


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


class UnknownMarket(RuntimeError):
    """The coin is not listed on this network (not a network error)."""


def asset_ctx(coin: str, url: str | None = None) -> dict:
    dex, full = split_coin(coin)
    ctx = asset_ctxs(dex, url).get(full)
    if ctx is None:
        raise UnknownMarket(f"unknown Hyperliquid market {full}")
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


_candle_cache: dict[tuple, tuple[int, list[dict]]] = {}
INTERVAL_S = {"1h": 3600, "15m": 900}          # live, cached intervals (1h for every bot, 15m for the lab's 15m twins)
_candle_lock = threading.Lock()


def candles(coin: str, interval: str = "1h", start_ms: int | None = None, end_ms: int | None = None,
            url: str | None = None) -> list[dict]:
    # Live use (default window): every engine (real, simulation, lab bots) asks for the same 30 coins each cycle.
    # Closed 1h candles only change once an hour, so one fetch per coin per hour is shared by all of them —
    # without this, ~150 heavy candleSnapshot calls per round exceed Hyperliquid's 1200 weight/min and return 429.
    step = INTERVAL_S.get(interval, 3600)
    if start_ms is None and end_ms is None and interval in INTERVAL_S:
        with _candle_lock:            # one fetch even when several engines miss at once (e.g. right after a restart)
            bucket = int(time.time()) // step
            key = (coin, url) if interval == "1h" else (coin, url, interval)
            hit = _candle_cache.get(key)
            if hit and hit[0] == bucket:
                return hit[1]
            rows = candles(coin, interval, end_ms=int(time.time() * 1000), url=url)
            _candle_cache[key] = (bucket, rows)
            return rows
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = start_ms or end_ms - 400 * step * 1000        # 400 bars: ≥ 200 closed bars for EMA200
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
        # long: lift the asks. short (lab only): hit the bids; the mirror price starts at the entry, so vwap = entry
        qty, got = walk(book["bids"] if is_short(instrument) else book["asks"], usd=usd)
        vwap = got / qty
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="buy", mint=instrument, symbol=symbol,
            strategy=strategy, qty=qty, decimals=0, usd_gross=usd, fee_usd=fee, price=vwap, quote_price=mid,
            price_impact_pct=(vwap / mid - 1) * 100, slippage_bps=round((vwap / mid - 1) * 1e4),
            route=f"L2 book {coin} (taker {taker_fee_bps(ctx):.2f} bps)", stop_price=stop_price, tp_price=tp_price,
            meta=json.dumps({**(meta or {}), "leverage": 1, "margin": "isolated"}),
        ))
        return fill

    def _entry(self, instrument) -> float:
        """The short's fill price (vwap, before fees) — positions' entry_price includes the fee."""
        pos = next(p for p in self.ledger.positions_from_fills().values() if p["mint"] == instrument)
        return self.ledger.get_fill(pos["entry_order_id"])["price"]

    def _close_value(self, instrument, qty, book) -> float:
        """USD received closing qty: a long sells on the bids; a short buys back on the asks and is worth
        qty * (2*entry - buyback price) — i.e. it gains exactly what the price fell."""
        if not is_short(instrument):
            return walk(book["bids"], qty=qty)[1]
        _, cost = walk(book["asks"], qty=qty)
        return qty * (2 * self._entry(instrument)) - cost

    def quote_sell_value(self, instrument, qty, decimals=0):
        book = self.book_fn(coin_of(instrument))
        return self._close_value(instrument, qty, book), book

    def sell(self, order_id, instrument, symbol, qty, decimals=0, *, strategy, meta=None):
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        coin = coin_of(instrument)
        book = self.book_fn(coin)
        mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
        usd = self._close_value(instrument, qty, book)
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
            rate = float(ctx["funding"]) * (-1 if is_short(p["mint"]) else 1)     # positive funding pays shorts
            total += self.ledger.add_funding(self.venue, p["mint"], p["entry_order_id"], rate, hours, notional)
        return total


class ShortScout:
    """Lab-only: the same markets offered as short instruments (hl:BTC~short)."""

    def __init__(self, inner=None):
        self.inner = inner or HyperliquidScout()

    def candidates(self) -> list[Candidate]:
        out = []
        for c in self.inner.candidates():
            c.mint = c.mint + SHORT
            out.append(c)
        return out


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
            except UnknownMarket:         # not listed on this network (e.g. XRP on testnet); network errors propagate
                continue
            mark = float(ctx["markPx"])
            out.append(Candidate(
                mint=instrument_id(coin), symbol=coin, pair_address=coin, dex="hyperliquid", price_usd=mark,
                liquidity_usd=float(ctx.get("openInterest") or 0) * mark,
                volume_h24=float(ctx.get("dayNtlVlm") or 0), pair_created_at_ms=None,
                price_change={"funding_1h": float(ctx.get("funding") or 0)}, decimals=0, venue=VENUE,
            ))
        return out
