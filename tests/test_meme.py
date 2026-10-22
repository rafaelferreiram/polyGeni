"""Meme-coin trench: safety gate, signals, paper fills with fees and impact, rug alarm."""
from datetime import datetime, timedelta, timezone

from src.solana import meme
from src.solana.ledger import Ledger


def _token(**kw):
    now = datetime.now(timezone.utc)
    t = {"id": "MINT1", "symbol": "FROG", "holderCount": 900, "liquidity": 80_000, "organicScore": 70, "launchpad": "pump.fun",
         "graduatedAt": (now - timedelta(hours=1)).isoformat(), "firstPool": {"createdAt": (now - timedelta(hours=5)).isoformat()},
         "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 18, "devBalancePercentage": 1},
         "stats1h": {"priceChange": 30, "numBuys": 300, "numSells": 150, "numNetBuyers": 50, "buyOrganicVolume": 40_000},
         "stats5m": {"priceChange": 4}}
    t.update(kw)
    return t


def test_trench_buys_only_safe_tokens_and_runs_from_a_rug(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    for led in meme._ledgers.values():
        led.set_state("model", "x")
    good, bad = _token(), _token(id="MINT2", audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True,
                                                    "topHoldersPercentage": 18, "devBalancePercentage": 1})
    assert all(ok for ok, _ in meme.safety(good).values()) and not meme.safety(bad)["mint/freeze revogados"][0]
    assert meme.signal("meme_grad", good) and meme.signal("meme_momo", good)
    prices = {"MINT1": {"price": 0.01, "liq": 80_000}, "MINT2": {"price": 0.01, "liq": 80_000}}
    meme.enter("meme_grad", [good, bad], prices)
    led = meme.ledger("meme_grad")
    [pos] = led.positions()
    assert pos["mint"] == "MINT1" and abs(pos["cost_usd"] - 10) < 1e-9          # $10 in, fee and impact included
    meme.manage("meme_grad", {"MINT1": {"price": 0.01, "liq": 30_000}})          # liquidity collapsed: rug alarm
    assert led.positions() == [] and "rug" in led.closed_trades()[0]["reason"]
    assert led.closed_trades()[0]["pnl_usd"] < 0                                 # fees + impact on the way out
    meme.enter("meme_grad", [good], prices)
    assert led.positions() == []                                                 # no re-entry right after an exit


def test_trench_target_and_rising_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    meme.enter("meme_momo", [_token()], {"MINT1": {"price": 1.0, "liq": 1_000_000}})
    led = meme.ledger("meme_momo")
    meme.manage("meme_momo", {"MINT1": {"price": 1.35, "liq": 1_000_000}})       # +35%: the lock keeps 80% of the gain (+28%)
    assert led.positions()
    meme.manage("meme_momo", {"MINT1": {"price": 1.30, "liq": 1_000_000}})       # still above the lock: hold
    assert led.positions()
    meme.manage("meme_momo", {"MINT1": {"price": 1.15, "liq": 1_000_000}})       # fell through the lock: banked
    t = led.closed_trades()[0]
    assert "ganho rápido" in t["reason"] and t["pnl_usd"] > 0


def test_whale_flow_rony_and_learning(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    trade = lambda kind, usd, who, mins: {"attributes": {"kind": kind, "volume_in_usd": str(usd), "tx_from_address": who,
                                                          "block_timestamp": (now - timedelta(minutes=mins)).isoformat()}}
    rows = [trade("buy", 5000, "W1", 10), trade("buy", 4000, "W2", 20), trade("sell", 1500, "W3", 5),
            trade("buy", 900_000, "OLD", 90), trade("buy", 500, "SMALL", 1)]               # too old / too small: ignored
    monkeypatch.setattr(meme, "request_json", lambda *a, **k: {"data": rows})
    monkeypatch.setattr(meme, "_hot", {})
    w = meme.whale_flow("MINT1", "FROG", "POOL", 80_000)
    assert (w["buy_1h"], w["sell_1h"], w["net_1h"], w["buyers"]) == (9000, 1500, 7500, 2)
    meme.whale_flow("MINT9", "DOG", "POOL9", 80_000)
    assert {x["wallet"] for x in meme.hot_wallets()} == {"W1", "W2"}                       # whales seen in 2 tokens
    t = _token(whale=w)
    assert meme.signal("meme_whale", t) and "baleias comprando" in meme.situation(t)
    assert not meme.signal("meme_whale", _token(whale={"net_1h": -500, "buyers": 3}))

    # learning: each exit is linked to its buy, so a losing situation is skipped next time
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 1000.0) for b in meme.BOTS})
    monkeypatch.setattr(meme.cfg, "LEARN_MIN_TRADES", 2)
    led = meme.ledger("meme_whale")
    for i in range(3):
        led.set_state(f"pos:M{i}", None)
        meme.enter("meme_whale", [_token(id=f"M{i}", symbol=f"T{i}", whale=w)], {f"M{i}": {"price": 1.0, "liq": 1_000_000}})
        meme.manage("meme_whale", {f"M{i}": {"price": 0.7, "liq": 1_000_000}})            # stopped out
    from src.solana import learning
    learning._cache.clear()
    rec = learning.record(led, "meme_whale")
    assert rec["baleias comprando"]["n"] == 3 and rec["baleias comprando"]["wins"] == 0


def test_meme_reserve_without_profit_is_replaced_by_a_tuned_successor(tmp_path, monkeypatch):
    monkeypatch.setattr(meme.cfg, "MEME_LINEAGE_PATH", str(tmp_path / "meme_lineage.json"))
    monkeypatch.setattr(meme, "ROSTER", {"active": {"meme_grad": {**meme.FOUNDERS["meme_grad"], "base": "meme_grad", "gen": 1, "params": {}}}, "retired": []})
    monkeypatch.setattr(meme, "BOTS", meme.ROSTER["active"])
    monkeypatch.setattr(meme, "_ledgers", {})
    monkeypatch.setattr(meme.teamchat, "post", lambda *a, **k: True)
    monkeypatch.chdir(tmp_path)
    led = meme.ledger("meme_grad")
    born = datetime.now(timezone.utc) - timedelta(hours=13)
    led.set_state("born_at", born.isoformat())
    for i in range(3):                                            # three stop-outs inside the first 12h
        meme.enter("meme_grad", [_token(id=f"M{i}", symbol=f"T{i}")], {f"M{i}": {"price": 1.0, "liq": 1_000_000}})
        meme.manage("meme_grad", {f"M{i}": {"price": 0.7, "liq": 1_000_000}})
    [ev] = meme.evolve(now=datetime.now(timezone.utc) + timedelta(hours=11))   # trades fall inside the judged window
    assert ev["retired"] == "meme_grad" and "meme_grad" not in meme.BOTS
    child = meme.BOTS[ev["born"]]
    assert child["base"] == "meme_grad" and child["gen"] == 2 and child["params"]["stop_pct"] > meme.cfg.MEME_STOP_PCT
    assert meme.signal(ev["born"], _token())                      # same family's rules
    assert meme.evolve(now=datetime.now(timezone.utc) + timedelta(hours=11)) == []   # the newborn is not judged yet


def test_real_meme_account_dry_run_limits(tmp_path, monkeypatch):
    from src.solana import meme_wallet as mw
    monkeypatch.setattr(mw.cfg, "MEME_WALLET_PATH", str(tmp_path / "w.json"))
    monkeypatch.setattr(mw.cfg, "MEME_REAL_DB_PATH", str(tmp_path / "real.db"))
    monkeypatch.setattr(mw, "_kp", None)
    monkeypatch.setattr(mw, "_led", None)
    assert mw.address() and (tmp_path / "w.json").stat().st_mode & 0o777 == 0o600   # key readable only by this user
    monkeypatch.setattr(mw, "mint_info", lambda m: (6, 0))
    monkeypatch.setattr(mw, "sol_usd", lambda: 200.0)
    monkeypatch.setattr(mw, "quote", lambda i, o, a: {"outAmount": str(int(a * 1000)) if o != mw.SOL else str(int(a / 1000)),
                                                       "priceImpactPct": "0.001", "routePlan": []})
    monkeypatch.setattr(mw, "swap_tx", lambda q: b"tx")
    monkeypatch.setattr(mw, "simulate", lambda tx: None)
    monkeypatch.setattr(mw, "send", lambda tx: (_ for _ in ()).throw(AssertionError("dry run must not send")))
    mw.mirror({"meme_grad": {}}, set(), {})                       # first round only primes: no late buys of old positions
    mw.mirror({"meme_grad": {"A": "AAA", "B": "BBB", "C": "CCC"}}, set(), {})
    led = mw.ledger()
    assert {p["mint"] for p in led.positions()} == {"A", "B"}      # 2 positions max
    assert any("2 posições" in d["reason"] for d in led.decisions(20, kind="veto"))
    mw.mirror({"meme_grad": {"B": "BBB"}}, set(), {})               # the lab sold A: the real account sells A
    assert {p["mint"] for p in led.positions()} == {"B"} and len(led.closed_trades()) == 1
    monkeypatch.setattr(mw, "quote", lambda i, o, a: {"outAmount": "0", "priceImpactPct": "0.05", "routePlan": []})
    assert mw.buy("meme_grad", "Z", "ZZZ") is None                 # 5% price impact: no


def test_meme_quick_win_banks_a_sudden_spike_net_of_fees(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    meme.enter("meme_momo", [_token()], {"MINT1": {"price": 1.0, "liq": 1_000_000}})
    led = meme.ledger("meme_momo")
    meme.manage("meme_momo", {"MINT1": {"price": 1.05, "liq": 1_000_000}})       # +5% spike: lock 50% of it (+2.5%)
    assert led.positions()
    meme.manage("meme_momo", {"MINT1": {"price": 1.03, "liq": 1_000_000}})       # still above the lock: hold
    assert led.positions()
    meme.manage("meme_momo", {"MINT1": {"price": 1.02, "liq": 1_000_000}})       # fell through the lock: bank it
    [t] = led.closed_trades()
    assert "ganho rápido" in t["reason"] and t["pnl_usd"] > 0                     # a net gain after 0.5% fees each way
    d = meme.bot_detail("meme_momo")
    assert d["stats"]["exits"]["ganho rápido"]["n"] == 1 and d["trades"][0]["symbol"] == "FROG"


def test_fast_watch_reprices_only_open_positions_and_rony_skips_dumping_whales(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    monkeypatch.setattr(meme, "_mirror", lambda prices: None)
    assert meme.watch() == 0                                                     # nothing open: no price call
    meme.enter("meme_momo", [_token()], {"MINT1": {"price": 1.0, "liq": 1_000_000}})
    asked = []
    monkeypatch.setattr(meme, "pair_prices", lambda mints: asked.append(mints) or {"MINT1": {"price": 0.5, "liq": 1_000_000}})
    assert meme.watch() == 1 and asked == [["MINT1"]]
    assert meme.ledger("meme_momo").positions() == []                            # the −50% gap was caught by the watch
    whales = {"net_1h": 6000, "buy_1h": 10000, "sell_1h": 4000, "buyers": 3}     # sells already 40% of buys: the dump
    assert meme.signal("meme_whale", _token(whale=whales)) is None
    assert meme.signal("meme_whale", _token(whale={**whales, "sell_1h": 1000}))


def test_stop_guard_pauses_a_bot_after_three_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 1000.0) for b in meme.BOTS})
    led = meme.ledger("meme_momo")
    for i in range(3):
        meme.enter("meme_momo", [_token(id=f"M{i}", symbol=f"T{i}")], {f"M{i}": {"price": 1.0, "liq": 1_000_000}})
        meme.manage("meme_momo", {f"M{i}": {"price": 0.7, "liq": 1_000_000}})
    assert len(led.closed_trades()) == 3
    meme.enter("meme_momo", [_token(id="M9", symbol="T9")], {"M9": {"price": 1.0, "liq": 1_000_000}})
    assert led.positions() == [] and any("guarda de stops" in d["reason"] for d in led.decisions(10, kind="veto"))
    assert meme.stop_guard(led, "meme_momo").startswith("pausa até")


def test_safety_must_hold_on_consecutive_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(meme, "_ledgers", {b: Ledger(str(tmp_path / f"arena_{b}.db"), 100.0) for b in meme.BOTS})
    monkeypatch.setattr(meme, "_safe_streak", {})
    monkeypatch.setattr(meme, "discover", lambda: [_token()])
    monkeypatch.setattr(meme, "pair_prices", lambda mints: {"MINT1": {"price": 1.0, "liq": 80_000, "pool": None}})
    monkeypatch.setattr(meme, "_mirror", lambda prices: None)
    monkeypatch.setattr(meme, "evolve", lambda now=None: [])
    monkeypatch.setattr(meme.teamchat, "post", lambda *a, **k: True)
    meme.run_round()
    assert meme.ledger("meme_momo").positions() == []                            # safe once: not yet
    meme.run_round()
    assert meme.ledger("meme_momo").positions()                                  # safe twice in a row: buy
