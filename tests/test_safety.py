import time

from src.solana.agents import SafetyAgent
from tests.conftest import candidate

GOOD_MINT = {"program": "spl-token", "decimals": 6, "supply": 10**15, "mintAuthority": None,
             "freezeAuthority": None, "extensions": []}


def agent(mint=GOOD_MINT, holders=0.2, **kw):
    def mint_fn(_):
        if isinstance(mint, Exception):
            raise mint
        return mint

    def holders_fn(_):
        if isinstance(holders, Exception):
            raise holders
        return holders
    return SafetyAgent(mint_info_fn=mint_fn, holders_fn=holders_fn, min_age_h=24, min_liq=100_000,
                       max_top10=0.6, cache_ttl_s=0, **kw)


def test_passes_when_everything_verified():
    c = candidate(decimals=None)
    v = agent().check(c)
    assert v.approve and c.decimals == 6


def test_rejects_no_liquidity():
    assert not agent().check(candidate(liquidity_usd=0)).approve
    assert not agent().check(candidate(liquidity_usd=50_000)).approve


def test_rejects_new_pair_no_sniping():
    v = agent().check(candidate(pair_created_at_ms=(time.time() - 3600) * 1000))
    assert not v.approve and "too new" in v.reason


def test_rejects_active_authorities():
    assert "mint authority" in agent(mint={**GOOD_MINT, "mintAuthority": "Dev111"}).check(candidate()).reason
    assert "freeze authority" in agent(mint={**GOOD_MINT, "freezeAuthority": "Dev111"}).check(candidate()).reason


def test_rejects_risky_token2022_extensions():
    v = agent(mint={**GOOD_MINT, "extensions": ["tokenMetadata", "permanentDelegate"]}).check(candidate())
    assert not v.approve and "permanentDelegate" in v.reason


def test_rejects_concentrated_holders():
    assert not agent(holders=0.9).check(candidate()).approve


def test_rejects_missing_or_unverifiable_data():
    assert not agent().check(candidate(pair_created_at_ms=None)).approve
    assert not agent(mint=RuntimeError("rpc 429")).check(candidate()).approve
    assert not agent(mint={"decimals": 6}).check(candidate()).approve          # authorities missing
    assert not agent(holders=RuntimeError("blocked")).check(candidate()).approve
    assert not agent(holders=None).check(candidate()).approve
