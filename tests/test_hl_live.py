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
        return {"assetPositions": [{"position": {"coin": c, "szi": str(s), "entryPx": str(self.mids[c])}}
                                   for c, s in self.positions.items()],
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
        px = self.info.mids[coin] * (1.0005 if is_buy else 0.9995)
        oid = self._next()
        self.info.positions[coin] = self.info.positions.get(coin, 0) + (filled if is_buy else -filled)
        self.info.fills.append({"oid": oid, "coin": coin, "side": "B" if is_buy else "A", "sz": str(filled), "px": str(px), "fee": "0.004", "time": 0})
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(filled), "avgPx": str(px), "oid": oid}}]}}}

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False, cloid=None):
        self.calls.append(("trigger", coin, sz, px, order_type["trigger"]["tpsl"], reduce_only, is_buy))
        if self.fail_trigger:
            return {"status": "err", "response": "rejected"}
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": self._next()}}]}}}

    def market_close(self, coin, sz=None, slippage=0.05, cloid=None):
        self.calls.append(("close", coin, sz))
        was_short = self.info.positions.get(coin, 0) < 0
        px = self.info.mids[coin] * (1.0005 if was_short else 0.9995)
        oid = self._next()
        self.info.positions.pop(coin, None)
        self.info.fills.append({"oid": oid, "coin": coin, "side": "B" if was_short else "A", "sz": str(sz), "px": str(px), "fee": "0.004", "time": 0})
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
    assert ("leverage", "BTC", int(cfg.LIVE_LEVERAGE), False) in ex.calls   # isolated, LIVE_LEVERAGE
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


def test_sync_adopts_an_orphan_with_a_pending_intent_instead_of_kill_switch(live):
    """HYPE, 2026-10-21 23:05 UTC: the app died between the exchange fill and the ledger write and the kill switch
    looped until a human adopted the position. With the intent journaled before submission, sync adopts it alone:
    fill recorded from the exchange's numbers, stop/TP re-placed, no problem reported."""
    from src.solana.ledger import now_iso
    ledger, info, ex, br = live
    ledger.set_state("pending:o9", dict(coin="BTC", mint="hl:BTC", symbol="BTC", strategy="trend", usd=12.0,
                                        stop_price=76000.0, tp_price=84000.0, short=False, ts=now_iso(), meta={}))
    info.positions["BTC"] = 0.00015                     # the order filled on the exchange; the ledger never heard of it
    assert br.sync() == []                              # adopted, not a problem
    fill = ledger.get_fill("o9")
    assert fill and fill["side"] == "buy" and fill["qty"] == 0.00015 and json.loads(fill["meta"])["adopted"]
    assert fill["price"] == pytest.approx(80000.0)      # the exchange's entry price
    triggers = [c for c in ex.calls if c[0] == "trigger"]
    assert {t[4] for t in triggers} == {"sl", "tp"}     # protection resting again
    assert ledger.get_state("sl:o9") and ledger.get_state("tp:o9")
    assert ledger.states("pending:") == {}              # journal consumed
    assert "hl:BTC" in ledger.positions_from_fills()    # reconcile will materialize it


def test_stale_pending_intents_are_swept_and_fresh_ones_kept(live):
    """A rejected/unfilled order leaves an intent with no position: swept after an hour, never adopted."""
    from datetime import datetime, timedelta, timezone
    ledger, info, ex, br = live
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger.set_state("pending:old", dict(coin="SOL", mint="hl:SOL", short=False, ts=old))
    ledger.set_state("pending:new", dict(coin="SOL", mint="hl:SOL", short=False, ts=fresh))
    assert br.sync() == []
    assert list(ledger.states("pending:")) == ["pending:new"]   # old swept, fresh kept for its order to land


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
    for var in ("HL_API_WALLET_KEY", "HL_API_WALLET_KEY_TESTNET", "HL_API_WALLET_KEY_MAINNET"):
        monkeypatch.delenv(var, raising=False)
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
        def decision_mode(self):
            return "llm"
    monkeypatch.setattr(hl_live, "get_live_engine", lambda: LiveStub())
    monkeypatch.setattr(eng, "run_cycle", lambda: {"ok": True})
    hl_live.shadow_cycle()
    assert eng.decision_mode() == "llm"                                    # mirrors the real account
    from src.solana.strategies import LAB_ONLY_BOTS
    assert [m.model for m in eng.team] == [m for m in cfg.ARENA_MODELS if m not in LAB_ONLY_BOTS]   # same team as the real wallet (no shorts)


def test_unified_account_equity_comes_from_spot(live):
    ledger, info, ex, br = live
    info.query_user_abstraction_state = lambda user: "unifiedAccount"
    info.spot_user_state = lambda user: {"balances": [{"coin": "USDC", "total": "18.4"}, {"coin": "HYPE", "total": "3"}]}
    assert br.account_value() == pytest.approx(18.4)          # perp clearinghouse shows 0 in unified mode
    info.query_user_abstraction_state = lambda user: "disabled"
    assert br.account_value() == pytest.approx(20.0)          # standard mode: perp account value


def test_daily_report_counts_wins_losses_and_net(live, tmp_path, monkeypatch):
    from src.solana import costs
    ledger, info, ex, br = live
    monkeypatch.setattr(costs, "_book", costs.CostBook(str(tmp_path / "c.db")))
    eng = type("E", (), {"ledger": ledger})()
    for i, exit_px in enumerate((90000.0, 76000.0)):                    # one win, one loss
        buy(br, order_id=f"o{i}")
        info.mids["BTC"] = exit_px
        br.sell(f"exit-o{i}", "hl:BTC", "BTC", ledger.positions_from_fills()["hl:BTC"]["qty"], strategy="rules", meta={"reason": "t"})
        info.mids["BTC"] = 80000.0
    costs.book().record("claude-haiku-5-5", "x", {"input_tokens": 100_000})          # $0.01
    [today] = hl_live.daily_report(eng, days=1)
    assert today["trades"] == 2 and today["wins"] == 1 and today["losses"] == 1 and today["win_rate"] == 0.5
    assert today["ai_cost_usd"] == pytest.approx(0.01) and today["net_usd"] == pytest.approx(today["pnl_usd"] - 0.01)
    assert today["best"]["pnl_usd"] > 0 > today["worst"]["pnl_usd"]


def test_order_size_rounds_up_one_lot_for_expensive_coins():
    assert hl_live._order_size(12, 1300, 2) == 0.01                 # ZEC: floor 0.00 → one lot of $13 (≤ +20%)
    assert hl_live._order_size(12, 83000, 5) == 0.00014              # BTC: normal floor
    with pytest.raises(RuntimeError):
        hl_live._order_size(12, 9000, 0)                             # one lot = $9,000 → refused


def test_external_deposits_and_withdrawals_become_cash_movements(live):
    ledger, info, ex, br = live
    me = ADDR.lower()
    updates = [{"time": 2_000, "hash": "0xa", "delta": {"type": "deposit", "usdc": "25.0"}},
               {"time": 3_000, "hash": "0xb", "delta": {"type": "send", "user": "0xfriend", "destination": me, "amount": "5", "usdcValue": "5"}},
               {"time": 4_000, "hash": "0xc", "delta": {"type": "accountClassTransfer", "usdc": "3", "toPerp": True}},
               {"time": 5_000, "hash": "0xd", "delta": {"type": "withdraw", "usdc": "10", "fee": "1"}}]
    info.post = lambda path, body: [u for u in updates if u["time"] >= body["startTime"]]
    assert br.record_transfers() == []                        # first run only sets the starting point
    ledger.set_state("transfers_since", 1_000)
    done = br.record_transfers()
    assert [(d["kind"], d["usd"]) for d in done] == [("deposit", 25.0), ("deposit", 5.0), ("withdraw", 10.0)]
    assert ledger.contributed() == 20 + 25 + 5 - 10
    assert br.record_transfers() == []                        # nothing twice


def test_real_account_refuses_short_orders(live):
    ledger, info, ex, br = live
    with pytest.raises(RuntimeError, match="short"):
        br.buy("x", "hl:BTC~short", "BTC", 12, strategy="short", stop_price=1, tp_price=2)


def test_live_snapshot_maps_exchange_state(monkeypatch):
    from src.solana import hl_live

    class Info:
        def all_mids(self):
            return {"BTC": "100000", "@1": "1"}

        def user_state(self, addr, dex=""):
            if dex:
                return {"assetPositions": []}
            return {"assetPositions": [{"position": {"coin": "BTC", "szi": "0.0001", "entryPx": "99000", "positionValue": "10",
                                                     "unrealizedPnl": "0.1", "returnOnEquity": "0.02", "liquidationPx": "50000",
                                                     "leverage": {"value": 2}, "cumFunding": {"sinceOpen": "0.001"}}}]}

        def user_fills(self, addr):
            return [{"time": 1_000_000, "coin": "BTC", "side": "B", "px": "99000", "sz": "0.0001", "fee": "0.004", "closedPnl": "0"}]

    class B:
        address = "0xabc"

        def _connect(self):
            return None, Info()

        def balances(self):
            return {"collateral": 35.0, "margin_used": 5.0, "available": 30.0}

    monkeypatch.setattr(hl_live, "_broker", lambda eng: B())
    monkeypatch.setattr(hl_live, "get_live_engine", lambda: None)
    monkeypatch.setattr(hl_live, "_snap", {})
    d = hl_live.live_snapshot()
    [p] = d["positions"]
    assert p["mark"] == 100000 and p["upnl"] == 0.1 and p["funding"] == -0.001 and d["upnl"] == 0.1
    assert d["fills"][0]["side"] == "compra" and d["available"] == 30.0 and "@1" not in d["prices"]


def test_raised_stop_moves_the_exchange_stop_new_first_then_cancel_old(live):
    ledger, info, ex, br = live
    entry = buy(br)
    meta = json.loads(entry["meta"])
    pos = ledger.positions_from_fills()["hl:BTC"]
    n = len(ex.calls)
    br.move_stop(pos, 80_000.0)
    new_calls = ex.calls[n:]
    assert [c[0] for c in new_calls] == ["trigger", "cancel"]                      # placed first, old one cancelled after
    assert new_calls[0][3] == 80_000.0 and new_calls[0][4] == "sl" and new_calls[0][5] is True and new_calls[1][2] == meta["sl_oid"]
    new_oid = ledger.get_state(f"sl:{entry['order_id']}")
    assert new_oid and new_oid != meta["sl_oid"]
    br.sell("exit-o1", "hl:BTC", "BTC", entry["qty"], strategy="mean_reversion", meta={"reason": "tp"})
    cancels = {c[2] for c in ex.calls if c[0] == "cancel"}
    assert new_oid in cancels and meta["tp_oid"] in cancels                        # the exit cancels the MOVED stop, not the first one


def test_failed_stop_move_keeps_the_old_exchange_stop(live):
    ledger, info, ex, br = live
    entry = buy(br)
    pos = ledger.positions_from_fills()["hl:BTC"]
    ex.fail_trigger = True
    with pytest.raises(RuntimeError, match="trigger rejected"):
        br.move_stop(pos, 80_000.0)
    assert not any(c[0] == "cancel" for c in ex.calls) and ledger.get_state(f"sl:{entry['order_id']}") is None


# ─── real-account shorts (Phase 1: built, OFF by default) ───
def short_buy(br, order_id="s1", usd=12.0, coin="BTC", stop_m=79200.0, tp_m=81600.0):
    """Mirror convention (as in the lab): stop below / target above the entry on the mirror price."""
    return br.buy(order_id, f"hl:{coin}~short", coin, usd, strategy="short_break", stop_price=stop_m, tp_price=tp_m)


def test_real_shorts_are_refused_while_the_switch_is_off(live, monkeypatch):
    ledger, info, ex, br = live
    assert cfg.LIVE_ALLOW_SHORT is False                                  # the default everywhere
    with pytest.raises(RuntimeError, match="short orders are not allowed"):
        short_buy(br)
    assert ex.calls == [] and ledger.fills() == []
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    with pytest.raises(RuntimeError, match="short orders are not allowed"):          # and only the short bots may use it
        br.buy("s2", "hl:BTC~short", "BTC", 12.0, strategy="whales", stop_price=79200.0, tp_price=81600.0)


def test_a_real_short_sells_to_open_protects_with_buy_triggers_and_books_the_mirror_price(live, monkeypatch):
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    ledger, info, ex, br = live
    entry = short_buy(br)
    E = entry["price"]
    assert info.positions["BTC"] < 0 and ex.calls[1][0] == "open" and E == pytest.approx(80000.0 * 0.9995)      # sold on the bid
    sl, tp = [c for c in ex.calls if c[0] == "trigger"]
    assert sl[4] == "sl" and sl[5] is True and sl[6] is True and tp[6] is True                                     # reduce-only BUYs
    assert sl[3] == pytest.approx(round(2 * E - 79200.0, 0), abs=1) and tp[3] == pytest.approx(round(2 * E - 81600.0, 0), abs=1)
    assert sl[3] > E > tp[3]                                                                                      # real stop above, real target below
    assert json.loads(entry["meta"])["short"] is True and entry["price_impact_pct"] >= 0
    pos = ledger.positions_from_fills()["hl:BTC~short"]
    info.mids["BTC"] = 79000.0                                                                                    # price fell 1.25%: the short wins
    value, _ = br.quote_sell_value("hl:BTC~short", pos["qty"])
    assert value > pos["cost_usd"]
    sell = br.sell("exit-s1", "hl:BTC~short", "BTC", pos["qty"], strategy="short_break", meta={"reason": "tp"})
    assert ex.calls[-3][0] == "close" or any(c[0] == "close" for c in ex.calls)
    [t] = ledger.closed_trades()
    assert t["pnl_usd"] > 0.1 and sell["usd_gross"] > entry["usd_gross"]                                          # gained about what the price fell
    cancels = {c[2] for c in ex.calls if c[0] == "cancel"}
    assert len(cancels) == 2                                                                                      # both exchange orders cancelled


def test_a_short_that_loses_books_a_loss_and_the_exchange_stop_closing_it_is_recorded(live, monkeypatch):
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    ledger, info, ex, br = live
    entry = short_buy(br)
    ledger.upsert_position({**ledger.positions_from_fills()["hl:BTC~short"], "mark_price": None, "mark_value": None})
    info.positions.pop("BTC")                                                                                      # squeezed up: the exchange stop (a BUY) fired
    info.fills.append({"oid": 999, "coin": "BTC", "side": "B", "sz": str(entry["qty"]), "px": "80800.0", "fee": "0.004", "time": 10**15})
    assert br.sync() == []                                                                                         # the ledger learns the native exit
    [t] = ledger.closed_trades()
    assert t["pnl_usd"] < 0 and "exchange" in t["reason"]                                                          # bought back higher than it sold: a loss


def test_one_direction_per_coin_and_sync_tells_long_from_short(live, monkeypatch):
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    ledger, info, ex, br = live
    buy(br)                                                                                                       # a long BTC is open
    with pytest.raises(RuntimeError, match="already has an open long"):
        short_buy(br, order_id="s9")
    ledger2 = Ledger(":memory:", 0.0)
    ledger2.move_cash("deposit", 20, "sync")
    info2 = FakeInfo()
    ex2 = FakeExchange(info2)
    br2 = hl_live.HyperliquidLiveBroker(ledger2, exchange=ex2, info=info2, address=ADDR)
    short_buy(br2)
    ledger2.upsert_position({**ledger2.positions_from_fills()["hl:BTC~short"], "mark_price": None, "mark_value": None})
    assert br2.sync() == []                                                                                       # a tracked short is expected
    info2.positions["BTC"] = 0.00015                                                                              # the exchange now says long
    assert any("long on the exchange" in p for p in br2.sync())
    info2.positions["BTC"] = -0.00015
    other = Ledger(":memory:", 0.0)
    br3 = hl_live.HyperliquidLiveBroker(other, exchange=FakeExchange(info2), info=info2, address=ADDR)
    assert any("not in the ledger" in p for p in br3.sync())                                                      # an untracked short is still a problem


def test_a_raised_short_stop_moves_the_real_stop_down_with_a_buy_trigger(live, monkeypatch):
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    ledger, info, ex, br = live
    entry = short_buy(br)
    pos = ledger.positions_from_fills()["hl:BTC~short"]
    n = len(ex.calls)
    br.move_stop(pos, entry["price"] + 100.0)                                                                     # mirror stop +100: the short locks a gain
    trig = [c for c in ex.calls[n:] if c[0] == "trigger"][0]
    assert trig[6] is True and trig[3] == pytest.approx(entry["price"] - 100.0, abs=1)                            # real stop now BELOW the entry


def test_short_gate_enforces_regime_and_the_small_limits(live, monkeypatch):
    ledger, info, ex, br = live
    assert "desligado" in hl_live.short_gate(ledger, False)                                                       # switch off
    monkeypatch.setattr(cfg, "LIVE_ALLOW_SHORT", True)
    assert "acima da média" in hl_live.short_gate(ledger, True)                                                   # only while BTC is weak
    assert hl_live.short_gate(ledger, False) is None
    short_buy(br)
    assert "já há 1 short" in hl_live.short_gate(ledger, False)                                                   # 1 open at a time
    monkeypatch.setattr(cfg, "LIVE_SHORT_MAX_OPEN", 5)
    monkeypatch.setattr(cfg, "LIVE_SHORT_MAX_PER_DAY", 1)
    assert "por dia" in hl_live.short_gate(ledger, False)


def test_429_is_retried_with_waits_and_other_errors_are_not():
    from hyperliquid.utils.error import ClientError

    class Api:
        def __init__(self, failures):
            self.failures, self.calls = failures, []

        def post(self, url_path, payload=None):
            self.calls.append(url_path)
            if len(self.calls) <= self.failures:
                raise ClientError(429, None, "rate limited", None)
            return {"ok": True}

    waits = []
    api = hl_live.with_429_retry(Api(failures=2), sleep=waits.append)
    assert api.post("/info", {"type": "allMids"}) == {"ok": True}
    assert len(api.calls) == 3 and waits == [2.0, 5.0]

    # the waits run out: the last 429 propagates
    waits.clear()
    api = hl_live.with_429_retry(Api(failures=99), sleep=waits.append)
    with pytest.raises(ClientError):
        api.post("/exchange")
    assert waits == list(hl_live.RETRY_WAITS)

    # a non-429 client error (bad request) is never retried
    class Bad:
        def post(self, url_path, payload=None):
            raise ClientError(422, "bad", "bad order", None)

    api = hl_live.with_429_retry(Bad(), sleep=lambda s: pytest.fail("slept on a 422"))
    with pytest.raises(ClientError):
        api.post("/exchange")


def test_native_exit_books_only_this_positions_qty_even_with_other_fills_on_the_coin(live):
    """SOL (24/10/2026): another bot's sell on the same coin matched the closing-fills filter and the long's exit
    was booked at double its value, inflating the ledger's cash by one position size."""
    ledger, info, ex, br = live
    entry = buy(br)
    ledger.upsert_position({**ledger.positions_from_fills()["hl:BTC"], "mark_price": None, "mark_value": None})
    info.positions.pop("BTC")
    qty = float(entry["qty"])
    info.fills.append({"oid": 998, "coin": "BTC", "side": "A", "sz": str(qty), "px": "80000", "fee": "0.004", "time": 10**15})
    info.fills.append({"oid": 999, "coin": "BTC", "side": "A", "sz": str(qty), "px": "80000", "fee": "0.004", "time": 10**15})
    assert br.sync() == []
    exit_fill = [f for f in ledger.fills(10) if f["side"] == "sell"][0]
    assert exit_fill["usd_gross"] == pytest.approx(qty * 80000)
    assert exit_fill["qty"] == pytest.approx(qty)
    assert exit_fill["fee_usd"] == pytest.approx(0.004)
