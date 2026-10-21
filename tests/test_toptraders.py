from src.solana import toptraders as tt


def row(addr, av, m_pnl, m_roi, m_vlm, w_pnl=1, a_pnl=1):
    win = lambda p, r, v: {"pnl": str(p), "roi": str(r), "vlm": str(v)}
    return {"ethAddress": addr, "accountValue": str(av), "displayName": None,
            "windowPerformances": [["day", win(0, 0, 0)], ["week", win(w_pnl, 0.01, 0)],
                                   ["month", win(m_pnl, m_roi, m_vlm)], ["allTime", win(a_pnl, 0.1, 0)]]}


def test_pick_keeps_consistent_real_traders_ranked_by_dollars():
    rows = [row("0xbig", 10e6, 2e6, 0.2, 50e6), row("0xbetter", 5e6, 3e6, 0.6, 20e6),
            row("0xsmall", 50_000, 9e6, 0.5, 1e6),          # account too small
            row("0xfluke", 1e6, 5e6, 50.0, 5e6),            # deposit artefact ROI
            row("0xmm", 1e6, 1e6, 0.1, 1e9),                # market maker turnover
            row("0xidle", 1e6, 1e6, 0.1, 1e5),              # not trading
            row("0xweek", 1e6, 1e6, 0.1, 5e6, w_pnl=-1)]    # losing this week
    assert [t["address"] for t in tt.pick(rows)] == ["0xbetter", "0xbig"]


def test_position_changes_become_events_and_consensus():
    before = {"BTC": {"szi": 0.0, "value": 0}, "ETH": {"szi": 1.0, "value": 100}}
    after = {"BTC": {"szi": 2.0, "value": 200}, "ETH": {"szi": 0.0, "value": 0}, "SOL": {"szi": -5.0, "value": 50}}
    ev = {(e[2], e[3]) for e in tt.diff("0xa", before, after, 1000)}
    assert ev == {("BTC", "abriu compra"), ("ETH", "fechou compra"), ("SOL", "abriu venda (short)")}
    tt._traders[:] = [{"address": "0xa"}, {"address": "0xb"}]
    tt._positions.clear(); tt._events.clear()
    tt._positions.update({"0xa": after, "0xb": {"BTC": {"szi": 1.0, "value": 300}}})
    tt._events.extend(tt.diff("0xa", before, after, 1000))
    c = tt.consensus("BTC", now=1500)
    assert c == {"long": 2, "short": 0, "new_longs_1h": 1, "closed_longs_1h": 0, "long_usd": 500, "of": 2}


def test_copy_bot_needs_agreement_fresh_entry_and_uptrend():
    from src.solana.strategies import CopyTop
    up = [{"t": 3600 * i, "open": 100 * 1.002 ** i, "high": 100 * 1.002 ** i * 1.002, "low": 100 * 1.002 ** i * 0.998,
           "close": 100 * 1.002 ** i, "volume": 1} for i in range(300)]
    bot = CopyTop()
    ok = {"long": 3, "short": 1, "new_longs_1h": 1, "of": 10}
    assert bot.decide(up, {"symbol": "X", "top_traders": ok}) is not None
    assert bot.decide(up, {"symbol": "X", "top_traders": {**ok, "new_longs_1h": 0}}) is None   # nobody just entered
    assert bot.decide(up, {"symbol": "X", "top_traders": {**ok, "long": 1}}) is None           # no agreement
