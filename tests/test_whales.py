from src.solana import whales


def fill(coin, side, px, sz, h, t=1_000_000, users=("0xbuyer", "0xseller")):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "hash": h, "time": t, "users": list(users)}


def test_whale_orders_are_grouped_by_tx_and_split_into_buy_and_sell_flow():
    whales._trades.clear()
    whales._thresholds["TST"] = 25_000
    now = 1_000
    whales.ingest([fill("TST", "B", 100, 150, "0x1"), fill("TST", "B", 100, 150, "0x1"),    # one $30k buy in 2 fills
                   fill("TST", "A", 100, 300, "0x2", users=("0xb", "0xwhale")),             # $30k sell
                   fill("TST", "B", 100, 400, "0x3"), fill("TST", "B", 100, 10, "0x4")], now=now)  # $40k buy, $1k noise
    s = whales.summary("TST", now=now)
    assert s["1h"] == {"buy_usd": 70_000, "sell_usd": 30_000, "net_usd": 40_000, "n": 3}
    assert s["pressure"] == "comprando"
    assert {"wallet": "0xwhale", "net_usd": -30_000} in s["top_wallets"]
    assert whales.summary("TST", now=now + 5 * 3600)["1h"]["n"] == 0                           # rolls off after the window
