"""
The trench: lab bots dedicated to Solana meme coins (paper money, real prices). Meme coins live and die in hours —
79% of pump.fun tokens only trade on launch day — so these bots are separate from the Hyperliquid team:
  discovery (every MEME_CYCLE_SEC): Jupiter's trending / top-organic lists and DexScreener's newest profiles (tokens that
      paid for a page with socials). No X API: socials are only checked to exist.
  safety (Jupiter token audit, required for every buy): mint and freeze authority revoked, dev holding ≤ 5%, top
      holders ≤ 30%, enough holders, enough liquidity, organic score (real wallets, not wash volume).
  bots:
      Breno Lopes (meme_grad)  — tokens that graduated from pump.fun in the last hours and stay healthy.
      Deyverson   (meme_momo)  — momentum: rising price with more buyers than sellers and real volume.
      Rony        (meme_whale) — follows the whales: big net buying in the last hour by several large wallets.
  whales (GeckoTerminal trades ≥ $1k on each token's deepest pool, for open positions and safe candidates): net whale
      buying in 1h, how many whale wallets, and "hot wallets" — whales seen buying several of our tokens.
  learning: every buy keeps its situation (graduation age, 1h move, organic score, liquidity, whales); a situation a
      bot keeps losing in is skipped, and the margin of error (edge.py) skips situations clearly worse than its usual.
  exits: stop −18%, target +40%, the stop rises with the gain (+20% locks +8%, +30% locks 60% of the gain), out after
      MEME_MAX_HOLD_H, and out at once if liquidity collapses (rug alarm).
Fills: $10 per trade at the DexScreener price, plus 0.5% fees per side and price impact against the pool's liquidity.
Never real money: there is no wallet and no swap here.
"""
import logging

from src.solana import teamchat
import threading
import time
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg
from src.solana import edge, learning
from src.solana.ledger import Ledger, now_iso
from src.solana.market_data import request_json

logger = logging.getLogger("solana.meme")
JUP = "https://lite-api.jup.ag/tokens/v2"
DEX = "https://api.dexscreener.com"
FEE = 0.005                     # per side: swap fee + priority fee, as a fraction of the trade
SKIP = {"SOL", "USDC", "USDT", "WSOL", "JUP", "PUMP"}          # majors and PUMP itself (traded on Hyperliquid)

BOTS = {
    "meme_grad": {"name": "Breno Lopes", "role": "formatura: tokens que acabaram de se formar na pump.fun"},
    "meme_momo": {"name": "Deyverson", "role": "momentum de meme: sobe com mais compradores e volume real"},
    "meme_whale": {"name": "Rony", "role": "segue as baleias das meme coins: compra pesada de várias carteiras grandes"},
}
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/pools"
WHALE_MIN_USD = 1000
_flows: dict[str, dict] = {}
_hot: dict[str, dict] = {}            # wallet -> {"tokens": {symbol: ts}, "usd": total whale buys seen}
_ledgers: dict[str, Ledger] = {}
_radar: list[dict] = []
_status = {"at": None, "error": None, "candidates": 0}
_lock = threading.Lock()


def ledger(bot: str) -> Ledger:
    if bot not in _ledgers:
        led = Ledger(f"arena_{bot}.db", 0.0)
        if led.contributed() < cfg.MEME_CAPITAL_USD:      # paper capital, counted as a contribution (never profit)
            led.move_cash("deposit", cfg.MEME_CAPITAL_USD - led.contributed(), "capital do laboratório de memes")
        led.set_state("model", bot)
        _ledgers[bot] = led
    return _ledgers[bot]


# ─── data ───
def _age_h(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return None


def discover() -> list[dict]:
    """Candidate meme tokens with Jupiter's audit and stats (one record per token)."""
    seen: dict[str, dict] = {}
    for path in ("toptrending/1h?limit=100", "toporganicscore/1h?limit=100"):
        try:
            for t in request_json("GET", f"{JUP}/{path}") or []:
                seen.setdefault(t["id"], t)
        except Exception as e:
            logger.warning(f"meme discover {path}: {e}")
    try:
        new = [p["tokenAddress"] for p in request_json("GET", f"{DEX}/token-profiles/latest/v1") or []
               if p.get("chainId") == "solana" and p["tokenAddress"] not in seen][:8]
        for addr in new:
            rows = request_json("GET", f"{JUP}/search", params={"query": addr}) or []
            t = next((r for r in rows if r.get("id") == addr), None)
            if t:
                seen[addr] = {**t, "dex_profile": True}
    except Exception as e:
        logger.warning(f"meme discover dexscreener: {e}")
    out = []
    for t in seen.values():
        age = _age_h((t.get("firstPool") or {}).get("createdAt"))
        if t.get("symbol", "").upper() in SKIP or (age is not None and age > 24 * 14) or not (t.get("launchpad") or (age or 1e9) < 24 * 14):
            continue                                  # not a meme: majors, stocks, old tokens
        out.append(t)
    return out


def safety(t: dict) -> dict:
    """Each check -> (ok, text). A buy needs all of them."""
    a = t.get("audit") or {}
    top, dev = a.get("topHoldersPercentage"), a.get("devBalancePercentage")
    checks = {
        "mint/freeze revogados": (bool(a.get("mintAuthorityDisabled")) and bool(a.get("freezeAuthorityDisabled")), "o dev não pode criar nem congelar tokens"),
        # no dev data is common on older tokens: after 24h it no longer blocks (top holders still must be ≤ 30%)
        "dev ≤ 5%": (dev <= 5 if dev is not None else (_age_h((t.get("firstPool") or {}).get("createdAt")) or 0) > 24,
                     f"dev tem {dev:.1f}%" if dev is not None else "sem dado do dev"),
        "top holders ≤ 30%": (top is not None and top <= 30, f"top holders {top:.0f}%" if top is not None else "sem dado"),
        "holders ≥ 300": ((t.get("holderCount") or 0) >= 300, f"{t.get('holderCount') or 0} holders"),
        "liquidez ≥ $30k": ((t.get("liquidity") or 0) >= 30_000, f"liquidez ${(t.get('liquidity') or 0):,.0f}"),
        "volume orgânico": ((t.get("organicScore") or 0) >= 40, f"score orgânico {(t.get('organicScore') or 0):.0f}"),
    }
    return checks


def signal(bot: str, t: dict) -> str | None:
    """Why this bot would buy this token now (None = no)."""
    s1, s5 = t.get("stats1h") or {}, t.get("stats5m") or {}
    ch1, ch5 = s1.get("priceChange") or 0, s5.get("priceChange") or 0
    buyers = (s1.get("numBuys") or 0) > 1.2 * (s1.get("numSells") or 0) and (s1.get("numNetBuyers") or 0) > 0
    organic = (s1.get("buyOrganicVolume") or 0) >= 8_000
    if bot == "meme_grad":
        grad = _age_h(t.get("graduatedAt"))
        if grad is not None and grad <= 6 and -15 <= ch1 <= 120 and ch5 > -5 and buyers:
            return f"formou na pump.fun há {grad:.1f}h, {ch1:+.0f}% em 1h, mais compradores que vendedores"
    w = t.get("whale") or {}
    if bot == "meme_momo":
        if 5 <= ch1 <= 150 and 0 < ch5 <= 25 and buyers and organic and w.get("net_1h", 0) >= 0:
            return f"{ch1:+.0f}% em 1h e {ch5:+.1f}% em 5 min, compra orgânica ${s1.get('buyOrganicVolume', 0):,.0f}/h"
    if bot == "meme_whale":
        need = max(3000, 0.03 * (t.get("liquidity") or 0))
        if w.get("net_1h", 0) >= need and w.get("buyers", 0) >= 2 and -10 <= ch1 <= 150 and ch5 > -5:
            return f"baleias compraram ${w['net_1h']:,.0f} líquidos em 1h ({w['buyers']} carteiras grandes)"
    return None


def pair_prices(mints: list[str]) -> dict[str, dict]:
    """Live price and liquidity per token (DexScreener, deepest pool)."""
    out: dict[str, dict] = {}
    for i in range(0, len(mints), 30):
        try:
            for p in request_json("GET", f"{DEX}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []:
                m = (p.get("baseToken") or {}).get("address")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m and p.get("priceUsd") and liq >= (out.get(m) or {}).get("liq", -1):
                    out[m] = {"price": float(p["priceUsd"]), "liq": float(liq), "pool": p.get("pairAddress"),
                              "boosts": (p.get("boosts") or {}).get("active", 0)}
        except Exception as e:
            logger.warning(f"meme prices: {e}")
    return out


def whale_flow(mint: str, symbol: str, pool: str, liq: float) -> dict:
    """Big trades of the last hour on the token's deepest pool: whale = at least max($1k, 1% of liquidity)."""
    big = max(WHALE_MIN_USD, 0.01 * (liq or 0))
    rows = (request_json("GET", f"{GECKO}/{pool}/trades", retries=1,      # rate-limited: skip, the next round retries
                         params={"trade_volume_in_usd_greater_than": WHALE_MIN_USD}) or {}).get("data") or []
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    buy = sell = 0.0
    buyers, sellers = set(), set()
    for r in rows:
        a = r.get("attributes") or {}
        usd = float(a.get("volume_in_usd") or 0)
        ts = datetime.fromisoformat((a.get("block_timestamp") or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
        if usd < big or ts < since:
            continue
        who = a.get("tx_from_address") or "?"
        if a.get("kind") == "buy":
            buy += usd
            buyers.add(who)
            h = _hot.setdefault(who, {"tokens": {}, "usd": 0.0})
            h["tokens"][symbol] = ts.isoformat()
            h["usd"] += usd
        else:
            sell += usd
            sellers.add(who)
    flow = {"buy_1h": round(buy), "sell_1h": round(sell), "net_1h": round(buy - sell), "buyers": len(buyers),
            "sellers": len(sellers), "min_usd": round(big), "at": time.time()}
    _flows[mint] = flow
    return flow


def hot_wallets(limit: int = 8) -> list[dict]:
    """Whale wallets seen buying two or more of the tokens we watch in the last 24h (possible smart money)."""
    cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    out = []
    for w, h in list(_hot.items()):
        toks = {s: t for s, t in h["tokens"].items() if t >= cut}
        if not toks:
            _hot.pop(w, None)
            continue
        if len(toks) >= 2:
            out.append({"wallet": w, "tokens": sorted(toks), "usd": round(h["usd"])})
    return sorted(out, key=lambda x: (-len(x["tokens"]), -x["usd"]))[:limit]


def situation(t: dict) -> list[str]:
    """Plain labels of a meme token's moment (what the bots learn by)."""
    s1, w = t.get("stats1h") or {}, t.get("whale") or {}
    ch, grad = s1.get("priceChange") or 0, _age_h(t.get("graduatedAt"))
    tags = ["formatura < 6h" if grad is not None and grad <= 6 else "formado há mais tempo" if grad is not None else "sem formatura",
            "1h acima de +50%" if ch > 50 else "1h subindo" if ch > 0 else "1h caindo",
            "orgânico alto" if (t.get("organicScore") or 0) >= 60 else "orgânico médio",
            "liquidez < $100k" if (t.get("liquidity") or 0) < 100_000 else "liquidez ≥ $100k",
            "baleias comprando" if w.get("net_1h", 0) > 0 else "baleias vendendo" if w.get("net_1h", 0) < 0 else "sem baleias",
            f"launchpad {t.get('launchpad') or 'outro'}", f"moeda {t.get('symbol', '?')}"]
    return tags


# ─── paper trading ───
def _fill(led: Ledger, bot: str, side: str, mint: str, symbol: str, price: float, liq: float, usd: float = 0.0,
          qty: float = 0.0, reason: str = "", stop: float | None = None, tp: float | None = None, oid: str | None = None) -> dict:
    impact = min(0.25, (usd or qty * price) / max(liq / 2, 1))       # constant-product estimate against the pool
    px = price * (1 + impact) if side == "buy" else price * (1 - impact)
    if side == "buy":
        qty = usd * (1 - FEE) / px
    gross = qty * px                                  # buy: what the money bought (usd minus fee); sell: before fee
    fill, _ = led.append_fill(dict(
        order_id=oid or f"meme-{bot}-{side}-{mint[:8]}-{int(time.time() * 1000)}", ts=now_iso(), venue="meme", side=side,
        mint=mint, symbol=symbol, strategy=bot, qty=qty, decimals=0, usd_gross=gross,
        fee_usd=(usd if side == "buy" else gross) * FEE, price=px, quote_price=price, price_impact_pct=impact * 100,
        slippage_bps=round(impact * 1e4), route="paper · DexScreener price + impact", stop_price=stop, tp_price=tp,
        meta=__import__("json").dumps({"reason": reason})))
    return fill


def _sync_positions(led: Ledger):
    truth = led.positions_from_fills()
    rows = {p["mint"]: p for p in led.positions()}
    for mint, t in truth.items():
        led.upsert_position({**t, "mark_price": (rows.get(mint) or {}).get("mark_price"), "mark_value": (rows.get(mint) or {}).get("mark_value")})
    for mint in rows:
        if mint not in truth:
            led.delete_position(mint)


def manage(bot: str, prices: dict[str, dict]):
    """Exits: rug alarm, stop (rising with the gain), target, time."""
    led = ledger(bot)
    for p in led.positions():
        q = prices.get(p["mint"])
        if not q:
            continue
        px, entry = q["price"], p["cost_usd"] / p["qty"]
        led.set_mark(p["mint"], px, px * p["qty"])
        meta = led.get_state(f"pos:{p['mint']}") or {}
        peak, liq0 = max(meta.get("peak", entry), px), meta.get("liq0", q["liq"])
        stop = entry * (1 - cfg.MEME_STOP_PCT)
        gain = peak / entry - 1
        if gain >= 0.30:
            stop = max(stop, entry * (1 + 0.6 * gain))
        elif gain >= 0.20:
            stop = max(stop, entry * 1.08)
        held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
        reason = ("🚨 liquidez desabou (possível rug)" if q["liq"] < 0.6 * liq0 else
                  "🎯 alvo" if px >= entry * (1 + cfg.MEME_TP_PCT) else
                  "🛑 stop" if px <= stop else
                  "⏱ tempo máximo" if held_h >= cfg.MEME_MAX_HOLD_H else None)
        led.set_state(f"pos:{p['mint']}", {"peak": peak, "liq0": liq0})
        if not reason:
            continue
        fill = _fill(led, bot, "sell", p["mint"], p["symbol"], px, q["liq"], qty=p["qty"], reason=reason,
                     oid=f"exit-{p['entry_order_id']}")
        pnl = fill["usd_gross"] - fill["fee_usd"] - p["cost_usd"]
        led.log_decision("exit", mint=p["mint"], symbol=p["symbol"], strategy=bot, reason=reason,
                         signals={"pnl_usd": round(pnl, 4), "price": px})
        led.set_state(f"pos:{p['mint']}", None)
        _sync_positions(led)


def enter(bot: str, cands: list[dict], prices: dict[str, dict]):
    led = ledger(bot)
    held = {p["mint"] for p in led.positions()}
    recent = {d["mint"] for d in led.decisions(200, kind="exit")
              if d["ts"] >= (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()}
    for t in cands:
        if len(held) >= cfg.MEME_MAX_POSITIONS or led.cash() < cfg.MEME_TRADE_USD:
            return
        mint = t["id"]
        if mint in held or mint in recent or mint not in prices:
            continue
        why = signal(bot, t)
        if not why or not all(ok for ok, _ in safety(t).values()):
            continue
        tags = situation(t)
        rec = learning.record(led, bot)
        if avoided := learning.avoid(rec, tags):
            led.log_decision("veto", mint=mint, symbol=t.get("symbol"), strategy=bot, signals={"tags": tags},
                             reason="aprendizado: evita " + ", ".join(f"'{x}' ({rec[x]['wins']}/{rec[x]['n']} ganhos)" for x in avoided))
            continue
        done = led.closed_trades()
        est = edge.estimate(tags, rec, {}, *edge.payoff(done), baseline=sum(x["pnl_usd"] > 0 for x in done) / len(done) if len(done) >= 10 else None)
        if est["skip"]:
            led.log_decision("veto", mint=mint, symbol=t.get("symbol"), strategy=bot, signals={"tags": tags, "edge": est},
                             reason=f"margem de erro: {edge.describe(est)}")
            continue
        q = prices[mint]
        buy = _fill(led, bot, "buy", mint, t.get("symbol", "?"), q["price"], q["liq"], usd=cfg.MEME_TRADE_USD, reason=why,
              stop=q["price"] * (1 - cfg.MEME_STOP_PCT), tp=q["price"] * (1 + cfg.MEME_TP_PCT))
        led.set_state(f"pos:{mint}", {"peak": q["price"], "liq0": q["liq"]})
        _sync_positions(led)
        led.log_decision("entry", mint=mint, symbol=t.get("symbol"), strategy=bot, signals={"tags": tags, "edge": est},
                         order_id=buy["order_id"], reason=f"{why} · {edge.describe(est)}; seguro: mint/freeze revogados, dev {(t.get('audit') or {}).get('devBalancePercentage', 0):.1f}%, "
                                f"{t.get('holderCount')} holders, liquidez ${q['liq']:,.0f}")
        held.add(mint)


def run_round() -> dict:
    """One trench round: discover, price, manage exits, then look for entries (each bot on its own ledger)."""
    try:
        cands = discover()
        open_mints = [p["mint"] for b in BOTS for p in ledger(b).positions()]
        prices = pair_prices(list(dict.fromkeys(open_mints + [t["id"] for t in cands])))
        watch = list(dict.fromkeys(open_mints + [t["id"] for t in cands if all(ok for ok, _ in safety(t).values())]))
        sym = {t["id"]: t.get("symbol", "?") for t in cands}
        for p in (p for b in BOTS for p in ledger(b).positions()):
            sym.setdefault(p["mint"], p["symbol"])
        # GeckoTerminal's free API 429s fast: a few pools per round, the stalest first (each refreshes every few minutes)
        stale = sorted(watch[:cfg.MEME_WHALE_TOKENS], key=lambda m: (m not in open_mints, (_flows.get(m) or {}).get("at", 0)))
        for mint in stale[:cfg.MEME_WHALE_PER_ROUND]:
            q = prices.get(mint) or {}
            if q.get("pool"):
                try:
                    whale_flow(mint, sym.get(mint, "?"), q["pool"], q.get("liq", 0))
                except Exception as e:
                    logger.warning(f"meme whales {sym.get(mint)}: {e}")
        for t in cands:
            f = _flows.get(t["id"]) or {}
            t["whale"] = f if time.time() - f.get("at", 0) < 900 else {}      # older than 15 min: not current
            if t["whale"].get("net_1h", 0) >= max(3000, 0.03 * (t.get("liquidity") or 0)) and t["whale"].get("buyers", 0) >= 2:
                teamchat.post("meme_whale", "tip", f"🐋 Baleias compraram ${t['whale']['net_1h']:,.0f} líquidos de {t.get('symbol')} "
                                                   f"na última hora ({t['whale']['buyers']} carteiras grandes)", t.get("symbol"))
        for b in BOTS:
            manage(b, prices)
            enter(b, cands, prices)
        rows = []
        for t in cands:
            checks = safety(t)
            rows.append({"mint": t["id"], "symbol": t.get("symbol"), "name": t.get("name"), "icon": t.get("icon"),
                         "price": (prices.get(t["id"]) or {}).get("price"), "liq": t.get("liquidity"),
                         "holders": t.get("holderCount"), "organic": t.get("organicScore"),
                         "ch1h": (t.get("stats1h") or {}).get("priceChange"), "launchpad": t.get("launchpad"),
                         "graduated_h": _age_h(t.get("graduatedAt")), "whale": t.get("whale") or None,
                         "boosts": (prices.get(t["id"]) or {}).get("boosts", 0),
                         "checks": {k: {"ok": ok, "txt": txt} for k, (ok, txt) in checks.items()},
                         "safe": all(ok for ok, _ in checks.values()),
                         "signals": {b: signal(b, t) for b in BOTS}})
        rows.sort(key=lambda r: (not r["safe"], -(r["organic"] or 0)))
        with _lock:
            _radar[:] = rows[:40]
        _status.update(at=now_iso(), error=None, candidates=len(cands))
        return {"candidates": len(cands)}
    except Exception as e:
        _status["error"] = str(e)[:200]
        logger.warning(f"meme round: {e}")
        return {"error": str(e)}


def board() -> dict:
    bots = []
    for b, meta in BOTS.items():
        led = ledger(b)
        trades = led.closed_trades()
        pos = led.positions()
        eq = led.cash() + sum((p["mark_value"] if p["mark_value"] is not None else p["cost_usd"]) for p in pos)
        wins = sum(t["pnl_usd"] > 0 for t in trades)
        bots.append({"model": b, **meta, "learned": learning.summary(learning.record(led, b)), "equity": round(eq, 2), "pnl": round(eq - led.contributed(), 4),
                     "trades": len(trades), "wins": wins, "win_rate": round(wins / len(trades), 3) if trades else None,
                     "positions": [{"symbol": p["symbol"], "mint": p["mint"], "cost": round(p["cost_usd"], 2),
                                    "now": round(p["mark_value"] or p["cost_usd"], 2), "opened_at": p["opened_at"]} for p in pos],
                     "recent": [{"symbol": t["symbol"], "pnl": round(t["pnl_usd"], 4), "pct": round(t["pnl_pct"] * 100, 1),
                                 "exit_ts": t["exit_ts"], "reason": t["reason"]} for t in trades[:8]]})
    with _lock:
        radar = list(_radar)
    return {"status": dict(_status), "bots": bots, "radar": radar, "hot_wallets": hot_wallets()}


def start():
    from src.solana import engine as paper_engine
    sch = paper_engine.scheduler()
    if cfg.MEME_ENABLED and sch.get_job("meme_round") is None:
        sch.add_job(run_round, "interval", seconds=cfg.MEME_CYCLE_SEC, id="meme_round", max_instances=1, coalesce=True,
                    next_run_time=datetime.now() + timedelta(seconds=30))
