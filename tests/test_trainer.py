from src.solana import config as cfg, trainer


def bars(n, start=100.0, step=1.002):
    return [{"t": 3600 * i, "open": start * step ** i, "high": start * step ** i * 1.004, "low": start * step ** i * 0.996,
             "close": start * step ** i, "volume": 1} for i in range(n)]


def test_outcome_target_stop_and_time():
    up = [{"high": 103.5, "low": 99.5}]
    assert trainer.outcome(100, 0.01, up, "long") == "win"          # +3% target touched, -2% stop not
    assert trainer.outcome(100, 0.01, [{"high": 101, "low": 97.9}], "long") == "loss"
    assert trainer.outcome(100, 0.01, [{"high": 100.5, "low": 99.5}] * 30, "long") == "time"
    assert trainer.outcome(100, 0.01, [{"high": 100.5, "low": 96.9}], "short") == "win"


def test_record_resolve_and_lessons(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TRAINING_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(trainer, "_conn", None)
    candles = bars(260)
    for i in range(210, 230):
        assert trainer.record("X", candles[:i + 1], {"whale_flow": {"pressure": "comprando"}})
    assert not trainer.record("X", candles[:211], {})                # idempotent per candle
    assert trainer.resolve("X", candles) == 20
    by = {x["tag"]: x for x in trainer.lessons(min_n=5)}
    assert by["alta"]["n"] == 20 and by["baleias comprando"]["long_win"] > by["baleias comprando"]["short_win"]
    assert trainer.lessons_for(["alta", "inexistente"], min_n=5)[0].startswith("'alta'")
    assert trainer.stats() == {"samples": 20, "resolved": 20, "last_t": candles[229]["t"]}
