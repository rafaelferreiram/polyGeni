from src.solana import news

RSS = """<rss><channel>
<item><title>Bitcoin breaks $84k as ETF inflows return</title><description>BTC rallies</description>
<link>https://x.example/1</link><pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Zcash hack? No — ZEC privacy upgrade ships</title><description></description>
<link>https://x.example/2</link><pubDate>Wed, 21 Oct 2026 11:00:00 GMT</pubDate></item>
<item><title>Markets wrap</title><description>stocks and dollar</description><link>https://x.example/3</link>
<pubDate>Wed, 21 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""


def test_news_items_are_tagged_with_the_coins_they_mention():
    items = news._parse("Test", RSS)
    assert [i["coins"] for i in items] == [["BTC"], ["ZEC"], []]
    assert news.tag("Solana and Ethereum ETFs") == ["ETH", "SOL"]
    assert news.tag("a pepe meme") == ["kPEPE"] and news.tag("nearly done") == []      # word boundaries


def test_trending_ranks_coins_and_picks_one_headline_each(monkeypatch):
    import time
    now = time.time()
    items = [{"source": "A", "title": "Bitcoin up", "summary": "", "url": "u1", "ts": now - 60, "coins": ["BTC"]},
             {"source": "B", "title": "BTC and ETH ETFs", "summary": "", "url": "u2", "ts": now - 120, "coins": ["BTC", "ETH"]},
             {"source": "C", "title": "Old ZEC story", "summary": "", "url": "u3", "ts": now - 30 * 3600, "coins": ["ZEC"]}]
    monkeypatch.setattr(news, "_items", items)
    t = news.trending(hours=12)
    assert [c["coin"] for c in t["coins"]] == ["BTC", "ETH"] and t["mentions"] == {"BTC": 2, "ETH": 1}
    assert [h["title"] for h in t["headlines"]] == ["Bitcoin up", "BTC and ETH ETFs"]
