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
    monkeypatch.setattr(cfg, "LIVE_MIN_LAB_TRADES", 2)
    led = bots["claude-opus-5"].ledger
    for i, px in enumerate((1.1, 1.2)):                      # two winning lab trades for opus, none for haiku
        led.append_fill(dict(order_id=f"b{i}", ts=f"2026-10-0{i + 1}T00:00:00+00:00", venue="hyperliquid", side="buy", mint="hl:BTC",
                             symbol="BTC", strategy="x", qty=10, decimals=0, usd_gross=10, fee_usd=0, price=1, quote_price=1))
        led.append_fill(dict(order_id=f"s{i}", ts=f"2026-10-0{i + 1}T01:00:00+00:00", venue="hyperliquid", side="sell", mint="hl:BTC",
                             symbol="BTC", strategy="x", qty=10, decimals=0, usd_gross=10 * px, fee_usd=0, price=px, quote_price=px))
    team = [TeamMember("rules"), TeamMember("claude-opus-5"), TeamMember("claude-haiku-5-5")]
    assert [m.model for m in arena.eligible_for_real(team)] == ["rules", "claude-opus-5"]
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
    monkeypatch.setattr(cfg, "LIVE_MIN_LAB_TRADES", 4)
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
    assert not g["ok"] and g["missing"] == ["trades"] and g["win_rate"] == 0.667 and g["profit_factor"] == 4.0
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
