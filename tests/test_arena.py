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


def test_arena_needs_api_key(monkeypatch):
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ValueError):
        arena.set_running(True)
