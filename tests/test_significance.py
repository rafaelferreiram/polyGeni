"""Rule-significance test: a bot's record must beat random entries with the same exits on the same candles."""
import random
from datetime import datetime, timedelta, timezone

from src.solana import arena, significance as sg
from src.solana.ledger import Ledger


def _candles(n=400, seed=1):
    rng = random.Random(seed)
    out, px, t0 = [], 100.0, int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    for i in range(n):
        px *= 1 + rng.uniform(-0.004, 0.004)                     # noise only: no edge to be had from random entries
        out.append({"t": t0 + i * 3600, "open": px, "high": px * 1.003, "low": px * 0.997, "close": px, "volume": 1e6})
    return out


def _book(path, exit_prices):
    led = Ledger(path, 1000.0)
    t0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
    for i, exit_px in enumerate(exit_prices):
        ts = t0 + timedelta(hours=3 * i)
        base = dict(venue="hyperliquid", mint="hl:SOL", symbol="SOL", strategy="trend", decimals=0, quote_price=100.0,
                    price_impact_pct=0.0, slippage_bps=0, route="test", meta="{}")
        led.append_fill({**base, "order_id": f"b{i}", "ts": ts.isoformat(), "side": "buy", "qty": 0.1, "usd_gross": 10.0,
                         "fee_usd": 0.0045, "price": 100.0, "stop_price": 95.0, "tp_price": 103.0})
        led.append_fill({**base, "order_id": f"s{i}", "ts": (ts + timedelta(hours=2)).isoformat(), "side": "sell", "qty": 0.1,
                         "usd_gross": 0.1 * exit_px, "fee_usd": 0.0045, "price": exit_px, "stop_price": None, "tp_price": None})
    return led


def test_skill_beats_random_entries_and_a_flat_record_does_not(tmp_path):
    skilled = sg.p_value(_book(str(tmp_path / "a.db"), [103.0] * 15), candles_fn=lambda coin: _candles())
    assert skilled["trades"] == 15 and skilled["p"] < sg.P_MAX and skilled["real_pct"] > skilled["random_pct"]
    flat = sg.p_value(_book(str(tmp_path / "b.db"), [100.0, 100.5, 99.5] * 5), candles_fn=lambda coin: _candles())
    assert flat["p"] >= sg.P_MAX
    assert sg.p_value(Ledger(str(tmp_path / "c.db"), 10.0), candles_fn=lambda coin: _candles())["p"] is None


def test_promotion_needs_the_significance_test_besides_wins_and_win_rate(tmp_path, monkeypatch):
    from src.solana import config as cfg, evolution
    from tests.test_arena import make_bots
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(evolution, "PATH", str(tmp_path / "lineage.json"))
    bots, _ = make_bots(tmp_path, {"claude-haiku-5-5": "{}"})
    monkeypatch.setattr(arena, "_bots", bots)
    fake = [{"pnl_usd": 0.1 if i < 12 else -0.1} for i in range(30)]          # 12 wins of 30 = 40%
    monkeypatch.setattr(type(bots["claude-haiku-5-5"].ledger), "closed_trades", lambda self: fake)
    monkeypatch.setattr(arena, "net_of_cost", lambda m: (1.0, 0.1, 0.9))     # net positive: not what this test is about
    monkeypatch.setattr(arena, "significance", lambda m: {"p": 0.3})
    c = arena.can_promote("claude-haiku-5-5")
    assert not c["ok"] and c["p_value"] == 0.3 and c["wins"] == 12
    monkeypatch.setattr(arena, "significance", lambda m: {"p": 0.01})
    assert arena.can_promote("claude-haiku-5-5")["ok"]
    monkeypatch.setattr(arena, "net_of_cost", lambda m: (-0.5, 0.1, -0.6))   # and a net loser never passes
    assert not arena.can_promote("claude-haiku-5-5")["ok"]
