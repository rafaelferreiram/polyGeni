"""
Read-only market data from public endpoints: Jupiter quotes, DexScreener pairs,
GeckoTerminal candles, Solana JSON-RPC (mint info, holders). No signing, ever.
"""
import logging
import threading
import time
from urllib.parse import urlparse

import httpx

from src.solana import config as cfg

logger = logging.getLogger("solana.data")

# Minimum seconds between requests per host (documented limits: DexScreener 60-300/min,
# GeckoTerminal ~30/min, public Solana RPC is aggressive with 429s).
_MIN_INTERVAL = {
    "api.dexscreener.com": 1.1,
    "api.geckoterminal.com": 2.1,
    "api.mainnet-beta.solana.com": 0.5,
    "api.jup.ag": 1.0,
    "api.hyperliquid.xyz": 0.6,           # weight-based limit per IP; heavy history paging triggers 429s
    "api.hyperliquid-testnet.xyz": 0.6,
}
_last_call: dict[str, float] = {}
_lock = threading.Lock()


def _throttle(url: str):
    host = urlparse(url).hostname or ""
    gap = _MIN_INTERVAL.get(host, 0.2)
    with _lock:
        wait = _last_call.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def request_json(method: str, url: str, retries: int = 3, timeout: float = 10, **kw):
    """HTTP with per-host rate limit, timeout and exponential backoff on 429/5xx/network errors."""
    last_err = None
    for attempt in range(retries):
        _throttle(url)
        try:
            resp = httpx.request(method, url, timeout=timeout, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code} from {url}")
            else:
                resp.raise_for_status()
                return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_err = e
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                break  # 4xx (other than 429) won't fix itself
        time.sleep(0.8 * 2 ** attempt)
    raise RuntimeError(f"request failed: {url}: {last_err}")


# ─── Jupiter ─────────────────────────────────────────────────────────────────

def jupiter_quote(input_mint: str, output_mint: str, amount_atomic: int,
                  slippage_bps: int = cfg.PAPER_QUOTE_SLIPPAGE_BPS) -> dict:
    headers = {"x-api-key": cfg.JUPITER_API_KEY} if cfg.JUPITER_API_KEY else {}
    return request_json("GET", cfg.JUPITER_QUOTE_URL, retries=5, headers=headers, params={
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": int(amount_atomic),
        "slippageBps": slippage_bps,
        "restrictIntermediateTokens": "true",
    })


def sol_usd() -> float:
    q = jupiter_quote(cfg.SOL_MINT, cfg.USDC_MINT, 10**9)
    return int(q["outAmount"]) / 1e6


# ─── DexScreener ─────────────────────────────────────────────────────────────

def dexscreener_boosted_solana() -> list[str]:
    """Token addresses currently trending/boosted on Solana (attention ≠ quality)."""
    out = []
    for path in ("/token-boosts/top/v1", "/token-profiles/latest/v1"):
        try:
            rows = request_json("GET", cfg.DEXSCREENER_BASE + path)
            out += [r["tokenAddress"] for r in rows if r.get("chainId") == "solana"]
        except Exception as e:
            logger.warning(f"dexscreener {path}: {e}")
    return list(dict.fromkeys(out))


def dexscreener_pairs(token_addresses: list[str]) -> list[dict]:
    """Pairs for up to 30 tokens per call."""
    pairs = []
    for i in range(0, len(token_addresses), 30):
        chunk = ",".join(token_addresses[i:i + 30])
        pairs += request_json("GET", f"{cfg.DEXSCREENER_BASE}/tokens/v1/solana/{chunk}") or []
    return pairs


# ─── GeckoTerminal candles ───────────────────────────────────────────────────

def pool_candles(pool_address: str, timeframe: str = "hour", limit: int = 400) -> list[dict]:
    """OHLCV oldest→newest as dicts {t, open, high, low, close, volume}."""
    data = request_json(
        "GET", f"{cfg.GECKOTERMINAL_BASE}/networks/solana/pools/{pool_address}/ohlcv/{timeframe}",
        params={"limit": limit, "currency": "usd"},
    )
    rows = data["data"]["attributes"]["ohlcv_list"]
    return [dict(t=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in reversed(rows)]


def binance_candles(symbol: str, interval: str = "1h", start_ms: int | None = None,
                    end_ms: int | None = None) -> list[dict]:
    """Paged Binance klines (public) for backtests, oldest→newest."""
    out, cursor = [], start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if cursor:
            params["startTime"] = cursor
        if end_ms:
            params["endTime"] = end_ms
        rows = request_json("GET", "https://api.binance.com/api/v3/klines", params=params)
        out += [dict(t=r[0] // 1000, open=float(r[1]), high=float(r[2]), low=float(r[3]),
                     close=float(r[4]), volume=float(r[7])) for r in rows]
        if len(rows) < 1000 or start_ms is None:
            return out
        cursor = rows[-1][0] + 1


# ─── Solana RPC (read-only) ──────────────────────────────────────────────────

def rpc(method: str, params: list):
    data = request_json("POST", cfg.SOLANA_RPC_URL, retries=4,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in data:
        raise RuntimeError(f"rpc {method}: {data['error']}")
    return data["result"]


def mint_info(mint: str) -> dict:
    """{program, decimals, supply, mintAuthority, freezeAuthority, extensions}"""
    value = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
    if not value or value["data"].get("parsed", {}).get("type") != "mint":
        raise RuntimeError(f"{mint} is not a parsed mint account")
    info = value["data"]["parsed"]["info"]
    return {
        "program": value["data"]["program"],
        "decimals": info["decimals"],
        "supply": int(info["supply"]),
        "mintAuthority": info.get("mintAuthority"),
        "freezeAuthority": info.get("freezeAuthority"),
        "extensions": [e.get("extension") for e in info.get("extensions", [])],
    }


def jupiter_token_info(mint: str) -> dict | None:
    """Jupiter Tokens API record: audit, organicScore, holderCount, stats5m/1h/6h/24h, tags."""
    rows = request_json("GET", cfg.JUPITER_TOKENS_URL, params={"query": mint})
    return next((r for r in rows if r.get("id") == mint), None)


def top_holders_pct(mint: str) -> float:
    """Fraction of supply held by the top holders. Jupiter Tokens API audit first (public RPCs
    block getTokenLargestAccounts), then on-chain top-10 as fallback. Raises if neither answers."""
    try:
        audit = (jupiter_token_info(mint) or {}).get("audit") or {}
        if audit.get("topHoldersPercentage") is not None:
            return float(audit["topHoldersPercentage"]) / 100
    except Exception as e:
        logger.warning(f"jupiter token audit {mint}: {e}")
    supply = int(rpc("getTokenSupply", [mint])["value"]["amount"])
    amounts = sorted((int(a["amount"]) for a in rpc("getTokenLargestAccounts", [mint])["value"]), reverse=True)
    if supply <= 0 or not amounts:
        raise RuntimeError("holder data missing")
    return sum(amounts[:10]) / supply


def wallet_balances(owner: str) -> dict:
    """Watch-only: SOL + SPL token balances of a PUBLIC address. Never needs a key."""
    sol = rpc("getBalance", [owner])["value"] / 1e9
    tokens = []
    for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        res = rpc("getTokenAccountsByOwner", [owner, {"programId": program}, {"encoding": "jsonParsed"}])
        for acc in res["value"]:
            info = acc["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"].get("uiAmount") or 0
            if amt:
                tokens.append({"mint": info["mint"], "amount": amt})
    return {"address": owner, "sol": sol, "tokens": tokens}


def evm_wallet_balances(address: str) -> dict:
    """Watch-only: Arbitrum ETH + native USDC, and Hyperliquid account value (mainnet + testnet). Public reads only."""
    def rpc_evm(method, params):
        r = request_json("POST", cfg.ARBITRUM_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RuntimeError(r["error"])
        return r["result"]
    eth = int(rpc_evm("eth_getBalance", [address, "latest"]), 16) / 1e18
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")      # balanceOf(address)
    usdc = int(rpc_evm("eth_call", [{"to": cfg.ARBITRUM_USDC, "data": data}, "latest"]), 16) / 1e6
    out = {"address": address, "chain": "evm", "arbitrum_eth": eth, "arbitrum_usdc": usdc}
    for key, url in (("hyperliquid", cfg.HYPERLIQUID_INFO_URL), ("hyperliquid_testnet", cfg.HYPERLIQUID_TESTNET_INFO_URL)):
        try:
            st = request_json("POST", url, json={"type": "clearinghouseState", "user": address})
            spot = request_json("POST", url, json={"type": "spotClearinghouseState", "user": address})
            out[key] = {"account_value": float(st["marginSummary"]["accountValue"]),
                        "withdrawable": float(st.get("withdrawable") or 0),
                        "positions": len(st.get("assetPositions") or []),
                        "spot": [{"coin": b["coin"], "total": float(b["total"])} for b in spot.get("balances", [])
                                 if float(b["total"])]}
        except Exception as e:
            out[key] = {"error": str(e)[:200]}
    return out
