import time

import pytest

from src.solana import config as cfg
from src.solana.agents import Candidate
from src.solana.ledger import Ledger

TOKEN = "TokenMint1111111111111111111111111111111111"


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "paper.db"), 100.0)


def make_quote(price_usd_per_token: float, decimals: int = 6, impact: str = "0.001"):
    """Fake Jupiter quote fn: deterministic price, no network."""
    def quote(inp, out, amount):
        if inp == cfg.USDC_MINT:   # buy: USDC atomic -> token atomic
            out_amt = amount / 1e6 / price_usd_per_token * 10**decimals
        else:                      # sell: token atomic -> USDC atomic
            out_amt = amount / 10**decimals * price_usd_per_token * 1e6
        return {"outAmount": str(int(out_amt)), "priceImpactPct": impact,
                "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
    return quote


def candidate(**kw):
    base = dict(mint=TOKEN, symbol="TKN", pair_address="Pool111", dex="raydium", price_usd=1.0,
                liquidity_usd=1_000_000, volume_h24=2_000_000,
                pair_created_at_ms=(time.time() - 30 * 86400) * 1000, decimals=6)
    base.update(kw)
    return Candidate(**base)


def flat_candles(n=60, price=1.0, vol=100.0):
    return [dict(t=i * 3600, open=price, high=price * 1.001, low=price * 0.999, close=price, volume=vol)
            for i in range(n)]
