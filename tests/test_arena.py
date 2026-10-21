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
