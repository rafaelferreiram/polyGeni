"""
Hyperliquid LIVE execution — testnet first. Same agents and engine as paper; only the broker is real.

Safety model (all enforced here, not in the UI):
- Two keys: an API ("agent") wallet in .env (it can trade but cannot withdraw) AND an explicit arm action.
  Mainnet additionally requires LIVE_TRADING=1.
- isolated leverage LIVE_LEVERAGE (default 2x, max 3x), long-only; hard USD caps on capital and position size;
  Hyperliquid's $10 minimum respected.
- Every entry gets reduce-only stop-loss and take-profit trigger orders ON THE EXCHANGE. If protection cannot be
  placed, the position is closed immediately.
- Client order ids derived from our order_id make submissions idempotent across crashes/retries.
- Each cycle the exchange is the source of truth: native stop/TP exits are recorded from fills; untracked
  positions or equity drift trip the kill switch.
The private key is read from the environment only and never logged or returned.
"""
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from src.solana import config as cfg
from src.solana import hyperliquid as hl
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, SafetyAgent, Vote
from src.solana.broker import Broker
from src.solana.engine import PaperEngine
from src.solana.ledger import Ledger, now_iso
from src.solana.llm_trader import LLMDecider
from src.solana.strategies import Signal, default_strategies

logger = logging.getLogger("solana.live")

API_URLS = {"testnet": "https://api.hyperliquid-testnet.xyz", "mainnet": "https://api.hyperliquid.xyz"}
MIN_ORDER_USD = 10.0
TAKER_FALLBACK_BPS = 9.0


def network() -> str:
    return cfg.HL_NETWORK if cfg.HL_NETWORK in API_URLS else "testnet"


def info_url() -> str:
    return API_URLS[network()] + "/info"


def api_key() -> str:
    """One API wallet per network: HL_API_WALLET_KEY_TESTNET / _MAINNET, falling back to HL_API_WALLET_KEY."""
    return os.getenv(f"HL_API_WALLET_KEY_{network().upper()}", "") or os.getenv("HL_API_WALLET_KEY", "")


def config_status() -> dict:
    """What is configured, without ever exposing the key."""
    problems, api_addr = [], None
    key = api_key()
    if not key:
        problems.append(f"HL_API_WALLET_KEY_{network().upper()} em falta no .env (chave privada da API wallet desta rede)")
    else:
        try:
            from eth_account import Account
            api_addr = Account.from_key(key).address
        except Exception:
            problems.append("HL_API_WALLET_KEY inválida")
    if not cfg.HL_ACCOUNT_ADDRESS.startswith("0x") or len(cfg.HL_ACCOUNT_ADDRESS) != 42:
        problems.append("HL_ACCOUNT_ADDRESS em falta ou inválido (o teu endereço 0x da MetaMask)")
    if network() == "mainnet" and not cfg.LIVE_TRADING_ALLOWED:
        problems.append("mainnet exige LIVE_TRADING=1 no .env")
    return {"network": network(), "account": cfg.HL_ACCOUNT_ADDRESS or None, "api_wallet": api_addr,
            "key_configured": bool(key), "problems": problems}


def cloid_for(order_id: str):
    from hyperliquid.utils.types import Cloid
    return Cloid("0x" + hashlib.sha256(order_id.encode()).hexdigest()[:32])


def _round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perps: ≤5 significant figures and ≤ (6 - szDecimals) decimals."""
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def _floor_sz(sz: float, sz_decimals: int) -> float:
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f      # epsilon: 0.00015 * 1e5 is 14.999999… in floating point


def _order_size(usd: float, mid: float, sz_decimals: int, max_over: float = 0.2) -> float:
    """Coin size for a USD order on the exchange's lot grid. Expensive coins with coarse lots (ZEC ≈ $1,300 trades
    in 0.01 = $13) can floor to below the $10 minimum: then take one lot more if it costs at most max_over (20%)
    above what Risk approved; otherwise refuse."""
    f = 10 ** sz_decimals
    sz = _floor_sz(usd / mid, sz_decimals)
    if sz * mid >= MIN_ORDER_USD:
        return sz
    up = math.ceil(usd / mid * f - 1e-9) / f
    if MIN_ORDER_USD <= up * mid <= usd * (1 + max_over):
        return up
    raise RuntimeError(f"${usd:.2f} cannot be sized on {mid:.6g} with lot {1 / f:g}: the nearest lot is "
                       f"${up * mid:.2f} (Hyperliquid minimum ${MIN_ORDER_USD:.0f})")


def _parse_fill(resp) -> tuple[float, float, int]:
    """(filled_size, avg_px, oid) from an IOC order response, or raise with the exchange's message."""
    if not resp or resp.get("status") != "ok":
        raise RuntimeError(f"order rejected: {resp}")
    st = resp["response"]["data"]["statuses"][0]
    if "filled" in st:
        f = st["filled"]
        return float(f["totalSz"]), float(f["avgPx"]), int(f["oid"])
    raise RuntimeError(f"order not filled: {st}")


def _resting_oid(resp) -> int:
    if not resp or resp.get("status") != "ok":
        raise RuntimeError(f"trigger rejected: {resp}")
    st = resp["response"]["data"]["statuses"][0]
    if "resting" in st:
        return int(st["resting"]["oid"])
    raise RuntimeError(f"trigger not accepted: {st}")


class HyperliquidLiveBroker(Broker):
    venue = hl.VENUE

    def __init__(self, ledger: Ledger, exchange=None, info=None, address: str | None = None,
                 slippage: float = 0.01, now_fn=time.time):
        self.ledger, self._exchange, self._info = ledger, exchange, info
        self.address = address or cfg.HL_ACCOUNT_ADDRESS
        self.slippage, self.now_fn = slippage, now_fn

    # ─── connection (lazy: building Exchange fetches metadata over the network) ───
    def _connect(self):
        if self._exchange is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            wallet = Account.from_key(api_key())
            self._exchange = Exchange(wallet, API_URLS[network()], account_address=self.address, perp_dexs=["", "xyz"])
            self._info = self._exchange.info
        return self._exchange, self._info

    def _sz_decimals(self, coin: str) -> int:
        _, info = self._connect()
        return info.asset_to_sz_decimals[info.name_to_asset(coin)]

    def _mid(self, coin: str) -> float:
        _, info = self._connect()
        return float(info.all_mids(hl.split_coin(coin)[0])[coin])

    def _fee(self, oid: int, notional: float) -> float:
        _, info = self._connect()
        try:
            fees = [float(f["fee"]) for f in info.user_fills(self.address) if int(f.get("oid", -1)) == oid]
            if fees:
                return sum(fees)
        except Exception as e:
            logger.warning(f"fee lookup failed: {e}")
        return notional * TAKER_FALLBACK_BPS / 1e4

    def abstraction(self) -> str:
        """'disabled' (standard: separate perp/spot) | 'unifiedAccount' | 'portfolioMargin'."""
        _, info = self._connect()
        try:
            return info.query_user_abstraction_state(self.address) or "disabled"
        except Exception:
            return "disabled"

    def account_value(self) -> float:
        _, info = self._connect()
        if self.abstraction() in ("unifiedAccount", "portfolioMargin"):
            # docs: unified/portfolio-margin balances and holds live in the SPOT clearinghouse state; per-DEX perp
            # account values are "not meaningful". Equity = stablecoin collateral + open positions' unrealized P&L.
            spot = info.spot_user_state(self.address)
            cash = sum(float(b["total"]) for b in spot.get("balances", []) if b["coin"] in ("USDC", "USDT0", "USDH", "USDE"))
            upnl = sum(float(p["position"].get("unrealizedPnl") or 0)
                       for dex in ("", "xyz") for p in info.user_state(self.address, dex)["assetPositions"])
            return cash + upnl
        return sum(float(info.user_state(self.address, dex)["marginSummary"]["accountValue"]) for dex in ("", "xyz"))

    def balances(self) -> dict:
        """Collateral split as the exchange sees it: total, held as margin by open positions, and free to use."""
        _, info = self._connect()
        stable = ("USDC", "USDT0", "USDH", "USDE")
        if self.abstraction() in ("unifiedAccount", "portfolioMargin"):
            bal = [b for b in info.spot_user_state(self.address).get("balances", []) if b["coin"] in stable]
            total, hold = sum(float(b["total"]) for b in bal), sum(float(b["hold"]) for b in bal)
            return {"collateral": round(total, 4), "margin_used": round(hold, 4), "available": round(total - hold, 4)}
        s = info.user_state(self.address)
        return {"collateral": float(s["marginSummary"]["accountValue"]), "margin_used": float(s["marginSummary"]["totalMarginUsed"]),
                "available": float(s.get("withdrawable") or 0)}

    def exchange_positions(self) -> dict[str, float]:
        _, info = self._connect()
        out = {}
        for dex in ("", "xyz"):
            for p in info.user_state(self.address, dex)["assetPositions"]:
                szi = float(p["position"]["szi"])
                if szi:
                    out[p["position"]["coin"]] = szi
        return out

    def _protect(self, coin: str, sz: float, stop_price: float, tp_price: float, szd: int) -> dict:
        """Reduce-only SL + TP trigger orders resting on the exchange."""
        ex, _ = self._connect()
        oids = {}
        for kind, px in (("sl", stop_price), ("tp", tp_price)):
            trig = _round_px(px, szd)
            resp = ex.order(coin, False, sz, trig, {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": kind}},
                            reduce_only=True)
            oids[kind] = _resting_oid(resp)
        return oids

    def buy(self, order_id, instrument, symbol, usd, decimals=0, *, strategy, stop_price, tp_price, meta=None):
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        ex, info = self._connect()
        coin = hl.coin_of(instrument)
        cloid = cloid_for(order_id)
        if info.query_order_by_cloid(self.address, cloid).get("status") == "order":
            # we crashed after submitting: never submit twice; flag for a human
            raise RuntimeError(f"order {order_id} already exists on the exchange but not in the ledger — reconcile manually")
        szd = self._sz_decimals(coin)
        mid = self._mid(coin)
        sz = _order_size(usd, mid, szd)
        lev = ex.update_leverage(int(cfg.LIVE_LEVERAGE), coin, is_cross=False)       # isolated, every time
        if not lev or lev.get("status") != "ok":
            raise RuntimeError(f"could not set {int(cfg.LIVE_LEVERAGE)}x isolated leverage: {lev}")
        filled, avg, oid = _parse_fill(ex.market_open(coin, True, sz, slippage=self.slippage, cloid=cloid))
        try:
            prot = self._protect(coin, filled, stop_price, tp_price, szd)
        except Exception as e:
            ex.market_close(coin, sz=filled, slippage=self.slippage)   # never leave a position unprotected
            raise RuntimeError(f"protection orders failed, position closed immediately: {e}")
        notional = filled * avg
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="buy", mint=instrument, symbol=symbol,
            strategy=strategy, qty=filled, decimals=0, usd_gross=notional, fee_usd=self._fee(oid, notional),
            price=avg, quote_price=mid, price_impact_pct=(avg / mid - 1) * 100,
            slippage_bps=round((avg / mid - 1) * 1e4), route=f"Hyperliquid {network()} IOC oid {oid}",
            stop_price=stop_price, tp_price=tp_price,
            meta=json.dumps({**(meta or {}), "oid": oid, "sl_oid": prot["sl"], "tp_oid": prot["tp"],
                             "network": network(), "leverage": 1, "requested_usd": usd}),
        ))
        return fill

    def _cancel_protection(self, coin: str, entry_order_id: str):
        ex, _ = self._connect()
        entry = self.ledger.get_fill(entry_order_id) or {}
        m = json.loads(entry.get("meta") or "{}")
        for k in ("sl_oid", "tp_oid"):
            if m.get(k):
                try:
                    ex.cancel(coin, m[k])
                except Exception as e:                       # already triggered/cancelled is fine
                    logger.info(f"cancel {k} {m[k]}: {e}")

    def sell(self, order_id, instrument, symbol, qty, decimals=0, *, strategy, meta=None):
        if (existing := self.ledger.get_fill(order_id)):
            return existing
        ex, _ = self._connect()
        coin = hl.coin_of(instrument)
        entry_id = order_id[len("exit-"):] if order_id.startswith("exit-") else None
        if coin not in self.exchange_positions():
            return self._record_native_exit(order_id, instrument, symbol, qty, strategy, entry_id)
        mid = self._mid(coin)
        filled, avg, oid = _parse_fill(ex.market_close(coin, sz=qty, slippage=self.slippage, cloid=cloid_for(order_id)))
        if entry_id:
            self._cancel_protection(coin, entry_id)
        notional = filled * avg
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="sell", mint=instrument, symbol=symbol,
            strategy=strategy, qty=filled, decimals=0, usd_gross=notional, fee_usd=self._fee(oid, notional),
            price=avg, quote_price=mid, price_impact_pct=(1 - avg / mid) * 100,
            slippage_bps=round((1 - avg / mid) * 1e4), route=f"Hyperliquid {network()} IOC oid {oid}",
            stop_price=None, tp_price=None, meta=json.dumps({**(meta or {}), "oid": oid, "network": network()}),
        ))
        return fill

    def _record_native_exit(self, order_id, instrument, symbol, qty, strategy, entry_id) -> dict:
        """The exchange closed the position (our SL/TP trigger fired): record what actually filled."""
        _, info = self._connect()
        coin = hl.coin_of(instrument)
        entry = self.ledger.get_fill(entry_id) if entry_id else None
        since = datetime.fromisoformat(entry["ts"]).timestamp() * 1000 if entry else 0
        fills = [f for f in info.user_fills(self.address)
                 if f["coin"] == coin and f["side"] == "A" and f["time"] >= since]
        sz = sum(float(f["sz"]) for f in fills)
        if sz <= 0:
            raise RuntimeError(f"{coin} is gone from the exchange but no closing fills were found")
        notional = sum(float(f["sz"]) * float(f["px"]) for f in fills)
        avg = notional / sz
        if entry_id:
            self._cancel_protection(coin, entry_id)
        fill, _ = self.ledger.append_fill(dict(
            order_id=order_id, ts=now_iso(), venue=self.venue, side="sell", mint=instrument, symbol=symbol,
            strategy=strategy, qty=min(sz, qty), decimals=0, usd_gross=notional,
            fee_usd=sum(float(f.get("fee", 0)) for f in fills), price=avg, quote_price=avg, price_impact_pct=0,
            slippage_bps=0, route=f"Hyperliquid {network()} native trigger",
            stop_price=None, tp_price=None, meta=json.dumps({"reason": "stop/take-profit filled on the exchange",
                                                             "oids": [f.get("oid") for f in fills]}),
        ))
        return fill

    def quote_sell_value(self, instrument, qty, decimals=0):
        _, info = self._connect()
        book = info.l2_snapshot(hl.coin_of(instrument))
        bids = [(float(l["px"]), float(l["sz"])) for l in book["levels"][0]]
        return hl.walk(bids, qty=qty)[1], book

    def accrue(self, positions: list[dict]) -> float:
        """Record funding actually paid on the exchange since the last accrual."""
        _, info = self._connect()
        total = 0.0
        for p in positions:
            if p.get("venue") != self.venue:
                continue
            last = self.ledger.last_funding_ts(p["entry_order_id"]) or p["opened_at"]
            start = int(datetime.fromisoformat(last).timestamp() * 1000) + 1
            coin = hl.coin_of(p["mint"])
            for ev in info.user_funding_history(self.address, start):
                d = ev.get("delta", {})
                rate = float(d.get("fundingRate", 0))
                if d.get("coin") != coin or not rate:
                    continue
                paid = -float(d.get("usdc", 0))              # exchange reports usdc < 0 when we pay
                # store the exact amount paid: notional chosen so that notional * rate * 1h == paid
                total += self.ledger.add_funding(self.venue, p["mint"], p["entry_order_id"], rate, 1.0, paid / rate)
        return total

    def record_transfers(self) -> list[dict]:
        """Deposits, withdrawals and transfers made outside the bot (e.g. adding funds) become cash movements in the
        ledger, so they count as contributions — not profit — and never look like an unexplained balance drift."""
        since = self.ledger.get_state("transfers_since")
        if since is None:              # first run: everything before is already in the opening balance
            self.ledger.set_state("transfers_since", int(time.time() * 1000))
            return []
        _, info = self._connect()
        rows = info.post("/info", {"type": "userNonFundingLedgerUpdates", "user": self.address, "startTime": since + 1}) or []
        me, done = self.address.lower(), []
        for r in sorted(rows, key=lambda x: x["time"]):
            d = r.get("delta", {})
            amount = float(d.get("usdc") or d.get("usdcValue") or d.get("amount") or 0)
            src, dst = str(d.get("user", "")).lower(), str(d.get("destination", "")).lower()
            if d.get("type") == "deposit" or (dst == me and src != me):
                kind = "deposit"
            elif d.get("type") == "withdraw" or (src == me and dst and dst != me):
                kind = "withdraw"
            else:
                continue                       # spot <-> perp moves inside the same account
            if amount > 0:
                self.ledger.move_cash(kind, amount, f"{d.get('type')} on Hyperliquid ({r.get('hash', '')[:12]})", check=False)
                self.ledger.log_decision("funds", reason=f"{'💵 depósito' if kind == 'deposit' else '🏧 saque'} de ${amount:.2f} registado")
                done.append({"kind": kind, "usd": amount, "time": r["time"]})
        if rows:
            self.ledger.set_state("transfers_since", max(r["time"] for r in rows))
        return done

    def sync(self) -> list[str]:
        """Exchange is the truth: record native exits, flag untracked positions and equity drift."""
        problems = []
        try:
            self.record_transfers()
        except Exception as e:           # never let the transfer log break the safety sync
            logger.warning(f"transfer history: {e}")
        ex_pos = self.exchange_positions()
        mine = {p["mint"]: p for p in self.ledger.positions() if p.get("venue") == self.venue}
        for mint, p in mine.items():
            if hl.coin_of(mint) not in ex_pos:
                self._record_native_exit(f"exit-{p['entry_order_id']}", mint, p["symbol"], p["qty"], p["strategy"],
                                         p["entry_order_id"])
                self.ledger.log_decision("exit", mint=mint, symbol=p["symbol"], strategy=p["strategy"],
                                         order_id=f"exit-{p['entry_order_id']}",
                                         reason="stop-loss/take-profit executed by the exchange")
        tracked = {hl.coin_of(m) for m in mine}
        for coin, szi in ex_pos.items():
            if coin not in tracked:
                problems.append(f"position {coin} ({szi}) on the exchange is not in the ledger")
            elif szi < 0:
                problems.append(f"{coin} is short on the exchange — this bot is long-only")
        equity = self.account_value()
        ledger_equity = self.ledger.cash() + sum(p["cost_usd"] for p in self.ledger.positions())
        if abs(equity - ledger_equity) > max(1.0, 0.05 * max(equity, 1)) and not ex_pos and not mine:
            problems.append(f"account value ${equity:.2f} differs from ledger ${ledger_equity:.2f} "
                            "(deposit/withdrawal outside the bot?) — re-arm to resync")
        return problems


# ─── live engine & controls ──────────────────────────────────────────────────

def _risk() -> RiskAgent:
    return RiskAgent(max_pos_pct=1.0, max_exposure_pct=1.0, max_positions=cfg.LIVE_MAX_POSITIONS,
                     max_daily_loss_pct=cfg.LIVE_MAX_DAILY_LOSS_PCT, min_trade_usd=MIN_ORDER_USD,
                     max_entries_per_day=cfg.LIVE_MAX_ENTRIES_PER_DAY,
                     max_position_usd=cfg.LIVE_MAX_POSITION_USD, max_capital_usd=cfg.LIVE_MAX_CAPITAL_USD,
                     leverage=cfg.LIVE_LEVERAGE)


class _NoSafety:
    min_age_h = 0

    def check(self, c):
        return Vote("safety", True, "n/a for perps")


_live: PaperEngine | None = None


def get_live_engine() -> PaperEngine:
    global _live
    if _live is None:
        ledger = Ledger(cfg.LIVE_DB_PATH, 0.0)
        url = info_url()
        _live = PaperEngine(
            ledger, [hl.HyperliquidScout(cfg.HL_LIVE_MARKETS, ctx_fn=lambda c: hl.asset_ctx(c, url))], _NoSafety(),
            default_strategies(), DebateAgent(), _risk(), ExecutorAgent(HyperliquidLiveBroker(ledger)),
            candles_fn={hl.VENUE: lambda coin: hl.candles(coin, url=url)},
            profiles={hl.VENUE: {"max_pos_pct": 1.0, "stop_mult": cfg.MARKET_PROFILES.get(hl.VENUE, {}).get("stop_mult", 1.5)}},
            llm=team(), coordinator=_coordinate_real,
        )
        from src.solana import arena
        _live.size_policy = arena.size_for
    return _live


def team() -> list:
    """All bots propose for the one shared wallet; LLM bots reuse their lab decision for the same candle."""
    from src.solana import arena
    from src.solana.llm_trader import TeamMember
    return [TeamMember(m, lab_lookup=arena.lab_decider) for m in cfg.ARENA_MODELS]


def _coordinate(market, members):
    from src.solana import arena
    return arena.rank(market, members)


def _coordinate_real(market, members):
    """Like the simulation, but only bots with lab evidence may spend real money."""
    from src.solana import arena
    return arena.rank(market, arena.eligible_for_real(members))


_shadow: PaperEngine | None = None


def get_shadow_engine() -> PaperEngine:
    """'What would the real account do?': identical limits and decision mode, but fills are simulated on real
    Hyperliquid MAINNET order books with fake money (SHADOW_START_USDC). It scans HL_MARKETS (the same
    broad set as the real account by default)."""
    global _shadow
    if _shadow is None:
        ledger = Ledger(cfg.SHADOW_DB_PATH, cfg.SHADOW_START_USDC)
        from src.solana.engine import bench_prices
        _shadow = PaperEngine(
            ledger, [hl.HyperliquidScout(cfg.HL_MARKETS)], _NoSafety(), default_strategies(), DebateAgent(),
            _risk(), ExecutorAgent(hl.HyperliquidPaperBroker(ledger, leverage=cfg.LIVE_LEVERAGE)), candles_fn={hl.VENUE: hl.candles},
            profiles={hl.VENUE: {"max_pos_pct": 1.0, "stop_mult": cfg.MARKET_PROFILES.get(hl.VENUE, {}).get("stop_mult", 1.5)}},
            llm=team(), coordinator=_coordinate, bench_fn=bench_prices,
        )
        from src.solana import arena
        _shadow.size_policy = arena.size_for
        if ledger.get_state("running") is None:
            ledger.set_state("running", True)
    return _shadow


def shadow_cycle() -> dict | None:
    eng = get_shadow_engine()
    if not eng.ledger.get_state("running", False):
        return None
    # mirror the real account's decision mode (the team and coordinator are identical by construction)
    eng.ledger.set_state("decision_mode", get_live_engine().decision_mode())
    return eng.run_cycle()


def shadow_status() -> dict:
    from src.api.paper_routes import performance
    eng = get_shadow_engine()
    led = eng.ledger
    p = eng.portfolio()
    trades = led.closed_trades()
    return {"mode": "PAPER", "running": led.get_state("running", False), "scanning": eng.scanning(),
            "decision_mode": eng.decision_mode(), "model": "equipe",
            "start_usdc": cfg.SHADOW_START_USDC, "equity": round(p.equity, 4), "cash": round(p.cash, 4),
            "exposure": round(p.exposure, 4), "pnl_usd": round(p.equity - led.contributed(), 4),
            "performance": performance(led.equity_history(100_000)), "positions": led.positions(),
            "trades": trades[:50], "win_rate": round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 3) if trades else None,
            "decisions": led.decisions(15), "last_cycle": led.get_state("last_cycle"), "cycle": eng.cycle,
            "kill_switch": eng.kill_switch()}


def _broker(eng: PaperEngine) -> HyperliquidLiveBroker:
    return eng.executor._b(hl.VENUE)


def schedule():
    """Register the live job (a no-op while disarmed); safe to call more than once."""
    from src.solana import engine as paper_engine
    sch = paper_engine.scheduler()
    from datetime import datetime, timedelta
    if sch.get_job("live_cycle") is None:   # first run right after a (re)start, not 5 min later
        sch.add_job(live_cycle, "interval", seconds=cfg.LIVE_CYCLE_SEC, id="live_cycle", max_instances=1, coalesce=True,
                    next_run_time=datetime.now() + timedelta(seconds=20))
    if sch.get_job("daily_report") is None:
        sch.add_job(write_daily_reports, "cron", hour=23, minute=55, id="daily_report", coalesce=True)
    if sch.get_job("shadow_cycle") is None:
        sch.add_job(shadow_cycle, "interval", seconds=cfg.LIVE_CYCLE_SEC, id="shadow_cycle", max_instances=1,
                    coalesce=True, next_run_time=datetime.now())


def is_armed() -> bool:
    return bool(get_live_engine().ledger.get_state("armed", {}).get("armed"))


def arm() -> dict:
    """Checks every precondition against the exchange, syncs the ledger to the real balance, then arms."""
    st = config_status()
    if st["problems"]:
        raise ValueError("; ".join(st["problems"]))
    eng = get_live_engine()
    if eng.kill_switch().get("active"):
        raise ValueError("kill switch ativo — rearme-o primeiro")
    br = _broker(eng)
    _, info = br._connect()
    agents = {a["address"].lower() for a in (info.extra_agents(br.address) or [])}
    if st["api_wallet"].lower() not in agents:
        raise ValueError("a API wallet não está autorizada nesta conta (crie/autorize-a em More → API na Hyperliquid)")
    value = br.account_value()
    if value < MIN_ORDER_USD + 1:
        raise ValueError(f"saldo na Hyperliquid ${value:.2f}: precisa de pelo menos ${MIN_ORDER_USD + 1:.0f}")
    if br.exchange_positions() and not eng.ledger.positions():
        raise ValueError("há posições abertas na exchange que o bot não abriu — feche-as antes de ativar")
    drift = value - (eng.ledger.cash() + sum(p["cost_usd"] for p in eng.ledger.positions()))
    if abs(drift) > 0.01:
        eng.ledger.move_cash("deposit" if drift > 0 else "withdraw", abs(drift), f"sync with {network()} account")
    eng.ledger.set_state("transfers_since", int(time.time() * 1000))   # later deposits/withdrawals are tracked
    eng.ledger.set_state("armed", {"armed": True, "ts": now_iso(), "network": network()})
    eng.ledger.log_decision("arm", cycle=eng.cycle, reason=f"live trading armed on {network()} (balance ${value:.2f})")
    return {"armed": True, "network": network(), "balance": value}


def disarm(reason: str = "manual") -> dict:
    eng = get_live_engine()
    eng.ledger.set_state("armed", {"armed": False, "ts": now_iso()})
    eng.ledger.log_decision("disarm", cycle=eng.cycle, reason=f"live trading disarmed: {reason} "
                                                              "(open positions keep their exchange stop/TP)")
    return {"armed": False}


def live_cycle() -> dict | None:
    """Scheduler entry point: does nothing unless armed and still correctly configured."""
    if not is_armed():
        return None
    if config_status()["problems"]:
        disarm("configuration changed")
        return None
    return get_live_engine().run_cycle()


def flatten(reason: str = "manual flatten") -> dict:
    """Close every live position at market and cancel its exchange stops. Also disarms."""
    eng = get_live_engine()
    disarm(reason)
    closed, errors = [], []
    for p in eng.ledger.positions():
        try:
            fill = eng.executor.close(f"exit-{p['entry_order_id']}", p, reason)
            eng.ledger.log_decision("exit", mint=p["mint"], symbol=p["symbol"], strategy=p["strategy"],
                                    order_id=fill["order_id"], reason=reason)
            closed.append(p["symbol"])
        except Exception as e:
            errors.append(f"{p['symbol']}: {e}")
    eng.reconcile()
    return {"closed": closed, "errors": errors}


class _TestSignal:
    """Testnet only: a minimal entry through the full Risk/Executor path, to prove execution end to end."""
    name, capital_pct = "test_order", 1.0

    def signal(self, candles):
        return Signal(self.name, candles[-1]["close"], 0.02, 0.02, "manual testnet test order") if candles else None


def test_order(coin: str = "BTC") -> dict:
    if network() != "testnet":
        raise ValueError("ordens de teste só em testnet")
    if not is_armed():
        raise ValueError("ative o trading primeiro (testnet)")
    eng = get_live_engine()
    url = info_url()
    one = PaperEngine(eng.ledger, [hl.HyperliquidScout([coin], ctx_fn=lambda c: hl.asset_ctx(c, url))], _NoSafety(),
                      [_TestSignal()], DebateAgent(enabled=False), _risk(), eng.executor,
                      candles_fn={hl.VENUE: lambda c: hl.candles(c, url=url)}, profiles={hl.VENUE: {"max_pos_pct": 1.0}})
    stats = one.run_cycle()
    pos = [p for p in eng.ledger.positions() if p["mint"] == hl.instrument_id(coin)]
    return {"cycle": stats, "position": pos[0] if pos else None,
            "note": "A posição tem stop-loss e take-profit de ±2% na exchange. Use 'Fechar tudo' para fechá-la agora."}


def status() -> dict:
    eng = get_live_engine()
    st = config_status()
    out = {"config": st, "armed": is_armed(), "decision_mode": eng.decision_mode(), "kill_switch": eng.kill_switch(), "network": network(),
           "limits": {"max_capital_usd": cfg.LIVE_MAX_CAPITAL_USD, "max_position_usd": cfg.LIVE_MAX_POSITION_USD,
                      "max_positions": cfg.LIVE_MAX_POSITIONS, "max_daily_loss_pct": cfg.LIVE_MAX_DAILY_LOSS_PCT,
                      "leverage": 1, "markets": cfg.HL_LIVE_MARKETS},
           "positions": eng.ledger.positions(), "trades": eng.ledger.closed_trades()[:50],
           "decisions": eng.ledger.decisions(30), "last_cycle": eng.ledger.get_state("last_cycle"),
           "ledger_equity": eng.ledger.cash() + sum(p["cost_usd"] for p in eng.ledger.positions())}
    if not st["problems"]:
        try:
            out["exchange_balance"] = _broker(eng).account_value()
        except Exception as e:
            out["exchange_error"] = str(e)[:200]
    return out


# ─── daily report (wins, losses, trades, P&L, costs) ─────────────────────────

def daily_report(eng, days: int = 14) -> list[dict]:
    """One row per local calendar day, newest first. Trade P&L already includes fees and funding."""
    from datetime import date, timedelta as td
    from src.solana import costs
    led = eng.ledger
    trades = led.closed_trades()
    hist = led.equity_history(100_000)
    decs = led.decisions(5000)
    tz = datetime.now().astimezone().tzinfo
    local = lambda iso: datetime.fromisoformat(iso).astimezone(tz).date()
    today = datetime.now(tz).date()
    rows = []
    for i in range(days):
        d = today - td(days=i)
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        end = start + td(days=1)
        t = [x for x in trades if local(x["exit_ts"]) == d]
        eq = [h for h in hist if local(h["ts"]) == d]
        dd = [x for x in decs if local(x["ts"]) == d]
        ai_usd, ai_calls = costs.book().between(start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                                end.astimezone(timezone.utc).isoformat(timespec="seconds"))
        pnl = sum(x["pnl_usd"] for x in t)
        wins = [x for x in t if x["pnl_usd"] > 0]
        if not (t or eq or dd or ai_calls):
            continue
        rows.append({
            "date": d.isoformat(), "trades": len(t), "wins": len(wins), "losses": len(t) - len(wins),
            "win_rate": round(len(wins) / len(t), 3) if t else None, "pnl_usd": round(pnl, 4),
            "fees_usd": round(sum(x["fees_usd"] + (x["funding_usd"] or 0) for x in t), 4),
            "ai_cost_usd": round(ai_usd, 4), "ai_calls": ai_calls, "net_usd": round(pnl - ai_usd, 4),
            "equity_start": round(eq[0]["equity"], 4) if eq else None, "equity_end": round(eq[-1]["equity"], 4) if eq else None,
            "best": max(t, key=lambda x: x["pnl_usd"], default=None) and {k: max(t, key=lambda x: x["pnl_usd"])[k] for k in ("symbol", "pnl_usd", "strategy")},
            "worst": min(t, key=lambda x: x["pnl_usd"], default=None) and {k: min(t, key=lambda x: x["pnl_usd"])[k] for k in ("symbol", "pnl_usd", "strategy")},
            "entries": sum(1 for x in dd if x["kind"] == "entry"), "vetoes": sum(1 for x in dd if x["kind"] == "veto"),
            "sleeps": sum(1 for x in dd if x["kind"] == "sleep"), "errors": sum(1 for x in dd if x["kind"] in ("error", "reconcile")),
            "cycles": len({x["cycle"] for x in eq}),
        })
    return rows


def write_daily_reports(out_dir: str = "reports/daily"):
    """Nightly markdown snapshot of today's report for the real account and the simulation."""
    import os as _os
    _os.makedirs(out_dir, exist_ok=True)
    for kind, eng in (("real", get_live_engine()), ("sim", get_shadow_engine())):
        rows = daily_report(eng, days=1)
        if not rows:
            continue
        r = rows[0]
        lines = [f"# Relatório diário {r['date']} — conta {'real (' + network() + ')' if kind == 'real' else 'simulada'}", "",
                 f"- Trades fechados: **{r['trades']}** (ganhos {r['wins']}, perdas {r['losses']}"
                 + (f", acerto {r['win_rate'] * 100:.0f}%)" if r["win_rate"] is not None else ")"),
                 f"- Lucro dos trades (com taxas e funding): **${r['pnl_usd']:+.4f}** · taxas+funding ${r['fees_usd']:.4f}",
                 f"- Custo da IA: ${r['ai_cost_usd']:.4f} ({r['ai_calls']} análises pagas) · **lucro líquido ${r['net_usd']:+.4f}**",
                 f"- Saldo: {r['equity_start']} → {r['equity_end']} · ciclos {r['cycles']} · compras {r['entries']} · "
                 f"bloqueios {r['vetoes']} · pausas {r['sleeps']} · erros {r['errors']}"]
        if r["best"]:
            lines.append(f"- Melhor trade: {r['best']['symbol']} ${r['best']['pnl_usd']:+.4f} ({r['best']['strategy']}) · "
                         f"pior: {r['worst']['symbol']} ${r['worst']['pnl_usd']:+.4f} ({r['worst']['strategy']})")
        with open(f"{out_dir}/{r['date']}-{kind}.md", "w") as f:
            f.write("\n".join(lines) + "\n")
