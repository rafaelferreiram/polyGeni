"""
Real meme-coin account on Solana — separate from the crypto account (Hyperliquid) and from its team.
  The bot wallet: a keypair made on this Mac (MEME_WALLET_PATH, git-ignored, readable only by this user). It signs
      its own Jupiter swaps, so the bots can trade 24/7 without anyone clicking. It holds only what the owner deposits.
  The owner: the user's Phantom wallet. Connecting proves ownership (Phantom signs a message, checked here); deposits
      are signed in Phantom; withdrawals can only go back to that address.
  Modes (MEME_REAL_MODE): "ensaio" — every lab meme entry/exit is mirrored with real Jupiter quotes and a transaction
      built, signed and simulated on Solana, never sent; "real" — the same for the meme titulars only, sent for real.
  Small limits: $5 a trade, 2 open positions, 6 buys a day, −$5 a day stops buying, price impact ≤ 2%, slippage ≤ 3%,
      no token Jupiter can't sell back or that charges a transfer fee, and equity below 60% of deposits sells all and halts.
"""
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.hash import Hash
from solders.transaction import Transaction, VersionedTransaction

from src.solana import config as cfg
from src.solana.ledger import Ledger, now_iso
from src.solana.market_data import request_json

logger = logging.getLogger("solana.meme_wallet")
SOL = "So11111111111111111111111111111111111111112"
JUP = "https://lite-api.jup.ag"
LAMPORTS = 1_000_000_000
FEE_LAMPORTS = 5000
_kp: Keypair | None = None
_led: Ledger | None = None


def keypair() -> Keypair:
    global _kp
    if _kp is None:
        path = cfg.MEME_WALLET_PATH
        if os.path.exists(path):
            with open(path) as f:
                _kp = Keypair.from_bytes(bytes(json.load(f)))
        else:
            _kp = Keypair()
            os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
            fd = os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)     # only this user can read the key
            with os.fdopen(fd, "w") as f:
                json.dump(list(bytes(_kp)), f)
            logger.info(f"meme wallet created: {_kp.pubkey()}")
    return _kp


def address() -> str:
    return str(keypair().pubkey())


def ledger() -> Ledger:
    global _led
    if _led is None:
        _led = Ledger(cfg.MEME_REAL_DB_PATH, 0.0)
        if dry() and _led.contributed() < cfg.MEME_REAL_DRY_CAPITAL_USD:   # dry run: pretend deposit to size the limits
            _led.move_cash("deposit", cfg.MEME_REAL_DRY_CAPITAL_USD - _led.contributed(), "ensaio: depósito simulado")
    return _led


def dry() -> bool:
    return cfg.MEME_REAL_MODE != "real"


# ─── Solana / Jupiter ───
def rpc(method: str, params: list):
    r = request_json("POST", cfg.SOLANA_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if r.get("error"):
        raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
    return r["result"]


def sol_usd() -> float:
    d = request_json("GET", f"{JUP}/price/v3", params={"ids": SOL})
    return float(d[SOL]["usdPrice"])


def sol_balance(addr: str | None = None) -> float:
    return rpc("getBalance", [addr or address()])["value"] / LAMPORTS


def mint_info(mint: str) -> tuple[int, int]:
    """(decimals, transfer fee in basis points) — a Token-2022 transfer fee eats every buy and sell."""
    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]["data"]["parsed"]["info"]
    fee = next((e["state"].get("newerTransferFee", {}).get("transferFeeBasisPoints", 0)
                for e in info.get("extensions", []) if e.get("extension") == "transferFeeConfig"), 0)
    return int(info["decimals"]), int(fee)


def token_atomic(mint: str) -> int:
    accs = rpc("getTokenAccountsByOwner", [address(), {"mint": mint}, {"encoding": "jsonParsed"}])["value"]
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accs)


def quote(inp: str, out: str, amount: int) -> dict:
    return request_json("GET", f"{JUP}/swap/v1/quote", params={"inputMint": inp, "outputMint": out, "amount": int(amount),
                                                                "slippageBps": cfg.MEME_REAL_SLIPPAGE_BPS,
                                                                "restrictIntermediateTokens": "true"})


def swap_tx(q: dict) -> VersionedTransaction:
    r = request_json("POST", f"{JUP}/swap/v1/swap", json={
        "quoteResponse": q, "userPublicKey": address(), "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": cfg.MEME_REAL_MAX_PRIORITY_LAMPORTS,
                                                                       "priorityLevel": "high"}}})
    tx = VersionedTransaction.from_bytes(base64.b64decode(r["swapTransaction"]))
    return VersionedTransaction(tx.message, [keypair()])


def simulate(tx) -> str | None:
    v = rpc("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "sigVerify": False,
                                                                           "replaceRecentBlockhash": True}])["value"]
    return json.dumps(v["err"]) if v.get("err") else None


def send(tx) -> str:
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "maxRetries": 3}])
    for _ in range(30):                                   # wait for confirmation (~30s)
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("err"):
            raise RuntimeError(f"transação falhou: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig
        time.sleep(1)
    raise RuntimeError(f"sem confirmação: {sig}")


# ─── trading ───
def _today_buys(led: Ledger) -> list[dict]:
    day = datetime.now(timezone.utc).date().isoformat()
    return [f for f in led.fills(500) if f["side"] == "buy" and f["ts"] >= day]


def _day_pnl(led: Ledger) -> float:
    day = datetime.now(timezone.utc).date().isoformat()
    return sum(t["pnl_usd"] for t in led.closed_trades() if t["exit_ts"] >= day)


def buy(bot: str, mint: str, symbol: str) -> dict | None:
    """Mirror a lab entry. Returns the fill, or None (and logs why) when a limit or check says no."""
    led = ledger()
    no = lambda why: led.log_decision("veto", mint=mint, symbol=symbol, strategy=bot, reason=why) and None
    if led.get_state("halted"):
        return no("conta de memes parada pelo kill switch")
    if mint in {p["mint"] for p in led.positions()}:
        return None
    if len(led.positions()) >= cfg.MEME_REAL_MAX_POSITIONS:
        return no(f"limite de {cfg.MEME_REAL_MAX_POSITIONS} posições abertas")
    if len(_today_buys(led)) >= cfg.MEME_REAL_MAX_BUYS_DAY:
        return no(f"limite de {cfg.MEME_REAL_MAX_BUYS_DAY} compras por dia")
    if _day_pnl(led) <= -cfg.MEME_REAL_DAILY_LOSS_USD:
        return no(f"perda do dia chegou a −${cfg.MEME_REAL_DAILY_LOSS_USD:.0f}: sem compras até amanhã")
    usd = cfg.MEME_REAL_TRADE_USD
    if led.cash() < usd:
        return no("saldo insuficiente")
    decimals, fee_bps = mint_info(mint)
    if fee_bps:
        return no(f"token cobra taxa de transferência ({fee_bps / 100:.1f}%)")
    sol_px = sol_usd()
    q = quote(SOL, mint, usd / sol_px * LAMPORTS)
    impact = float(q.get("priceImpactPct") or 0) * 100
    if impact > cfg.MEME_REAL_MAX_IMPACT_PCT:
        return no(f"impacto no preço {impact:.1f}% > {cfg.MEME_REAL_MAX_IMPACT_PCT:.0f}%")
    try:
        back = quote(mint, SOL, int(q["outAmount"]))      # can it be sold back? (a token with no sell route is a trap)
    except Exception:
        back = None
    if not back or int(back.get("outAmount", 0)) <= 0:
        return no("o Jupiter não consegue vender esse token de volta")
    return _execute(led, bot, "buy", mint, symbol, q, decimals, sol_px, usd=usd)


def sell(bot: str, mint: str, symbol: str, reason: str) -> dict | None:
    led = ledger()
    pos = next((p for p in led.positions() if p["mint"] == mint), None)
    if not pos:
        return None
    decimals, _ = mint_info(mint)
    atomic = int(round(pos["qty"] * 10 ** decimals)) if dry() else token_atomic(mint)
    q = quote(mint, SOL, atomic)
    return _execute(led, bot, "sell", mint, symbol, q, decimals, sol_usd(), qty=pos["qty"], reason=reason,
                    oid=f"exit-{pos['entry_order_id']}")


def _execute(led, bot, side, mint, symbol, q, decimals, sol_px, usd=0.0, qty=0.0, reason="", oid=None) -> dict:
    tx = swap_tx(q)
    if dry():
        err = simulate(tx)
        sig, how = None, f"ensaio · simulado na Solana: {'ok' if not err else 'falharia (' + err[:80] + ')'}"
    else:
        sig, how = send(tx), "real"
    impact = float(q.get("priceImpactPct") or 0)
    if side == "buy":
        qty = int(q["outAmount"]) / 10 ** decimals
        gross = usd
    else:
        gross = int(q["outAmount"]) / LAMPORTS * sol_px
    fill, _ = led.append_fill(dict(
        order_id=oid or f"memereal-{bot}-{mint[:8]}-{int(time.time() * 1000)}", ts=now_iso(), venue="solana", side=side,
        mint=mint, symbol=symbol, strategy=bot, qty=qty, decimals=decimals, usd_gross=gross, fee_usd=0.0,
        price=gross / qty if qty else 0, quote_price=gross / qty if qty else 0, price_impact_pct=impact * 100,
        slippage_bps=cfg.MEME_REAL_SLIPPAGE_BPS, route=" → ".join(r["swapInfo"].get("label", "?") for r in q.get("routePlan", [])),
        stop_price=None, tp_price=None, meta=json.dumps({"reason": reason, "how": how, "tx": sig})))
    truth = led.positions_from_fills()
    for m in {p["mint"] for p in led.positions()} - set(truth):
        led.delete_position(m)
    for m, t in truth.items():
        led.upsert_position({**t, "mark_price": None, "mark_value": None})
    led.log_decision("entry" if side == "buy" else "exit", mint=mint, symbol=symbol, strategy=bot, order_id=fill["order_id"],
                     reason=f"{'compra' if side == 'buy' else 'venda'} ${gross:.2f} · {how}" + (f" · {reason}" if reason else ""))
    return fill


def mirror(holdings: dict[str, dict[str, str]], titulars: set[str], marks: dict[str, float]):
    """Once per meme round. holdings = {bot: {mint: symbol}} of the lab. Buys what an eligible bot just bought, sells
    what its bot sold; then the kill switch. Eligible: every meme bot in the dry run, only the titulars for real."""
    if not cfg.MEME_REAL_ENABLED or (not dry() and not ledger().get_state("owner")):
        return
    led = ledger()
    held = {p["mint"]: p for p in led.positions()}
    for mint, p in held.items():
        if mint not in holdings.get(p["strategy"], {}):
            _safe(sell, p["strategy"], mint, p["symbol"], "o bot do laboratório vendeu")
        elif mint in marks:
            led.set_mark(mint, marks[mint], marks[mint] * p["qty"])
    seen = led.get_state("seen") or {}
    for bot, mints in holdings.items():
        if not dry() and bot not in titulars:
            continue
        for mint, sym in mints.items():
            key = f"{bot}:{mint}"
            if key not in seen:                           # only fresh entries: never buy a lab position hours late
                seen[key] = now_iso()
                if led.get_state("primed"):
                    _safe(buy, bot, mint, sym)
    led.set_state("seen", {k: v for k, v in seen.items() if k.split(":", 1)[1] in {m for ms in holdings.values() for m in ms}})
    led.set_state("primed", True)
    eq = equity(led)
    if not led.get_state("halted") and led.contributed() and eq < cfg.MEME_REAL_KILL_PCT * led.contributed():
        led.set_state("halted", True)
        for p in led.positions():
            _safe(sell, p["strategy"], p["mint"], p["symbol"], "kill switch")
        led.log_decision("veto", reason=f"🛑 kill switch: patrimônio ${eq:.2f} < {cfg.MEME_REAL_KILL_PCT:.0%} do depositado")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        logger.warning(f"meme real {fn.__name__} {a[:3]}: {e}")
        ledger().log_decision("veto", mint=a[1] if len(a) > 1 else None, strategy=a[0], reason=f"erro: {str(e)[:160]}")


def equity(led: Ledger | None = None) -> float:
    led = led or ledger()
    return led.cash() + sum(p["mark_value"] if p["mark_value"] is not None else p["cost_usd"] for p in led.positions())


# ─── owner (Phantom): connect, deposit, withdraw ───
def owner_message(owner: str) -> str:
    return f"Kairos: autorizo a carteira de memes {address()} a sacar somente para {owner}"


def set_owner(owner: str, signature_b64: str) -> str:
    """Phantom signed owner_message(owner): proof that the person connecting controls that wallet."""
    pk = Pubkey.from_string(owner)
    if not Signature.from_bytes(base64.b64decode(signature_b64)).verify(pk, owner_message(owner).encode()):
        raise ValueError("assinatura da Phantom não confere")
    ledger().set_state("owner", owner)
    ledger().log_decision("owner", reason=f"Phantom conectada: {owner}")
    return owner


def latest_blockhash() -> str:
    return rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]


def record_deposit(sig: str) -> float:
    """Count a deposit (signed in Phantom) only after checking on Solana that it moved SOL from the owner to the bot."""
    led, owner = ledger(), ledger().get_state("owner")
    if any((d.get("note") or "").endswith(sig) for d in led.cash_movements(500)):
        return 0.0
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
    if not tx or tx["meta"].get("err"):
        raise ValueError("depósito ainda não confirmado")
    lamports = sum(int(i["parsed"]["info"]["lamports"]) for i in tx["transaction"]["message"]["instructions"]
                   if i.get("program") == "system" and i.get("parsed", {}).get("type") == "transfer"
                   and i["parsed"]["info"]["source"] == owner and i["parsed"]["info"]["destination"] == address())
    if not lamports:
        raise ValueError("essa transação não é um depósito da sua Phantom para a carteira de memes")
    usd = lamports / LAMPORTS * sol_usd()
    if not dry():
        led.move_cash("deposit", usd, f"depósito Phantom {sig}")
    led.log_decision("deposit", reason=f"depósito de {lamports / LAMPORTS:.4f} SOL (${usd:.2f}) confirmado")
    return usd


def withdraw() -> str:
    """Sell every meme position, then send all SOL back to the owner's Phantom (the only allowed destination)."""
    led, owner = ledger(), ledger().get_state("owner")
    if not owner:
        raise ValueError("conecte a Phantom primeiro")
    for p in led.positions():
        _safe(sell, p["strategy"], p["mint"], p["symbol"], "saque")
    lamports = rpc("getBalance", [address()])["value"] - FEE_LAMPORTS
    if lamports <= 0:
        raise ValueError("carteira de memes sem saldo")
    bh = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([transfer(TransferParams(from_pubkey=keypair().pubkey(), to_pubkey=Pubkey.from_string(owner),
                                                              lamports=lamports))], keypair().pubkey(), bh)
    sig = send(Transaction([keypair()], msg, bh))
    usd = lamports / LAMPORTS * sol_usd()
    if not dry():
        led.move_cash("withdraw", min(usd, led.cash()), f"saque para Phantom {sig}", check=False)
    led.log_decision("withdraw", reason=f"saque de {lamports / LAMPORTS:.4f} SOL (${usd:.2f}) para {owner}")
    return sig


_bal: tuple[float, float | None, float | None] = (0.0, None, None)   # (read at, sol, sol_usd): the page polls every 30s


def status() -> dict:
    global _bal
    led = ledger()
    if time.time() - _bal[0] > 25:
        try:
            _bal = (time.time(), sol_balance(), sol_usd())
        except Exception as e:
            _bal = (time.time(), None, None)
            logger.warning(f"meme wallet balance: {e}")
    _, sol, px = _bal
    trades = led.closed_trades()
    return {"address": address(), "owner": led.get_state("owner"), "mode": "ensaio" if dry() else "real",
            "halted": bool(led.get_state("halted")), "sol": sol, "sol_usd": px, "usd": round(sol * px, 2) if sol is not None and px else None,
            "equity": round(equity(led), 2), "deposited": round(led.contributed(), 2), "pnl": round(sum(t["pnl_usd"] for t in trades), 4),
            "trades": len(trades), "wins": sum(t["pnl_usd"] > 0 for t in trades), "buys_today": len(_today_buys(led)),
            "limits": {"trade_usd": cfg.MEME_REAL_TRADE_USD, "max_positions": cfg.MEME_REAL_MAX_POSITIONS,
                       "max_buys_day": cfg.MEME_REAL_MAX_BUYS_DAY, "daily_loss_usd": cfg.MEME_REAL_DAILY_LOSS_USD,
                       "max_impact_pct": cfg.MEME_REAL_MAX_IMPACT_PCT, "slippage_pct": cfg.MEME_REAL_SLIPPAGE_BPS / 100,
                       "kill_pct": cfg.MEME_REAL_KILL_PCT},
            "positions": [{"symbol": p["symbol"], "bot": p["strategy"], "cost": round(p["cost_usd"], 2),
                           "now": round(p["mark_value"] if p["mark_value"] is not None else p["cost_usd"], 2)} for p in led.positions()],
            "log": [{"ts": d["ts"], "kind": d["kind"], "symbol": d.get("symbol"), "bot": d.get("strategy"), "reason": d["reason"]}
                    for d in led.decisions(15)]}
