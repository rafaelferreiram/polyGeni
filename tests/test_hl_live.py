"""Hyperliquid LIVE broker against a simulated exchange — no network, no keys."""
import json

import pytest

from src.solana import config as cfg
from src.solana import hl_live
from src.solana.agents import Portfolio, RiskAgent
from src.solana.ledger import Ledger
from src.solana.strategies import Signal

ADDR = "0x374c90fC8Deb979D084e9799117700e908632240"


class FakeInfo:
    def __init__(self):
        self.asset_to_sz_decimals = {0: 5, 1: 2}
        self.mids = {"BTC": 80000.0, "SOL": 100.0}
        self.positions = {}            # coin -> szi
        self.fills = []
        self.known_cloids = set()
        self.account = 20.0

    def name_to_asset(self, coin):
        return {"BTC": 0, "SOL": 1}[coin]

    def all_mids(self, dex=""):
        return {k: str(v) for k, v in self.mids.items()}

    def query_order_by_cloid(self, user, cloid):
        return {"status": "order"} if str(cloid) in self.known_cloids else {"status": "unknownOid"}

    def user_fills(self, user):
        return self.fills

    def user_state(self, user, dex=""):
        if dex:
            return {"assetPositions": [], "marginSummary": {"accountValue": "0"}}
        return {"assetPositions": [{"position": {"coin": c, "szi": str(s)}} for c, s in self.positions.items()],
                "marginSummary": {"accountValue": str(self.account)}}

    def l2_snapshot(self, coin):
        m = self.mids[coin]
        return {"levels": [[{"px": str(m * 0.999), "sz": "100", "n": 1}], [{"px": str(m * 1.001), "sz": "100", "n": 1}]]}

    def user_funding_history(self, user, start):
        return []

    def extra_agents(self, user):
        return []


class FakeExchange:
    def __init__(self, info, fill_ratio=1.0, fail_trigger=False):
        self.info, self.fill_ratio, self.fail_trigger = info, fill_ratio, fail_trigger
        self.calls, self.oid = [], 100

    def _next(self):
        self.oid += 1
        return self.oid

    def update_leverage(self, lev, coin, is_cross=True):
        self.calls.append(("leverage", coin, lev, is_cross))
        return {"status": "ok"}

    def market_open(self, coin, is_buy, sz, slippage=0.05, cloid=None):
        self.calls.append(("open", coin, sz, str(cloid)))
        filled = round(sz * self.fill_ratio, 8)
        px = self.info.mids[coin] * 1.0005
        oid = self._next()
        self.info.positions[coin] = self.info.positions.get(coin, 0) + filled
        self.info.fills.append({"oid": oid, "coin": coin, "side": "B", "sz": str(filled), "px": str(px), "fee": "0.004", "time": 0})
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(filled), "avgPx": str(px), "oid": oid}}]}}}

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False, cloid=None):
        self.calls.append(("trigger", coin, sz, px, order_type["trigger"]["tpsl"], reduce_only))
        if self.fail_trigger:
            return {"status": "err", "response": "rejected"}
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": self._next()}}]}}}

    def market_close(self, coin, sz=None, slippage=0.05, cloid=None):
        self.calls.append(("close", coin, sz))
        px = self.info.mids[coin] * 0.9995
        oid = self._next()
        self.info.positions.pop(coin, None)
        self.info.fills.append({"oid": oid, "coin": coin, "side": "A", "sz": str(sz), "px": str(px), "fee": "0.004", "time": 0})
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": oid}}]}}}

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return {"status": "ok"}


@pytest.fixture
def live(tmp_path):
    ledger = Ledger(str(tmp_path / "live.db"), 0.0)
    ledger.move_cash("deposit", 20, "sync")
    info = FakeInfo()
    ex = FakeExchange(info)
    return ledger, info, ex, hl_live.HyperliquidLiveBroker(ledger, exchange=ex, info=info, address=ADDR)


def buy(br, order_id="o1", usd=12.0, coin="BTC"):
    return br.buy(order_id, f"hl:{coin}", coin, usd, strategy="mean_reversion", stop_price=76000.0, tp_price=84000.0)


def test_buy_is_1x_isolated_rounded_and_protected_on_exchange(live):
    ledger, info, ex, br = live
    fill = buy(br)
    assert ("leverage", "BTC", 1, False) in ex.calls                       # 1x isolated
    open_call = next(c for c in ex.calls if c[0] == "open")
    assert open_call[2] == 0.00015                                         # 12/80000 floored to 5 decimals
    triggers = [c for c in ex.calls if c[0] == "trigger"]
    assert {t[4] for t in triggers} == {"sl", "tp"} and all(t[5] for t in triggers)   # reduce-only SL + TP
    assert {t[3] for t in triggers} == {76000.0, 84000.0}
    meta = json.loads(fill["meta"])
    assert meta["sl_oid"] and meta["tp_oid"] and fill["fee_usd"] == pytest.approx(0.004)
    assert fill["qty"] == 0.00015 and fill["venue"] == "hyperliquid"


def test_partial_fill_records_what_actually_filled(live):
    ledger, info, ex, br = live
    ex.fill_ratio = 0.5
    fill = buy(br, usd=20.0)
    assert fill["qty"] == pytest.approx(0.000125)
    assert next(c for c in ex.calls if c[0] == "trigger")[2] == pytest.approx(0.000125)   # stops sized to the fill


def test_below_hyperliquid_minimum_is_refused(live):
    ledger, info, ex, br = live
    with pytest.raises(RuntimeError, match="minimum"):
        buy(br, usd=8.0)
    assert not [c for c in ex.calls if c[0] == "open"] and ledger.fills() == []


def test_idempotent_never_submits_twice(live):
    ledger, info, ex, br = live
    f1 = buy(br)
    n = len(ex.calls)
    assert buy(br) == f1 and len(ex.calls) == n                            # ledger hit: no exchange call
    info.known_cloids.add(str(hl_live.cloid_for("o2")))                    # crashed after submitting o2
    with pytest.raises(RuntimeError, match="already exists"):
        buy(br, order_id="o2", coin="SOL")
    assert not [c for c in ex.calls[n:] if c[0] == "open"]


def test_unprotected_position_is_closed_immediately(live):
    ledger, info, ex, br = live
    ex.fail_trigger = True
    with pytest.raises(RuntimeError, match="protection"):
        buy(br)
    assert ex.calls[-1][0] == "close" and "BTC" not in info.positions and ledger.fills() == []


def test_exit_closes_and_cancels_exchange_stops(live):
    ledger, info, ex, br = live
    entry = buy(br)
    sell = br.sell("exit-o1", "hl:BTC", "BTC", entry["qty"], strategy="mean_reversion", meta={"reason": "tp"})
    meta = json.loads(entry["meta"])
    cancels = {c[2] for c in ex.calls if c[0] == "cancel"}
    assert cancels == {meta["sl_oid"], meta["tp_oid"]} and sell["side"] == "sell"
    assert ledger.positions_from_fills() == {}


def test_sync_records_native_stop_and_flags_untracked(live):
    ledger, info, ex, br = live
    from src.solana.engine import PaperEngine
    entry = buy(br)
    ledger.upsert_position({**ledger.positions_from_fills()["hl:BTC"], "mark_price": None, "mark_value": None})
    # the exchange's stop-loss fired while we were away
    info.positions.pop("BTC")
    info.fills.append({"oid": 999, "coin": "BTC", "side": "A", "sz": str(entry["qty"]), "px": "76000", "fee": "0.004", "time": 10**15})
    assert br.sync() == []
    t = ledger.closed_trades()[0]
    assert t["exit_price"] == pytest.approx(76000) and "exchange" in t["reason"]
    info.positions["SOL"] = 0.5                                            # someone traded by hand
    assert any("not in the ledger" in p for p in br.sync())


def test_engine_trips_kill_switch_on_exchange_mismatch(live, monkeypatch):
    ledger, info, ex, br = live
    from src.solana.agents import DebateAgent, ExecutorAgent
    from src.solana.engine import PaperEngine
    info.positions["SOL"] = 1.0
    eng = PaperEngine(ledger, [], hl_live._NoSafety(), [], DebateAgent(enabled=False), RiskAgent(),
                      ExecutorAgent(br), candles_fn=lambda c: [], profiles={})
    eng.run_cycle()
    assert eng.kill_switch()["active"] and "mismatch" in eng.kill_switch()["reason"]


def test_risk_hard_usd_caps():
    r = RiskAgent(max_pos_pct=1.0, max_exposure_pct=1.0, min_trade_usd=10, max_position_usd=12, max_capital_usd=20)
    p = Portfolio(equity=1000, cash=1000, exposure=0, open_mints=set(), strategy_exposure={}, day_start_equity=1000,
                  kill_switch=False)
    v = r.evaluate(Signal("m", 1, 0.05, 0.1, "x"), "hl:BTC", 1.0, p)
    assert v.approve and v.detail["size_usd"] == 12                       # never more than $12, even with $1000


def test_arming_requires_configuration(monkeypatch):
    monkeypatch.delenv("HL_API_WALLET_KEY", raising=False)
    monkeypatch.setattr(cfg, "HL_ACCOUNT_ADDRESS", "")
    st = hl_live.config_status()
    assert len(st["problems"]) == 2 and st["api_wallet"] is None
    with pytest.raises(ValueError):
        hl_live.arm()
    monkeypatch.setattr(cfg, "HL_NETWORK", "mainnet")
    monkeypatch.setattr(cfg, "LIVE_TRADING_ALLOWED", False)
    assert any("LIVE_TRADING=1" in p for p in hl_live.config_status()["problems"])


def test_test_orders_only_on_testnet(monkeypatch):
    monkeypatch.setattr(cfg, "HL_NETWORK", "mainnet")
    with pytest.raises(ValueError, match="testnet"):
        hl_live.test_order()


def test_real_account_simulation_mirrors_live_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "SHADOW_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setattr(hl_live, "_shadow", None)
    eng = hl_live.get_shadow_engine()
    assert eng.ledger.cash() == cfg.SHADOW_START_USDC == 20.0
    assert eng.risk.max_position_usd == cfg.LIVE_MAX_POSITION_USD and eng.risk.max_capital_usd == cfg.LIVE_MAX_CAPITAL_USD
    assert eng.risk.min_trade_usd == hl_live.MIN_ORDER_USD
    assert type(eng.executor._b("hyperliquid")).__name__ == "HyperliquidPaperBroker"   # fake money, never the live broker
    assert eng.ledger.get_state("running") is True

    class LiveStub:
        llm = type("L", (), {"model": "claude-sonnet-5-5"})()
        def decision_mode(self):
            return "llm"
    monkeypatch.setattr(hl_live, "get_live_engine", lambda: LiveStub())
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(eng, "run_cycle", lambda: {"ok": True})
    hl_live.shadow_cycle()
    assert eng.decision_mode() == "rules" and eng.llm.model == "claude-sonnet-5-5"   # no key → falls back to rules
