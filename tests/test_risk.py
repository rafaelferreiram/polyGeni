from src.solana.agents import Portfolio, RiskAgent
from src.solana.strategies import Signal


def sig(stop=0.05, tp=0.1):
    return Signal("momentum", 1.0, stop, tp, "test")


def pf(**kw):
    base = dict(equity=100.0, cash=100.0, exposure=0.0, open_mints=set(), strategy_exposure={},
                day_start_equity=100.0, kill_switch=False)
    base.update(kw)
    return Portfolio(**base)


R = RiskAgent(max_pos_pct=0.10, max_exposure_pct=0.5, max_positions=3, max_daily_loss_pct=0.05, min_trade_usd=5)


def test_sizes_to_max_position_pct():
    v = R.evaluate(sig(), "M", 0.5, pf())
    assert v.approve and v.detail["size_usd"] == 10.0


def test_veto_when_size_below_minimum():
    v = R.evaluate(sig(), "M", 0.5, pf(equity=30, cash=30, day_start_equity=30))   # 10% of $30 = $3 < $5
    assert not v.approve and "size" in v.reason


def test_veto_on_total_exposure():
    v = R.evaluate(sig(), "M", 0.5, pf(cash=52, exposure=48, open_mints={"a", "b"}))
    assert not v.approve   # only $2 of exposure room left


def test_veto_on_strategy_capital():
    v = R.evaluate(sig(), "M", 0.2, pf(cash=82, exposure=18, strategy_exposure={"momentum": 18}, open_mints={"a"}))
    assert not v.approve


def test_veto_on_max_positions_and_duplicates():
    assert not R.evaluate(sig(), "M", 0.5, pf(open_mints={"a", "b", "c"})).approve
    assert not R.evaluate(sig(), "a", 0.5, pf(open_mints={"a"})).approve


def test_stop_and_tp_mandatory():
    assert not R.evaluate(sig(stop=0), "M", 0.5, pf()).approve
    assert not R.evaluate(sig(tp=0), "M", 0.5, pf()).approve


def test_daily_loss_and_kill_switch_veto():
    assert R.daily_loss_breached(pf(equity=94.9))
    assert not R.daily_loss_breached(pf(equity=95.1))
    assert not R.evaluate(sig(), "M", 0.5, pf(equity=94, cash=94)).approve
    assert not R.evaluate(sig(), "M", 0.5, pf(kill_switch=True)).approve


def test_daily_entry_cap_and_fingerprint():
    r = RiskAgent(max_entries_per_day=2)
    assert not r.evaluate(sig(), "M", 0.5, pf(entries_today=2)).approve
    v = r.evaluate(sig(), "M", 0.5, pf(entries_today=1))
    assert v.approve and len(v.detail["fingerprint"]) == 64


def test_two_x_leverage_fits_three_positions_on_18_dollars():
    r = RiskAgent(max_pos_pct=1.0, max_exposure_pct=1.0, max_positions=0, max_daily_loss_pct=0.1, min_trade_usd=10,
                  max_position_usd=12, max_capital_usd=20, leverage=2.0)
    p = pf(equity=18.4, cash=18.4, day_start_equity=18.4)
    sizes = []
    for mint in ("A", "B", "C"):
        v = r.evaluate(sig(), mint, 1.0, p)
        assert v.approve and v.detail["max_positions"] == 3
        sizes.append(v.detail["size_usd"])
        p = pf(equity=18.4, cash=p.cash - sizes[-1], exposure=p.exposure + sizes[-1], open_mints=p.open_mints | {mint},
               day_start_equity=18.4)
    assert sizes[:2] == [12, 12] and 10 <= sizes[2] < 12          # the third one is trimmed to the margin left
    assert not r.evaluate(sig(), "D", 1.0, p).approve               # a 4th never fits
    one_x = RiskAgent(max_pos_pct=1.0, max_exposure_pct=1.0, max_positions=0, min_trade_usd=10,
                      max_position_usd=12, max_capital_usd=20)
    assert one_x.evaluate(sig(), "A", 1.0, pf(equity=18.4, cash=18.4, day_start_equity=18.4)).detail["max_positions"] == 1


def test_coffee_doubles_the_daily_entry_cap():
    r = RiskAgent(max_pos_pct=0.1, max_exposure_pct=1.0, max_positions=10, min_trade_usd=5, max_entries_per_day=3)
    p = pf(entries_today=3)
    assert not r.evaluate(sig(), "M", 1.0, p).approve
    assert r.evaluate(sig(), "M", 1.0, p, entry_cap_mult=2).approve
