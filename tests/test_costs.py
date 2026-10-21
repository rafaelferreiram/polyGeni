"""AI must not cost more than it earns: real cost accounting, monthly budget, prefilter, auto-pause."""
import pytest

from src.solana import config as cfg
from src.solana import costs
from src.solana.llm_trader import LLMDecider
from tests.conftest import flat_candles


@pytest.fixture
def book(tmp_path, monkeypatch):
    b = costs.CostBook(str(tmp_path / "costs.db"))
    monkeypatch.setattr(costs, "_book", b)
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-key")
    return b


def test_prices_match_published_rates():
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert costs.price("claude-haiku-5-5", u) == pytest.approx(0.60)
    assert costs.price("claude-sonnet-5-5", u) == pytest.approx(12.0)
    assert costs.price("claude-opus-5-5", u) == pytest.approx(24.0)
    assert costs.price("claude-fable-5-1", u) == pytest.approx(60.0)
    assert costs.price("claude-haiku-5-5", {"searches": 100}) == pytest.approx(1.0)       # $10 / 1000 searches


def test_budget_blocks_calls_once_spent(book, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_MONTHLY_BUDGET_USD", 1.0)
    book.record("claude-sonnet-5-5", "x", {"input_tokens": 100_000, "output_tokens": 100_000})   # $1.20
    with pytest.raises(costs.BudgetExceeded):
        costs.check_budget()
    from src.solana.agents import anthropic_llm
    with pytest.raises(costs.BudgetExceeded):
        anthropic_llm("hi", model="claude-haiku-5-5")                         # never reaches the API


def test_prefilter_skips_paid_call_without_a_setup(book, monkeypatch):
    monkeypatch.setattr(cfg, "LLM_PREFILTER", True)
    calls = []
    d = LLMDecider(model="claude-haiku-5-5", llm_fn=lambda p, s: calls.append(1) or '{"action":"BUY"}')
    assert d.decide(flat_candles(260), {"symbol": "BTC"}) is None
    assert calls == [] and d.last["BTC"]["free"]


def test_unprofitable_ai_bot_is_paused(tmp_path, book, monkeypatch):
    from src.solana import arena
    from tests.test_arena import make_bots
    monkeypatch.setattr(cfg, "LLM_PREFILTER", False)
    bots, _ = make_bots(tmp_path, {"claude-sonnet-5-5": '{"action":"HOLD","reason":"meh"}'})
    monkeypatch.setattr(arena, "_bots", bots)
    book.record("claude-sonnet-5-5", "llm:claude-sonnet-5-5", {"input_tokens": 1_000_000})       # $2 spent, $0 earned
    arena.run_round()
    row = next(r for r in arena.leaderboard() if r["model"] == "claude-sonnet-5-5")
    assert not row["running"] and "custa mais" in row["state"]


def test_net_profit_subtracts_ai_and_server(ledger, book, monkeypatch):
    from src.api.paper_routes import profit
    from tests.test_engine import Price, build
    monkeypatch.setattr(cfg, "VPS_MONTHLY_USD", 0.0)
    eng = build(ledger, Price(1.0))
    ledger.record_equity(0, 100.0, 0.0)
    ledger.record_equity(1, 103.0, 0.0)                     # +$3 trading
    book.record("claude-haiku-5-5", "x", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})   # $0.60
    eng.ledger.move_cash("deposit", 0.000001)               # contributed stays ~100
    p = profit(eng)
    assert p["ai_all"] == pytest.approx(0.60) and p["net_all"] == pytest.approx(p["trading_all"] - 0.60)
