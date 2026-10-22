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
  team: separate from the crypto team, with its own titulars (promoted at 12 wins and ≥ 35%), who trade the real meme
      account (meme_wallet.py) at the same time the crypto titulars trade Hyperliquid.
  evolution: the goal is money. Every EVOLVE_PERIOD_H of a reserve's life, a period with no profit (or no trade) retires
      it; a successor of the same family is born with exits tuned to how the parent lost and its losing situations to
      avoid, named after a Libertadores champion. Titulars are not judged. Roster in MEME_LINEAGE_PATH.
Lab fills: $10 per trade at the DexScreener price, plus 0.5% fees per side and price impact against the pool's liquidity.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg
from src.solana import edge, learning, teamchat
from src.solana.ledger import Ledger, now_iso
from src.solana.market_data import request_json

logger = logging.getLogger("solana.meme")
JUP = "https://lite-api.jup.ag/tokens/v2"
DEX = "https://api.dexscreener.com"
FEE = 0.005                     # per side: swap fee + priority fee, as a fraction of the trade
SKIP = {"SOL", "USDC", "USDT", "WSOL", "JUP", "PUMP"}          # majors and PUMP itself (traded on Hyperliquid)

FOUNDERS = {
    "meme_grad": {"name": "Breno Lopes", "role": "formatura: tokens que acabaram de se formar na pump.fun"},
    "meme_momo": {"name": "Deyverson", "role": "momentum de meme: sobe com mais compradores e volume real"},
    "meme_whale": {"name": "Rony", "role": "segue as baleias das meme coins: compra pesada de várias carteiras grandes"},
}


# The Solana crypto team (same wallet, same process: lab -> titular -> real): verified, liquid, non-meme tokens (SOL, JUP,
# ZEC, XMR, PUMP, BTC/ETH wrappers…). Their moves are small, so exits are tight and quick, and swap costs are lower.
SOL_PARAMS = {"jump_guard": 0.08, "stop_pct": 0.02, "tp_pct": 0.04, "max_hold_h": 2, "traction_min": 20, "traction_pct": 0.004,
              "min_liq": 500_000, "fee": 0.002, "lock_floor": 0.002, "cooldown_h": 0.5,
              "quick_wins": [[0.01, 0.5], [0.02, 0.65], [0.035, 0.75]]}
SOL_FOUNDERS = {
    "sol_break": {"name": "Ademir da Guia", "role": "Solana cripto: rompimento — sobe com compradores (SOL, JUP, ZEC, PUMP…)", "team": "sol"},
    "sol_dip": {"name": "Marcos Assunção", "role": "Solana cripto: repique — compra a queda quando começa a virar", "team": "sol"},
}
SOL_TAG_SKIP = {"stable", "meme", "stocks", "rwa", "equities", "xstocks", "prestocks"}   # not crypto trades: dollars, shares, memes


def _founder(m: str, v: dict) -> dict:
    return {**v, "base": m, "gen": 1, "params": dict(SOL_PARAMS) if v.get("team") == "sol" else {}}


def _load_roster() -> dict:
    try:
        with open(cfg.MEME_LINEAGE_PATH) as f:
            roster = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        roster = {"active": {m: _founder(m, v) for m, v in FOUNDERS.items()}, "retired": []}
    have = {b.get("base") for b in roster["active"].values()} | {r["model"].split("_g")[0] for r in roster["retired"]}
    for m, v in SOL_FOUNDERS.items():                         # an older roster gets the new team without losing anything
        if m not in have:
            roster["active"][m] = _founder(m, v)
    return roster


def _save_roster():
    tmp = cfg.MEME_LINEAGE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ROSTER, f, indent=1, ensure_ascii=False)
    os.replace(tmp, cfg.MEME_LINEAGE_PATH)


ROSTER = _load_roster()
BOTS = ROSTER["active"]                 # model -> {name, role, base (family), gen, params, parent, inherited_avoid}


def fam(bot: str) -> str:
    return BOTS.get(bot, {}).get("base", bot)


def param(bot: str, key: str, default: float) -> float:
    return BOTS.get(bot, {}).get("params", {}).get(key, default)


def team(bot: str) -> str:
    return BOTS.get(bot, {}).get("team", "meme")


GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/pools"
WHALE_MIN_USD = 1000
_flows: dict[str, dict] = {}
_hot: dict[str, dict] = {}            # wallet -> {"tokens": {symbol: ts}, "usd": total whale buys seen}
_ledgers: dict[str, Ledger] = {}
_radar: list[dict] = []
_radar_sol: list[dict] = []
_status = {"at": None, "error": None, "candidates": 0}
_lock = threading.Lock()
_trade_lock = threading.Lock()        # the round and the fast watch never manage the same ledgers at once
_safe_streak: dict[str, int] = {}     # mint -> consecutive rounds it passed the safety checks


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


_sol_cache: dict = {"at": 0.0, "rows": []}


def discover_sol() -> list[dict]:
    """Verified, liquid Solana crypto tokens (not memes, stablecoins or tokenized shares) with Jupiter's stats. The two
    lists are re-read every 3 minutes (the lite API rate-limits hard); a failed read keeps the previous rows."""
    if time.time() - _sol_cache["at"] < 180 and _sol_cache["rows"]:
        return _sol_cache["rows"]
    seen: dict[str, dict] = {}
    for path in ("toptraded/1h?limit=100", "toporganicscore/1h?limit=100"):
        try:
            for t in request_json("GET", f"{JUP}/{path}", retries=2) or []:
                seen.setdefault(t["id"], t)
        except Exception as e:
            logger.warning(f"sol discover {path}: {e}")
    rows = [t for t in seen.values() if t.get("isVerified") and not t.get("launchpad") and not SOL_TAG_SKIP & set(t.get("tags") or [])
            and t.get("symbol", "").upper() not in {"WSOL", "USDC", "USDT"}]
    if rows:
        _sol_cache.update(at=time.time(), rows=rows)
    return rows or _sol_cache["rows"]


def safety(t: dict, team_: str = "meme") -> dict:
    """Each check -> (ok, text). A buy needs all of them."""
    if team_ == "sol":
        return {"verificado pela Jupiter": (bool(t.get("isVerified")), "token verificado"),
                "liquidez ≥ $500k": ((t.get("liquidity") or 0) >= 500_000, f"liquidez ${(t.get('liquidity') or 0):,.0f}"),
                "holders ≥ 5.000": ((t.get("holderCount") or 0) >= 5000, f"{t.get('holderCount') or 0} holders"),
                "volume orgânico": ((t.get("organicScore") or 0) >= 60, f"score orgânico {(t.get('organicScore') or 0):.0f}")}
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
    agg = 0 if is_titular(bot) else 1               # reserves train aggressively: wider windows (titulars stay strict)
    bot = fam(bot)
    if agg:
        buyers = (s1.get("numBuys") or 0) > (s1.get("numSells") or 0)
        organic = (s1.get("buyOrganicVolume") or 0) >= 4_000
    if bot == "sol_break":                            # majors move in fractions of a percent: tight windows
        if 0.8 - 0.4 * agg <= ch1 <= 6 and 0.05 <= ch5 <= 1.5 + 0.5 * agg and buyers:
            return f"{ch1:+.1f}% em 1h e {ch5:+.2f}% em 5 min, mais compradores que vendedores"
    if bot == "sol_dip":
        if -6 <= ch1 <= -(1.2 - 0.4 * agg) and ch5 >= 0.15 - 0.1 * agg and buyers:
            return f"caiu {ch1:.1f}% em 1h e já repica {ch5:+.2f}% em 5 min, com compradores voltando"
    if bot == "meme_grad":
        grad = _age_h(t.get("graduatedAt"))
        if grad is not None and grad <= 6 + 6 * agg and -15 - 10 * agg <= ch1 <= 120 + 80 * agg and ch5 > -5 - 3 * agg and buyers:
            return f"formou na pump.fun há {grad:.1f}h, {ch1:+.0f}% em 1h, mais compradores que vendedores"
    w = t.get("whale") or {}
    if bot == "meme_momo":
        if 5 - 3 * agg <= ch1 <= 150 + 50 * agg and 0 < ch5 <= 25 + 10 * agg and buyers and organic and w.get("net_1h", 0) >= 0:
            return f"{ch1:+.0f}% em 1h e {ch5:+.1f}% em 5 min, compra orgânica ${s1.get('buyOrganicVolume', 0):,.0f}/h"
    if bot == "meme_whale":
        need = max(3000 - 1500 * agg, (0.03 - 0.015 * agg) * (t.get("liquidity") or 0))
        # whales who are already selling are the dump, not the pump: no entry while whale sells run ≥ 40% of whale buys
        dumping = w.get("sell_1h", 0) >= 0.4 * max(w.get("buy_1h", 0), 1)
        if w.get("net_1h", 0) >= need and w.get("buyers", 0) >= 2 and not dumping and -10 <= ch1 <= 150 and ch5 > -5:
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
            f"launchpad {t.get('launchpad') or 'outro'}", f"moeda {t.get('symbol', '?')}", learning.window_tag()]
    return tags


# ─── paper trading ───
def _fill(led: Ledger, bot: str, side: str, mint: str, symbol: str, price: float, liq: float, usd: float = 0.0,
          qty: float = 0.0, reason: str = "", stop: float | None = None, tp: float | None = None, oid: str | None = None) -> dict:
    fee = param(bot, "fee", FEE)
    impact = min(0.25, (usd or qty * price) / max(liq / 2, 1))       # constant-product estimate against the pool
    px = price * (1 + impact) if side == "buy" else price * (1 - impact)
    if side == "buy":
        qty = usd * (1 - fee) / px
    gross = qty * px                                  # buy: what the money bought (usd minus fee); sell: before fee
    fill, _ = led.append_fill(dict(
        order_id=oid or f"meme-{bot}-{side}-{mint[:8]}-{int(time.time() * 1000)}", ts=now_iso(), venue="meme", side=side,
        mint=mint, symbol=symbol, strategy=bot, qty=qty, decimals=0, usd_gross=gross,
        fee_usd=(usd if side == "buy" else gross) * fee, price=px, quote_price=price, price_impact_pct=impact * 100,
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
        prev, guard = p["mark_price"] or entry, param(bot, "jump_guard", cfg.MEME_JUMP_GUARD)
        if abs(px / prev - 1) > guard:                    # one bad quote must not close (or mirror a sale of) a real position
            seen = led.get_state(f"suspect:{p['mint']}")
            if not seen or abs(px / seen - 1) > 0.03:     # first sighting (or a different price): wait for the next tick
                led.set_state(f"suspect:{p['mint']}", px)
                continue
        led.set_state(f"suspect:{p['mint']}", None)
        led.set_mark(p["mint"], px, px * p["qty"])
        meta = led.get_state(f"pos:{p['mint']}") or {}
        peak, liq0 = max(meta.get("peak", entry), px), meta.get("liq0", q["liq"])
        stop = entry * (1 - param(bot, "stop_pct", cfg.MEME_STOP_PCT))
        gain = peak / entry - 1
        locked = None                                     # quick win: bank a share of the peak gain, net of fees
        fee = param(bot, "fee", FEE)
        for at, keep in param(bot, "quick_wins", cfg.MEME_QUICK_WINS):
            if gain >= at:
                locked = max(entry * (1 + keep * gain), entry * (1 + 2 * fee + param(bot, "lock_floor", 0.005)))
        if locked is not None:
            stop = max(stop, locked)
        held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
        held_min = held_h * 60
        reason = ("🚨 liquidez desabou (possível rug)" if q["liq"] < 0.6 * liq0 else
                  "🎯 alvo" if px >= entry * (1 + param(bot, "tp_pct", cfg.MEME_TP_PCT)) else
                  (f"💨 ganho rápido (pico {gain:+.0%}, garantiu {px / entry - 1:+.1%})" if locked is not None and px > entry else "🛑 stop") if px <= stop else
                  f"🧊 sem tração: {held_min:.0f} min sem passar de +{param(bot, 'traction_pct', cfg.MEME_TRACTION_PCT):.1%} (pico {gain:+.1%})"
                  if held_min >= param(bot, "traction_min", cfg.MEME_TRACTION_MIN) and gain < param(bot, "traction_pct", cfg.MEME_TRACTION_PCT) else
                  "⏱ tempo máximo" if held_h >= min(param(bot, "max_hold_h", cfg.MEME_MAX_HOLD_H), cfg.MAX_HOLD_CAP_H) else None)
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


def stop_guard(led: Ledger, bot: str, strategy: str | None = None) -> str | None:
    """Stop-loss guard: N stop-outs within the window -> this bot pauses entries for a while (text says why)."""
    now = datetime.now(timezone.utc)
    key = f"stop_guard_until:{strategy or bot}"       # the accounts hold several strategies on one ledger
    paused = led.get_state(key)
    if paused and paused > now.isoformat():
        return f"pausa até {paused[11:16]} UTC: {cfg.STOPLOSS_GUARD_N} stops em {cfg.STOPLOSS_GUARD_WINDOW_H:.0f}h"
    since = (now - timedelta(hours=cfg.STOPLOSS_GUARD_WINDOW_H)).isoformat()
    stops = [t for t in led.closed_trades() if t["exit_ts"] >= since and (strategy is None or t["strategy"] == strategy)
             and ("stop" in t["reason"] or "rug" in t["reason"]) and "ganho rápido" not in t["reason"] and t["pnl_usd"] < 0]
    if len(stops) < cfg.STOPLOSS_GUARD_N:
        return None
    until = (now + timedelta(hours=cfg.STOPLOSS_GUARD_PAUSE_H)).isoformat(timespec="seconds")
    led.set_state(key, until)
    led.log_decision("veto", strategy=strategy or bot, reason=f"🛡 guarda de stops: {len(stops)} stops em {cfg.STOPLOSS_GUARD_WINDOW_H:.0f}h "
                                                 f"(${sum(t['pnl_usd'] for t in stops):+.2f}) — sem entradas por {cfg.STOPLOSS_GUARD_PAUSE_H:.0f}h")
    return f"pausa até {until[11:16]} UTC"


_regime = {"ok": True, "size_mult": 1.0, "why": ""}


def regime(candles_fn=None) -> dict:
    """Market regime for meme buys, from SOL's and BTC's closed 1h candles (Hyperliquid, one fetch per hour shared with
    the crypto bots): a fall below MEME_REGIME_1H_PCT in the last hour on either blocks buys, a 4h fall below
    MEME_REGIME_4H_PCT halves the trade size. Data errors never block."""
    from src.solana import hyperliquid as hl
    out = {"ok": True, "size_mult": 1.0, "why": "", "returns": {}}
    for coin in ("SOL", "BTC"):
        try:
            closes = [c["close"] for c in (candles_fn or hl.candles)(coin)][:-1]      # drop the still-open candle
        except Exception as e:
            logger.warning(f"meme regime {coin}: {e}")
            continue
        if len(closes) < 5:
            continue
        r1, r4 = closes[-1] / closes[-2] - 1, closes[-1] / closes[-5] - 1
        out["returns"][coin] = {"1h": round(r1, 4), "4h": round(r4, 4)}
        if r1 < cfg.MEME_REGIME_1H_PCT:
            out["ok"] = False
            out["why"] = ", ".join(x for x in (out["why"], f"{coin} {r1:+.1%} em 1h") if x)
        if r4 < cfg.MEME_REGIME_4H_PCT:
            out["size_mult"] = 0.5
    return out


def enter(bot: str, cands: list[dict], prices: dict[str, dict], regime: dict | None = None):
    led = ledger(bot)
    if stop_guard(led, bot):
        return
    reg = regime if regime is not None else _regime
    if not reg["ok"] and fam(bot) != "sol_dip":                    # the dip bot's whole job is buying a fall: it keeps the size cut only
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        if led.get_state("regime_veto_hour") != hour:              # logged once per hour, not every round
            led.set_state("regime_veto_hour", hour)
            led.log_decision("veto", strategy=bot, reason=f"🌧 regime: {reg['why']} — sem compras de meme nesta hora")
        return
    usd = cfg.MEME_TRADE_USD * reg["size_mult"]
    held = {p["mint"] for p in led.positions()}
    recent = {d["mint"] for d in led.decisions(200, kind="exit")        # no re-entry right after an exit (6h memes, 30 min majors)
              if d["ts"] >= (datetime.now(timezone.utc) - timedelta(hours=param(bot, "cooldown_h", 6))).isoformat()}
    for t in cands:
        if len(held) >= (cfg.MEME_MAX_POSITIONS if is_titular(bot) else cfg.MEME_RESERVE_MAX_POSITIONS) or led.cash() < usd:
            return
        mint = t["id"]
        if mint in held or mint in recent or mint not in prices:
            continue
        why = signal(bot, t)
        if not why or not all(ok for ok, _ in safety(t, team(bot)).values()):
            continue
        if (t.get("liquidity") or 0) < param(bot, "min_liq", 0):
            continue
        tags = situation(t)
        rec = learning.record(led, bot)
        if avoided := learning.avoid(rec, tags, BOTS.get(bot, {}).get("inherited_avoid", [])):
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
        buy = _fill(led, bot, "buy", mint, t.get("symbol", "?"), q["price"], q["liq"], usd=usd, reason=why,
              stop=q["price"] * (1 - param(bot, "stop_pct", cfg.MEME_STOP_PCT)), tp=q["price"] * (1 + param(bot, "tp_pct", cfg.MEME_TP_PCT)))
        led.set_state(f"pos:{mint}", {"peak": q["price"], "liq0": q["liq"]})
        _sync_positions(led)
        led.log_decision("entry", mint=mint, symbol=t.get("symbol"), strategy=bot, signals={"tags": tags, "edge": est},
                         order_id=buy["order_id"], reason=f"{why} · {edge.describe(est)}; "
                         + (f"token verificado, {t.get('holderCount')} holders, liquidez ${q['liq']:,.0f}" if team(bot) == "sol" else
                            f"seguro: mint/freeze revogados, dev {(t.get('audit') or {}).get('devBalancePercentage', 0):.1f}%, "
                            f"{t.get('holderCount')} holders, liquidez ${q['liq']:,.0f}"))
        held.add(mint)


def run_round() -> dict:
    """One trench round: discover, price, manage exits, then look for entries (each bot on its own ledger)."""
    try:
        cands = discover()
        sol_cands = discover_sol()
        open_mints = [p["mint"] for b in BOTS for p in ledger(b).positions()]
        prices = pair_prices(list(dict.fromkeys(open_mints + [t["id"] for t in cands] + [t["id"] for t in sol_cands])))
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
        # a token must pass the safety checks on consecutive rounds: one-second fake liquidity or a holder snapshot
        # that looks fine for a minute does not get a buy (warp-id's CONSECUTIVE_FILTER_MATCHES)
        for t in cands:
            _safe_streak[t["id"]] = _safe_streak.get(t["id"], 0) + 1 if all(ok for ok, _ in safety(t).values()) else 0
        ready = [t for t in cands if _safe_streak.get(t["id"], 0) >= cfg.MEME_SAFE_ROUNDS]
        for t in sol_cands:                                        # verified majors: one passing round is enough
            _safe_streak[t["id"]] = _safe_streak.get(t["id"], 0) + 1 if all(ok for ok, _ in safety(t, "sol").values()) else 0
        sol_ready = [t for t in sol_cands if _safe_streak.get(t["id"], 0) >= 1]
        global _regime
        _regime = regime()
        with _trade_lock:
            for b in list(BOTS):
                manage(b, prices)
                enter(b, sol_ready if team(b) == "sol" else ready, prices)
            _mirror(prices)
        evolve()
        with _lock:
            _radar[:] = _radar_rows(cands, prices, "meme")[:40]
            _radar_sol[:] = _radar_rows(sol_cands, prices, "sol")[:30]
        _status.update(at=now_iso(), error=None, candidates=len(cands))
        return {"candidates": len(cands)}
    except Exception as e:
        _status["error"] = str(e)[:200]
        logger.warning(f"meme round: {e}")
        return {"error": str(e)}


def _radar_rows(cands: list[dict], prices: dict[str, dict], team_: str) -> list[dict]:
    rows = []
    for t in cands:
        checks = safety(t, team_)
        rows.append({"mint": t["id"], "symbol": t.get("symbol"), "name": t.get("name"), "icon": t.get("icon"),
                     "price": (prices.get(t["id"]) or {}).get("price"), "liq": t.get("liquidity"),
                     "holders": t.get("holderCount"), "organic": t.get("organicScore"),
                     "ch1h": (t.get("stats1h") or {}).get("priceChange"), "ch5m": (t.get("stats5m") or {}).get("priceChange"),
                     "launchpad": t.get("launchpad"), "graduated_h": _age_h(t.get("graduatedAt")), "whale": t.get("whale") or None,
                     "boosts": (prices.get(t["id"]) or {}).get("boosts", 0),
                     "checks": {k: {"ok": ok, "txt": txt} for k, (ok, txt) in checks.items()},
                     "safe": all(ok for ok, _ in checks.values()),
                     "signals": {b: signal(b, t) for b in BOTS if team(b) == team_}})
    rows.sort(key=lambda r: (not r["safe"], not any(r["signals"].values()), -(r["organic"] or 0)))
    return rows


def _mirror(prices: dict[str, dict]):
    """The real meme account follows the lab (dry run, or the titulars for real)."""
    try:
        from src.solana import meme_wallet
        meme_wallet.mirror({b: {p["mint"]: {"symbol": p["symbol"], "opened_at": p["opened_at"]} for p in ledger(b).positions()} for b in BOTS},
                           {b for b in BOTS if is_titular(b)}, {m: q["price"] for m, q in prices.items()})
    except Exception as e:
        logger.warning(f"meme real account: {e}")


def watch() -> int:
    """Fast exit check (every MEME_WATCH_SEC): re-price only the open positions and run the exits. A meme can lose
    half its value between two one-minute rounds; the stop and the quick-win locks must see it sooner."""
    mints = list(dict.fromkeys(p["mint"] for b in list(BOTS) for p in ledger(b).positions()))
    if not mints:
        return 0
    prices = pair_prices(mints)
    with _trade_lock:
        for b in list(BOTS):
            manage(b, prices)
        _mirror(prices)
    return len(mints)


def board() -> dict:
    bots = []
    for b, meta in BOTS.items():
        led = ledger(b)
        trades = led.closed_trades()
        pos = led.positions()
        eq = led.cash() + sum((p["mark_value"] if p["mark_value"] is not None else p["cost_usd"]) for p in pos)
        wins = sum(t["pnl_usd"] > 0 for t in trades)
        wr = wins / len(trades) if trades else 0.0
        tit = is_titular(b)
        mode = {"real": tit and cfg.MEME_REAL_MODE == "real", "titular": tit, "wins": wins, "need_wins": cfg.PROMOTE_MIN_WINS,
                "win_rate": round(wr, 3), "need_win_rate": cfg.PROMOTE_MIN_WIN_RATE, "ready": can_promote(b),
                "gen": meta.get("gen", 1), "parent": meta.get("parent_name"), "why": meta.get("why", []),
                "next_cut": None if tit else _next_cut(led)}
        bots.append({"model": b, **meta, "mode": mode, "learned": learning.summary(learning.record(led, b)), "equity": round(eq, 2), "pnl": round(eq - led.contributed(), 4),
                     "trades": len(trades), "wins": wins, "win_rate": round(wins / len(trades), 3) if trades else None,
                     "positions": [{"symbol": p["symbol"], "mint": p["mint"], "cost": round(p["cost_usd"], 2),
                                    "now": round(p["mark_value"] or p["cost_usd"], 2), "opened_at": p["opened_at"]} for p in pos],
                     "recent": [{"symbol": t["symbol"], "pnl": round(t["pnl_usd"], 4), "pct": round(t["pnl_pct"] * 100, 1),
                                 "exit_ts": t["exit_ts"], "reason": t["reason"]} for t in trades[:8]]})
    with _lock:
        radar, radar_sol = list(_radar), list(_radar_sol)
    return {"status": dict(_status), "bots": bots, "radar": radar, "radar_sol": radar_sol, "hot_wallets": hot_wallets(),
            "retired": ROSTER["retired"][-6:][::-1]}


def bot_detail(bot: str) -> dict:
    """Everything the bot's modal shows: record, positions, trades, decisions, lessons, lineage, path to titular."""
    if bot not in BOTS:
        raise KeyError(bot)
    led, meta = ledger(bot), BOTS[bot]
    trades = led.closed_trades()
    wins = [t for t in trades if t["pnl_usd"] > 0]
    gross_w, gross_l = sum(t["pnl_usd"] for t in wins), -sum(t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0)
    exits: dict[str, dict] = {}
    for t in trades:
        k = ("ganho rápido" if "ganho rápido" in t["reason"] else "alvo" if "alvo" in t["reason"] else "rug" if "rug" in t["reason"]
             else "sem tração" if "sem tração" in t["reason"]
             else "stop" if "stop" in t["reason"] else "tempo" if "tempo" in t["reason"] else "outro")
        e = exits.setdefault(k, {"n": 0, "pnl": 0.0})
        e["n"] += 1
        e["pnl"] = round(e["pnl"] + t["pnl_usd"], 4)
    tit = is_titular(bot)
    return {"model": bot, "name": meta["name"], "role": meta["role"], "base": meta["base"], "gen": meta.get("gen", 1),
            "parent": meta.get("parent_name"), "why": meta.get("why", []), "inherited_avoid": meta.get("inherited_avoid", []),
            "params": {"stop_pct": param(bot, "stop_pct", cfg.MEME_STOP_PCT), "tp_pct": param(bot, "tp_pct", cfg.MEME_TP_PCT),
                       "max_hold_h": param(bot, "max_hold_h", cfg.MEME_MAX_HOLD_H), "min_liq": param(bot, "min_liq", 30_000),
                       "traction_min": param(bot, "traction_min", cfg.MEME_TRACTION_MIN), "traction_pct": param(bot, "traction_pct", cfg.MEME_TRACTION_PCT),
                       "team": team(bot), "fee": param(bot, "fee", FEE),
                       "quick_wins": param(bot, "quick_wins", cfg.MEME_QUICK_WINS)},
            "titular": tit, "can_promote": can_promote(bot), "need_wins": cfg.PROMOTE_MIN_WINS, "need_win_rate": cfg.PROMOTE_MIN_WIN_RATE,
            "born_at": _born(led).isoformat(timespec="seconds"), "next_cut": None if tit else _next_cut(led),
            "last_verdict": led.get_state("last_verdict"),
            "stats": {"trades": len(trades), "wins": len(wins), "win_rate": round(len(wins) / len(trades), 3) if trades else None,
                      "pnl_usd": round(sum(t["pnl_usd"] for t in trades), 4), "avg_win": round(gross_w / len(wins), 4) if wins else None,
                      "avg_loss": round(gross_l / (len(trades) - len(wins)), 4) if len(trades) > len(wins) else None,
                      "profit_factor": round(gross_w / gross_l, 2) if gross_l else None, "exits": exits,
                      "equity": round(led.cash() + sum((p["mark_value"] if p["mark_value"] is not None else p["cost_usd"]) for p in led.positions()), 2)},
            "positions": [{"symbol": p["symbol"], "mint": p["mint"], "cost": round(p["cost_usd"], 2),
                           "now": round(p["mark_value"] if p["mark_value"] is not None else p["cost_usd"], 2), "opened_at": p["opened_at"],
                           "peak_pct": round(((led.get_state(f"pos:{p['mint']}") or {}).get("peak", 0) / (p["cost_usd"] / p["qty"]) - 1) * 100, 1)
                           if led.get_state(f"pos:{p['mint']}") else None} for p in led.positions()],
            "trades": [{"symbol": t["symbol"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "pnl": round(t["pnl_usd"], 4),
                        "pct": round(t["pnl_pct"] * 100, 1), "reason": t["reason"]} for t in trades[:30]],
            "decisions": [{"ts": d["ts"], "kind": d["kind"], "symbol": d.get("symbol"), "reason": d["reason"]} for d in led.decisions(40)],
            "learned": learning.summary(learning.record(led, bot))}


# ─── the meme team: titulars and natural selection ───
def is_titular(bot: str) -> bool:
    return bool(ledger(bot).get_state("titular"))


def can_promote(bot: str) -> bool:
    trades = ledger(bot).closed_trades()
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    return wins >= cfg.PROMOTE_MIN_WINS and wins / max(len(trades), 1) >= cfg.PROMOTE_MIN_WIN_RATE


def promote(bot: str) -> dict:
    if bot not in BOTS or not can_promote(bot):
        raise ValueError(f"precisa de {cfg.PROMOTE_MIN_WINS} vitórias e acerto ≥ {cfg.PROMOTE_MIN_WIN_RATE:.0%} no laboratório")
    ledger(bot).set_state("titular", True)
    teamchat.post(bot, "event", f"⚽ {BOTS[bot]['name']} subiu para titular do time de memes: agora opera a conta real de meme coins", throttle=False)
    return {"ok": True, "bot": bot}


def _born(led: Ledger) -> datetime:
    s = led.get_state("born_at")
    if not s:
        s = min((t["entry_ts"] for t in led.closed_trades()), default=now_iso())
        led.set_state("born_at", s)
    return datetime.fromisoformat(s)


def _next_cut(led: Ledger) -> str:
    born, period = _born(led), timedelta(hours=cfg.EVOLVE_PERIOD_H)
    k = int((datetime.now(timezone.utc) - born) / period) + 1
    return (born + k * period).isoformat(timespec="seconds")


def mutate(trades: list[dict], params: dict) -> tuple[dict, list[str]]:
    """The successor's exits, from how the parent lost money (in plain words)."""
    p, why, n = dict(params), [], len(trades) or 1
    by = lambda k: [t for t in trades if k in (t.get("reason") or "")]
    pnl = lambda ts: sum(t["pnl_usd"] for t in ts)
    stops, tps, times, rugs, cold = by("stop"), by("alvo"), by("tempo"), by("rug"), by("sem tração")
    if len(stops) / n >= 0.4 and pnl(stops) < 0:
        # memes that fall hard keep falling (stops averaged −26%): a wider stop only deepens the loss. Cut sooner instead.
        stop = p.get("stop_pct", cfg.MEME_STOP_PCT)
        p["stop_pct"] = round(max(stop * 0.8, min(0.07, stop * 0.5)), 3)
        p["traction_min"] = round(max(p.get("traction_min", cfg.MEME_TRACTION_MIN) * 0.7, 5), 1)
        why.append(f"stop mais curto ({p['stop_pct']:.0%}) e corta sem tração em {p['traction_min']:.0f} min: o pai perdia nos stops")
    if len(cold) / n >= 0.4 and pnl(cold) < 0:
        p["traction_min"] = round(min(p.get("traction_min", cfg.MEME_TRACTION_MIN) * 1.5, 45), 1)
        why.append(f"dá mais tempo para engrenar ({p['traction_min']:.0f} min): o pai cortava cedo demais e perdia")
    if trades and len(tps) / n < 0.15:
        tp = p.get("tp_pct", cfg.MEME_TP_PCT)
        p["tp_pct"] = round(max(tp * 0.8, min(0.15, tp * 0.5)), 3)
        why.append(f"alvo mais perto (+{p['tp_pct']:.0%}): o pai quase nunca chegava ao alvo")
    if len(times) / n >= 0.4 and pnl(times) < 0:
        p["max_hold_h"] = round(max(p.get("max_hold_h", cfg.MEME_MAX_HOLD_H) * 0.6, 0.5), 1)
        why.append(f"sai mais cedo ({p['max_hold_h']:.1f}h): o pai perdia ficando preso por tempo")
    if rugs:
        p["min_liq"] = round(max(p.get("min_liq", 30_000) * 1.5, 45_000))
        why.append(f"só entra com liquidez ≥ ${p['min_liq']:,.0f}: o pai pegou {len(rugs)} rug(s)")
    if not trades:
        why.append("o pai não fez nenhum trade: herda tudo e tenta de novo")
    return p, why or ["mesmas saídas do pai; herda o que ele aprendeu"]


def evolve(now: datetime | None = None) -> list[dict]:
    """At each EVOLVE_PERIOD_H checkpoint of a reserve's life: no profit in the period (or no trade) -> retired, and a
    better-tuned successor of the same family takes the slot. A bot holding positions is judged once they close."""
    from src.solana import evolution
    now = now or datetime.now(timezone.utc)
    events = []
    for bot in list(BOTS):
        led = ledger(bot)
        if is_titular(bot) or led.positions():
            continue
        born, period = _born(led), timedelta(hours=cfg.EVOLVE_PERIOD_H)
        k = int((now - born) / period)
        if k < 1 or led.get_state("judged_checkpoint") == k:
            continue
        start, end = (born + (k - 1) * period).isoformat(), (born + k * period).isoformat()
        window = [t for t in led.closed_trades() if start <= t["exit_ts"] < end]
        pnl = sum(t["pnl_usd"] for t in window)
        led.set_state("judged_checkpoint", k)
        if window and pnl > 0:
            led.log_decision("evolution", reason=f"✅ passou no corte de {cfg.EVOLVE_PERIOD_H:.0f}h: lucro ${pnl:+.2f} em {len(window)} trades")
            continue
        parent = BOTS.pop(bot)
        used = {b.get("name") for b in BOTS.values()} | {r.get("name") for r in ROSTER["retired"]} | \
               {b.get("label") for b in evolution.load()["bots"].values()}
        name = next((c for c in evolution.CHAMPIONS if c not in used), f"{parent['name']} II")
        params, why = mutate(led.closed_trades(), parent.get("params", {}))
        rec = learning.record(led, bot)
        seen = learning.summary(rec)["seen"] or 1
        avoid = sorted(set(parent.get("inherited_avoid", [])) | {t for t, r in rec.items() if learning.bad(r, seen) and not t.startswith("moeda ")})
        gen = parent.get("gen", 1) + 1
        new = f"{parent['base']}_g{gen}_{len(ROSTER['retired']) + 1}"
        BOTS[new] = {"name": name, "role": parent["role"], "base": parent["base"], "team": parent.get("team", "meme"), "gen": gen, "params": params, "why": why,
                     "parent": bot, "parent_name": parent["name"], "inherited_avoid": avoid}
        verdict = f"{len(window)} trades, ${pnl:+.2f}"
        ROSTER["retired"].append({"model": bot, "name": parent["name"], "at": end, "verdict": verdict, "successor": new, "successor_name": name})
        _save_roster()
        led.log_decision("evolution", reason=f"❌ aposentado no corte de {cfg.EVOLVE_PERIOD_H:.0f}h ({verdict}): não fez dinheiro; sucessor: {name}")
        ledger(new).set_state("born_at", now.isoformat(timespec="seconds"))
        ledger(new).log_decision("evolution", reason=f"🐣 nasceu {name} (geração {gen}, filho de {parent['name']}): " + "; ".join(why))
        teamchat.post(new, "event", f"🐣 {parent['name']} não fez dinheiro em {cfg.EVOLVE_PERIOD_H:.0f}h e saiu. Eu sou {name}, "
                                    f"geração {gen}: {why[0]}", throttle=False)
        events.append({"retired": bot, "born": new, "name": name, "why": why})
    return events


def start():
    from src.solana import engine as paper_engine
    sch = paper_engine.scheduler()
    if cfg.MEME_ENABLED and sch.get_job("meme_round") is None:
        sch.add_job(run_round, "interval", seconds=cfg.MEME_CYCLE_SEC, id="meme_round", max_instances=1, coalesce=True,
                    next_run_time=datetime.now() + timedelta(seconds=30))
        sch.add_job(watch, "interval", seconds=cfg.MEME_WATCH_SEC, id="meme_watch", max_instances=1, coalesce=True,
                    next_run_time=datetime.now() + timedelta(seconds=45))
