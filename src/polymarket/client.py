"""
Polymarket CLOB client wrapper.
Handles authentication and order operations.
"""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType, PartialCreateOrderOptions
from src.config import CLOB_HOST, POLY_PRIVATE_KEY, CHAIN_ID, POLY_FUNDER_ADDRESS


_client: ClobClient | None = None


def get_client() -> ClobClient:
    global _client
    if _client is None:
        # signature_type=1 for Magic/email proxy wallets (Polymarket default)
        # funder is the proxy contract address that holds the actual USDC
        _client = ClobClient(
            host=CLOB_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=1,
            funder=POLY_FUNDER_ADDRESS,
        )
        # Derive and set L2 API credentials automatically
        creds = _client.create_or_derive_api_creds()
        _client.set_api_creds(creds)
    return _client


def get_balance() -> float:
    """Returns USDC balance available for trading."""
    client = get_client()
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    # Balance is returned as a string in raw USDC units (6 decimals)
    raw = float(bal.get("balance", 0))
    return raw / 1e6 if raw > 1000 else raw  # handle both raw and already-divided


def get_open_positions() -> list[dict]:
    """Returns all open positions."""
    client = get_client()
    return client.get_positions() or []


def get_open_orders() -> list[dict]:
    client = get_client()
    return client.get_open_orders() or []


def place_order(token_id: str, price: float, size_usdc: float, side: str, tick_size: str = "0.01") -> dict:
    """
    Place a limit order.
    size_usdc: how much USDC to spend (we convert to shares internally)
    side: 'BUY' or 'SELL'
    Returns order response dict.
    """
    client = get_client()
    shares = round(size_usdc / price, 2)
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=shares,
        side=side,
    )
    options = PartialCreateOrderOptions(tick_size=tick_size)
    resp = client.create_and_post_order(order_args, options)
    return resp


def cancel_order(order_id: str) -> dict:
    client = get_client()
    return client.cancel(order_id=order_id)


def get_market(condition_id: str) -> dict:
    client = get_client()
    return client.get_market(condition_id=condition_id)


def get_order_book(token_id: str) -> dict:
    client = get_client()
    return client.get_order_book(token_id=token_id)


def get_midpoint(token_id: str) -> float | None:
    """Returns the midpoint price (0-1) for a token."""
    client = get_client()
    try:
        result = client.get_midpoint(token_id=token_id)
        return float(result.get("mid", 0))
    except Exception:
        return None


def get_last_trade_price(token_id: str) -> float | None:
    client = get_client()
    try:
        result = client.get_last_trade_price(token_id=token_id)
        return float(result.get("price", 0))
    except Exception:
        return None
