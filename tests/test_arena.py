"""Model arena: one bot per model, separate ledgers, each bot calls its own model."""
import pytest

from src.solana import arena
from src.solana import config as cfg
from src.solana.broker import PaperBroker
from tests.conftest import flat_candles, make_quote
from tests.test_engine import FakeScout


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "LLM_PREFILTER", False)      # these tests use flat candles; prefilter has its own test


def make_bots(tmp_path, answers):
    calls = []
    bots = {}
    for model, answer in answers.items():
        def fn(prompt, system, model=model, answer=answer):
            calls.append(model)
            return answer
        broker = lambda ledger: PaperBroker(ledger, quote_fn=make_quote(1.0), sol_usd_fn=lambda: 100.0, slippage_bps=0, fee_lamports=0)
        eng = arena.build_bot(model, db_dir=str(tmp_path), llm_fn=fn, scout=FakeScout(), broker_factory=broker,
                              candles_fn=lambda pool: flat_candles(60))
        eng.profiles = {}
        eng.ledger.set_state("running", True)
        bots[model] = eng
    return bots, calls


def test_each_model_trades_its_own_book(tmp_path, monkeypatch):
    bots, calls = make_bots(tmp_path, {
        "claude-opus-5-5": '{"action":"BUY","stop_pct":0.05,"tp_pct":0.1,"reason":"bounce"}',
        "claude-haiku-5-5": '{"action":"HOLD","reason":"no edge"}',
    })
    monkeypatch.setattr(arena, "_bots", bots)
    arena.run_round()
    assert sorted(set(calls)) == ["claude-haiku-5-5", "claude-opus-5-5"]
    opus, haiku = bots["claude-opus-5-5"].ledger, bots["claude-haiku-5-5"].ledger
    assert opus.path != haiku.path
    assert opus.fills()[0]["strategy"] == "llm:claude-opus-5-5" and haiku.fills() == []
    board = arena.leaderboard()
    assert {r["model"] for r in board} == set(bots) and all(r["equity"] > 0 for r in board)
    assert next(r for r in board if r["model"] == "claude-opus-5-5")["open"]


def test_without_api_key_only_rules_bot_works(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    bots, calls = make_bots(tmp_path, {"claude-opus-5-5": '{"action":"BUY","stop_pct":0.05,"tp_pct":0.1}'})
    rules = arena.build_bot("rules", db_dir=str(tmp_path), scout=__import__("tests.test_engine", fromlist=["FakeScout"]).FakeScout(),
                            broker_factory=lambda l: PaperBroker(l, quote_fn=make_quote(1.0), sol_usd_fn=lambda: 100.0),
                            candles_fn=lambda pool: flat_candles(60))
    rules.ledger.set_state("running", True)
    bots["rules"] = rules
    monkeypatch.setattr(arena, "_bots", bots)
    out = arena.run_round()
    assert "rules" in out and "claude-opus-5-5" not in out and calls == []
    row = next(r for r in arena.leaderboard() if r["model"] == "claude-opus-5-5")
    assert row["needs_key"] and row["state"] == "sem chave API"


def test_coordinator_ranks_by_lab_results(tmp_path, monkeypatch):
    from src.solana.llm_trader import TeamMember
    bots, _ = make_bots(tmp_path, {"claude-opus-5": "{}", "claude-sonnet-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    # sonnet made money on SOL in the lab, opus lost
    for model, exit_px in (("claude-sonnet-5-5", 1.2), ("claude-opus-5", 0.8)):
        led = bots[model].ledger
        led.append_fill(dict(order_id=f"{model}-b", ts="2026-10-01T00:00:00+00:00", venue="hyperliquid", side="buy", mint="hl:SOL",
                             symbol="SOL", strategy=model, qty=10, decimals=0, usd_gross=10, fee_usd=0, price=1, quote_price=1))
        led.append_fill(dict(order_id=f"{model}-s", ts="2026-10-02T00:00:00+00:00", venue="hyperliquid", side="sell", mint="hl:SOL",
                             symbol="SOL", strategy=model, qty=10, decimals=0, usd_gross=10 * exit_px, fee_usd=0, price=exit_px, quote_price=exit_px))
    team = [TeamMember("claude-opus-5"), TeamMember("claude-sonnet-5-5")]
    assert [m.model for m in arena.rank("SOL", team)] == ["claude-sonnet-5-5", "claude-opus-5"]


def test_real_wallet_only_trusts_bots_with_lab_evidence(tmp_path, monkeypatch):
    from src.solana.llm_trader import TeamMember
    bots, _ = make_bots(tmp_path, {"claude-opus-5": "{}", "claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    monkeypatch.setattr(cfg, "GREEN_LIGHT_WINS", 2); monkeypatch.setattr(cfg, "GREEN_LIGHT_TRADES", 2)
    led = bots["claude-opus-5"].ledger
    for i, px in enumerate((1.1, 1.2)):                      # two winning lab trades for opus, none for haiku
        led.append_fill(dict(order_id=f"b{i}", ts=f"2026-10-0{i + 1}T00:00:00+00:00", venue="hyperliquid", side="buy", mint="hl:BTC",
                             symbol="BTC", strategy="x", qty=10, decimals=0, usd_gross=10, fee_usd=0, price=1, quote_price=1))
        led.append_fill(dict(order_id=f"s{i}", ts=f"2026-10-0{i + 1}T01:00:00+00:00", venue="hyperliquid", side="sell", mint="hl:BTC",
                             symbol="BTC", strategy="x", qty=10, decimals=0, usd_gross=10 * px, fee_usd=0, price=px, quote_price=px))
    team = [TeamMember("rules"), TeamMember("claude-opus-5"), TeamMember("claude-haiku-5-5")]
    assert [m.model for m in arena.eligible_for_real(team)] == ["rules", "claude-opus-5"]
    monkeypatch.setattr(cfg, "MAX_TITULARES", 1)          # only the best plays: opus has the lab wins
    assert [m.model for m in arena.eligible_for_real(team)] == ["claude-opus-5"]
    monkeypatch.setattr(cfg, "MAX_TITULARES", 6)
    # trusted rule bots trade real money from day one; untrusted ones need lab evidence
    assert "trend" in [m.model for m in arena.eligible_for_real([TeamMember("trend")])]
    monkeypatch.setattr(cfg, "REAL_TRUSTED_BOTS", ["rules"])
    assert "trend" not in [m.model for m in arena.eligible_for_real([TeamMember("trend")])]
    # the Coordinator follows RECENT results: those 2026-10-0x trades are older than a 1-day window
    monkeypatch.setattr(cfg, "LAB_SCORE_DAYS", 1)
    assert arena.score("claude-opus-5", "BTC", 1) == (0.0, 0) and arena.score("claude-opus-5", "BTC")[1] == 2


def test_trend_strong_needs_a_rising_trend():
    from src.solana.strategies import TrendStrong
    flat = [dict(t=i, open=100, high=100.5, low=99.5, close=100.0, volume=1) for i in range(300)]
    flat[-1] = dict(t=299, open=100, high=102, low=100, close=102.0, volume=1)          # breakout without a trend
    assert TrendStrong().signal(flat) is None
    up = [dict(t=i, open=100 * 1.002 ** i, high=100 * 1.002 ** i * 1.002, low=100 * 1.002 ** i * 0.998,
               close=100 * 1.002 ** i, volume=1) for i in range(300)]
    up[-1] = dict(up[-1], close=up[-1]["close"] * 1.01, high=up[-1]["close"] * 1.012)  # fresh breakout in a strong trend
    assert TrendStrong().signal(up) is not None


def test_reward_points_priority_and_size_penalty(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    bots, _ = make_bots(tmp_path, {"claude-opus-5": "{}", "claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    def trades(model, prices):
        led = bots[model].ledger
        for i, px in enumerate(prices):
            ts = (datetime.now(timezone.utc) - timedelta(hours=10 - i)).isoformat(timespec="seconds")
            led.append_fill(dict(order_id=f"{model}b{i}", ts=ts, venue="hyperliquid", side="buy", mint="hl:BTC", symbol="BTC",
                                 strategy="x", qty=10, decimals=0, usd_gross=10, fee_usd=0, price=1, quote_price=1))
            led.append_fill(dict(order_id=f"{model}s{i}", ts=ts, venue="hyperliquid", side="sell", mint="hl:BTC", symbol="BTC",
                                 strategy="x", qty=10, decimals=0, usd_gross=10 * px, fee_usd=0, price=px, quote_price=px))
    trades("claude-opus-5", (1.1, 1.1, 1.1))      # 3 wins: +$3
    trades("claude-haiku-5-5", (0.9, 0.9, 0.9))   # 3 losses: −$3
    win, lose = arena.reward("claude-opus-5"), arena.reward("claude-haiku-5-5")
    assert win["points"] > 0 and win["tier"] == "positivo" and win["streak"] == 3 and win["size_usd"] == cfg.LIVE_MAX_POSITION_USD
    assert lose["points"] < 0 and lose["tier"] == "em observação" and lose["size_usd"] == 10
    assert arena.size_for("llm:claude-haiku-5-5") == 10 and arena.size_for("unknown") is None
    from src.solana.llm_trader import TeamMember
    order = [m.model for m in arena.rank("ETH", [TeamMember("claude-haiku-5-5"), TeamMember("claude-opus-5")])]
    assert order == ["claude-opus-5", "claude-haiku-5-5"]          # the winner gets priority
    assert arena.track_record("claude-haiku-5-5")["recent_trades"][0]["pnl_pct"] < 0


def test_model_boost_runs_n_cycles_then_returns_to_default(tmp_path, monkeypatch):
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    eng = bots["claude-haiku-5-5"]
    with pytest.raises(ValueError, match="titulares"):                # reserves can't pick a model
        arena.boost("claude-haiku-5-5", "claude-sonnet-5-5", 2)
    monkeypatch.setattr(arena, "is_titular", lambda m: True)
    arena.boost("claude-haiku-5-5", "claude-sonnet-5-5", 2)
    models = []
    for _ in range(3):                                   # one lab round each
        arena._apply_boost("claude-haiku-5-5", eng)
        models.append(eng.llm.model)
    assert models == ["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert eng.ledger.get_state("boost") == {}
    with pytest.raises(ValueError):
        arena.boost("claude-haiku-5-5", "gpt-5", 1)


def test_green_light_needs_trades_profit_win_rate_and_profit_factor(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    monkeypatch.setattr(cfg, "GREEN_LIGHT_WINS", 3); monkeypatch.setattr(cfg, "GREEN_LIGHT_TRADES", 3)
    led = bots["claude-haiku-5-5"].ledger
    def trade(i, px):
        ts = (datetime.now(timezone.utc) - timedelta(hours=20 - i)).isoformat(timespec="seconds")
        led.append_fill(dict(order_id=f"b{i}", ts=ts, venue="hyperliquid", side="buy", mint="hl:BTC", symbol="BTC",
                             strategy="x", qty=10, decimals=0, usd_gross=10, fee_usd=0, price=1, quote_price=1))
        led.append_fill(dict(order_id=f"s{i}", ts=ts, venue="hyperliquid", side="sell", mint="hl:BTC", symbol="BTC",
                             strategy="x", qty=10, decimals=0, usd_gross=10 * px, fee_usd=0, price=px, quote_price=px))
    for i, px in enumerate((1.1, 0.95, 1.1)):
        trade(i, px)
    g = arena.green_light("claude-haiku-5-5")
    assert not g["ok"] and g["missing"] == ["vitórias"] and g["win_rate"] == 0.667 and g["profit_factor"] == 4.0
    trade(3, 1.05)
    assert arena.green_light("claude-haiku-5-5")["ok"]
    assert arena.green_light("trend") == {"ok": True, "trusted": True}


def test_ai_is_asked_only_when_a_setup_exists():
    from src.solana.llm_trader import _worth_asking
    flat = [{"t": 3600 * i, "open": 100, "high": 100.1, "low": 99.9, "close": 100, "volume": 1} for i in range(300)]
    up = {"uptrend": True, "rsi14": 60, "below_lower_band": False}
    assert not _worth_asking(up, flat, {})
    assert _worth_asking({**up, "rsi14": 35}, flat, {})                                          # dip in uptrend
    assert _worth_asking(up, flat, {"whale_flow": {"pressure": "comprando"}})                     # whales buying
    assert _worth_asking({"uptrend": False}, flat, {"top_traders": {"long": 3, "new_longs_1h": 1}})  # top traders entering


def test_training_boost_doubles_lab_slots_then_expires(tmp_path, monkeypatch):
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    eng = bots["claude-haiku-5-5"]
    arena.boost_training("claude-haiku-5-5", 24)
    assert eng.risk.max_positions == cfg.TRAIN_BOOST_MAX_POSITIONS and eng.risk.max_entries_per_day > 1000
    eng.ledger.set_state("train_boost_until", "2000-01-01T00:00:00+00:00")
    arena._apply_training_boost(eng)
    assert eng.risk.max_positions == cfg.LAB_MAX_POSITIONS


def test_ai_full_scan_turns_off_the_prefilter_for_a_while(tmp_path, monkeypatch):
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    eng = bots["claude-haiku-5-5"]
    assert not eng.llm.full_scan
    arena.ai_full_scan("claude-haiku-5-5", 24)
    assert eng.llm.full_scan
    eng.ledger.set_state("ai_full_scan_until", "2000-01-01T00:00:00+00:00")
    arena._apply_training_boost(eng)
    assert not eng.llm.full_scan


def test_short_bot_is_lab_only():
    from src.solana import hl_live
    from src.solana.strategies import SHORT_BOTS
    assert "short" in SHORT_BOTS and all(m.model not in SHORT_BOTS for m in hl_live.team())
    g = arena.green_light("short")
    assert not g["ok"] and "conta real não faz short" in g["missing"]


def test_fifteen_minute_twins_can_earn_real_money_and_are_named_for_their_exits():
    from src.solana import hl_live
    from src.solana.engine import _max_hold
    from src.solana.strategies import FAST_BOTS, RULE_BOTS
    for m in FAST_BOTS:
        s = RULE_BOTS[m]()
        assert s.name == m and s.max_hold_h == 6 and _max_hold(m, 72) == 2   # the 2h cap (MAX_HOLD_CAP_H) applies to every bot
        g = arena.green_light(m)
        assert "versão de 15 min: só laboratório" not in g.get("missing", [])     # same green light as everyone
    assert {"whales_15m", "copy_15m"} <= {t.model for t in hl_live.team()}


def test_turbo_runs_the_bot_every_minute_until_midnight(tmp_path, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    sch = BackgroundScheduler()                     # not started: only checks what gets scheduled
    monkeypatch.setattr(arena, "paper_scheduler", lambda: sch)
    r = arena.turbo("claude-haiku-5-5")
    job = sch.get_job("turbo_claude-haiku-5-5")
    assert job and job.trigger.interval.total_seconds() == cfg.TURBO_CYCLE_SEC
    eng = bots["claude-haiku-5-5"]
    assert eng.ledger.get_state("train_boost_until") >= r["until"][:13]
    assert r["until"].endswith(":00:00+00:00")      # Lisbon midnight
    from src.solana.engine import _max_hold
    assert _max_hold("claude-haiku-5-5", 48) == 2 and arena.fast_exit_hold("claude-haiku-5-5") is None   # no turbo exit; only the 2h cap


def test_titular_rule_bot_gets_its_reviews_by_the_chosen_model_and_reserves_one_coffee_a_day(tmp_path, monkeypatch):
    from src.solana.agents import DebateAgent
    from tests.conftest import candidate
    from src.solana.strategies import Signal
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    bots["trend"] = bots.pop("claude-haiku-5-5")            # reuse the engine as a rule bot's lab engine
    bots["trend"].llm = None
    monkeypatch.setattr(arena, "_bots", bots)
    monkeypatch.setattr(arena, "is_titular", lambda m: m == "trend")
    arena.boost("trend", "claude-opus-5-5", 3)
    assert arena.reviewer_model("trend") == "claude-opus-5-5" and arena.reviewer_model("whales") is None
    seen = []
    d = DebateAgent(llm_fn=lambda prompt, model=None: seen.append(model) or '{"veto": false}', enabled=True,
                    model_for=arena.reviewer_model)
    d.review(candidate(), Signal("trend", 1.0, 0.05, 0.1, "x"), {})
    assert seen == ["claude-opus-5-5"]
    monkeypatch.setattr(arena, "is_titular", lambda m: False)  # now a reserve: one coffee per day, lab only
    assert arena.coffee("trend")["accounts"] == []
    with pytest.raises(ValueError, match="1 café por dia"):
        arena.coffee("trend")


def test_evolution_retires_a_weak_reserve_and_breeds_an_improved_champion(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src.solana import evolution, hl_live, learning
    monkeypatch.setattr(evolution, "PATH", str(tmp_path / "lineage.json"))
    monkeypatch.setattr(cfg, "ARENA_MODELS", ["trend4h"])
    monkeypatch.setattr(arena, "build_bot", lambda m, **kw: _fresh(tmp_path, m))
    monkeypatch.setattr(evolution, "_refresh_accounts", lambda: None)
    monkeypatch.setattr(arena, "_bots", None)
    monkeypatch.setattr(arena, "is_titular", lambda m: False)
    eng = arena.bots()["trend4h"]
    born = datetime.now(timezone.utc) - timedelta(hours=21)
    eng.ledger.set_state("born_at", born.isoformat(timespec="seconds"))
    t = lambda h, pnl, why: {"pnl_usd": pnl, "reason": why, "strategy": "trend4h", "order_id": f"exit-o{h}",
                             "entry_ts": (born + timedelta(hours=h - 0.5)).isoformat(), "exit_ts": (born + timedelta(hours=h)).isoformat()}
    trades = [t(h, -0.1, "max holding time 2h reached") for h in (1, 2, 3)] + [t(4, 0.1, "take-profit hit")]
    monkeypatch.setattr(type(eng.ledger), "closed_trades", lambda self: trades if self is eng.ledger else [])
    [ev] = evolution.check()
    assert ev["retired"] == "trend4h" and ev["label"] == evolution.CHAMPIONS[0] and ev["verdict"]["win_rate"] == 0.25
    data = evolution.load()
    child = data["bots"][ev["born"]]
    assert data["active"] == [ev["born"]] and child["base"] == "trend4h" and child["gen"] == 2
    assert child["params"]["no_fast_exit"] and child["params"]["max_hold_h"] >= 12      # learned from losing time exits
    assert ev["born"] in arena.bots() and "trend4h" not in arena.bots()
    from src.solana.strategies import RULE_BOTS
    assert RULE_BOTS[ev["born"]]().name == ev["born"]
    assert evolution.check() == []                   # the newborn has its own 20h before its first cut


def _fresh(tmp_path, model):
    from src.solana.ledger import Ledger
    from src.solana.engine import PaperEngine
    from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, SafetyAgent
    from src.solana.broker import PaperBroker
    led = Ledger(str(tmp_path / f"{model}.db"), 100.0)
    return PaperEngine(led, [], SafetyAgent(), [], DebateAgent(enabled=False), RiskAgent(), ExecutorAgent(PaperBroker(led)))


def test_user_can_promote_a_reserve_with_12_wins_and_35pct(tmp_path, monkeypatch):
    from src.solana import evolution
    monkeypatch.setattr(evolution, "PATH", str(tmp_path / "lineage.json"))
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    led = bots["claude-haiku-5-5"].ledger
    fake = [{"pnl_usd": 0.1 if i < 12 else -0.1} for i in range(30)]          # 12 wins of 30 = 40%
    monkeypatch.setattr(type(led), "closed_trades", lambda self: fake)
    monkeypatch.setattr(arena, "significance", lambda m: {"p": 0.01})       # the luck test has its own tests
    assert arena.can_promote("claude-haiku-5-5")["ok"]
    arena.promote("claude-haiku-5-5")
    assert arena.is_titular("claude-haiku-5-5") and arena.green_light("claude-haiku-5-5").get("promoted")
    assert not arena.can_promote("claude-haiku-5-5")["ok"]                   # already titular
    fake[:] = [{"pnl_usd": 0.1}] * 11
    monkeypatch.setattr(evolution, "load", lambda: {"active": [], "bots": {}, "promoted": []})
    with pytest.raises(ValueError, match="12 vitórias"):
        arena.promote("claude-haiku-5-5")


def test_advisor_flags_promotion_potential_and_cut_risk(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    monkeypatch.setattr(arena, "is_titular", lambda m: False)
    led = bots["claude-haiku-5-5"].ledger
    now = datetime.now(timezone.utc)
    led.set_state("born_at", (now - timedelta(hours=5)).isoformat())
    ts = (now - timedelta(hours=1)).isoformat()
    trades = [{"pnl_usd": 0.3, "exit_ts": ts}] * 16 + [{"pnl_usd": -0.1, "exit_ts": ts}] * 8
    monkeypatch.setattr(type(led), "closed_trades", lambda self: trades)
    monkeypatch.setattr(arena, "net_of_cost", lambda m: (4.0, 0.0, 4.0))
    monkeypatch.setattr(arena, "significance", lambda m: {"p": 0.01})       # the luck test has its own tests
    assert arena.advise("claude-haiku-5-5", now)["flag"] == "promote"
    trades[:] = [{"pnl_usd": 0.05, "exit_ts": ts}] * 13 + [{"pnl_usd": -0.2, "exit_ts": ts}] * 20     # 39%: user's rule, but loses
    monkeypatch.setattr(arena, "net_of_cost", lambda m: (-3.35, 0.0, -3.35))
    a = arena.advise("claude-haiku-5-5", now)
    assert a["flag"] == "cut" and any("não recomendo" in r for r in a["reasons"])


def test_team_network_shares_lessons_and_routes_opportunities(monkeypatch):
    from types import SimpleNamespace
    from src.solana import learning
    from src.solana.strategies import Signal
    monkeypatch.setattr(learning, "CACHE_S", 0)
    monkeypatch.setattr(cfg, "LEARN_MIN_TRADES", 3)
    recs = {
        "a": {"moeda X": {"n": 8, "wins": 4, "pnl": 0.0}, "rompimento 1h": {"n": 4, "wins": 1, "pnl": -0.2}},
        "b": {"moeda X": {"n": 8, "wins": 6, "pnl": 0.6}, "rompimento 1h": {"n": 4, "wins": 4, "pnl": 0.4},
              "RSI alto": {"n": 4, "wins": 0, "pnl": -0.3}},
        "c": {"moeda X": {"n": 8, "wins": 4, "pnl": 0.0}, "RSI alto": {"n": 3, "wins": 0, "pnl": -0.2}},
    }
    bots = {m: SimpleNamespace(ledger=SimpleNamespace(path=m, set_state=lambda *a: None, get_state=lambda k, d=None: d)) for m in recs}
    monkeypatch.setattr(arena, "_bots", bots)
    monkeypatch.setattr(learning, "record", lambda led, strategy=None: recs[led.path])
    # b and c both lost in 'RSI alto' (7 trades, 0 wins): a, who never traded it, avoids it too
    assert learning.team_avoid(recs["a"], learning.team_record(), ["RSI alto", "moeda X"]) == ["RSI alto"]
    # but never a bot's own core move (on most of its trades): c is a "RSI alto" specialist
    core = {"moeda X": {"n": 4, "wins": 0, "pnl": -0.3}, "RSI alto": {"n": 4, "wins": 0, "pnl": -0.3}}
    assert learning.team_avoid(core, learning.team_record(), ["RSI alto"]) == []
    # a sees a breakout but b is clearly better at breakouts: the opportunity goes to b
    mate = learning.better_teammate("a", ["rompimento 1h"])
    assert mate and mate[0] == "b"
    arena.post_handoff("b", "BTC", Signal("a", 100.0, 0.02, 0.03, "rompeu"), "a", mate[1])
    sig = arena.take_handoff("b", "BTC", "b")
    assert sig.strategy == "b" and sig.handed and "recebida de a" in sig.reason
    assert arena.take_handoff("b", "BTC", "b") is None            # used once


def test_bots_talk_in_the_group_chat(tmp_path):
    from src.solana import teamchat
    from src.solana.ledger import Ledger
    led = Ledger(str(tmp_path / "arena_whales.db"), 100.0)
    led.set_state("model", "whales")
    led.log_decision("entry", mint="hl:BTC", symbol="BTC", strategy="whales", reason="baleias compraram $2M em 1h; size $10",
                     signals={"tags": ["alta"]})
    led.log_decision("exit", mint="hl:BTC", symbol="BTC", strategy="whales", reason="take-profit hit (1 >= 1)", signals={"pnl_usd": 0.12})
    led.log_decision("handoff", mint="hl:ETH", symbol="ETH", strategy="whales", reason="📨 passou ETH para trend: vai melhor")
    msgs = teamchat.recent()
    assert [m["kind"] for m in msgs] == ["entry", "exit", "handoff"] and all(m["author"] == "whales" for m in msgs)
    assert "bateu o alvo" in msgs[1]["text"] and "+$0.12" in msgs[1]["text"]
    teamchat.team_lessons([{"tag": "RSI alto", "n": 12, "wins": 2, "pnl": -0.8}])
    teamchat.team_lessons([{"tag": "RSI alto", "n": 12, "wins": 2, "pnl": -0.8}])        # said once
    assert sum(m["kind"] == "network" for m in teamchat.recent()) == 1


def test_short_successor_stays_lab_only(tmp_path, monkeypatch):
    from src.solana import evolution, strategies as st
    monkeypatch.setattr(evolution, "PATH", str(tmp_path / "lineage.json"))
    monkeypatch.setattr(st, "RULE_BOTS", dict(st.RULE_BOTS))
    monkeypatch.setattr(st, "SHORT_BOTS", st.SHORT_BOTS)
    monkeypatch.setattr(arena, "_bots", {})                    # never load the real lab bots in a test
    evolution.register({"active": [], "bots": {"short_g2_1": {"label": "Rony", "base": "short", "params": {}}}})
    try:
        assert "short_g2_1" in st.SHORT_BOTS and "short_g2_1" in st.LAB_ONLY_BOTS
        assert not arena.can_promote("short_g2_1")["ok"]
    finally:
        st.SHORT_BOTS.discard("short_g2_1")
