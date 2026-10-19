"""Screening fixtures shaped like the GoPlus schemas (official SDK v0.2.6) and Solana RPC output.

These are FABRICATED test data, not real attestations about any token.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal

from conftest import NOW

from spotguard.security.facts import (
    EvmRpcFacts,
    PoolFacts,
    SellSimulation,
    SolanaHolder,
    SolanaHolderFacts,
    SolanaMintFacts,
)
from spotguard.security.identity import CanonicalEntry, CanonicalRegistry
from spotguard.security.schema import Evidence
from spotguard.security.solana import SYSTEM_PROGRAM, TOKEN_PROGRAM

EVM_TOKEN = "0x1111111111111111111111111111111111111111"
EVM_PAIR = "0x2222222222222222222222222222222222222222"
SOL_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
SOL_POOL = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

CLEAN_EVM = {
    "token_symbol": "TEST",
    "token_name": "Test",
    "is_open_source": "1",
    **{
        k: "0"
        for k in (
            "is_honeypot",
            "cannot_sell_all",
            "cannot_buy",
            "owner_change_balance",
            "hidden_owner",
            "can_take_back_ownership",
            "selfdestruct",
            "transfer_pausable",
            "is_blacklisted",
            "is_whitelisted",
            "slippage_modifiable",
            "personal_slippage_modifiable",
            "trading_cooldown",
            "is_mintable",
            "external_call",
            "anti_whale_modifiable",
            "is_proxy",
        )
    },
    "honeypot_with_same_creator": "0",
    "buy_tax": "0",
    "sell_tax": "0.01",
    "owner_address": "",
    "is_in_dex": "1",
    "holders": [{"address": EVM_PAIR, "percent": "0.40", "is_locked": 0, "is_contract": 1}]
    + [{"address": f"0x{i:040x}", "percent": "0.015", "is_locked": 0, "is_contract": 0} for i in range(3, 12)],
    "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.99", "is_locked": 0}],
    "dex": [{"name": "UniswapV2", "liquidity": "900000", "pair": EVM_PAIR}],
}


def goplus_evm(**overrides) -> Evidence:
    data = copy.deepcopy(CLEAN_EVM)
    for k, v in overrides.items():
        if v is ...:
            data.pop(k, None)
        else:
            data[k] = v
    return Evidence("goplus", "fixture", NOW, {"code": 1, "message": "OK", "result": {EVM_TOKEN: data}})


def evm_rpc(**kw) -> EvmRpcFacts:
    base = dict(has_code=True, implementation=None, admin=None, decimals=18, symbol="TEST")
    base.update(kw)
    return EvmRpcFacts(Evidence("evm-rpc", "fixture", NOW, base), **base)


def pool(address=EVM_PAIR, program=RAYDIUM, liquidity="900000", age_days=200, concentrated=False) -> PoolFacts:
    return PoolFacts(
        Evidence("pool-fixture", "fixture", NOW, {"pool": address}),
        address,
        program,
        Decimal(liquidity),
        NOW - timedelta(days=age_days),
        concentrated,
    )


def sell(ok=True, out="99") -> SellSimulation:
    return SellSimulation(Evidence("sim-fixture", "fixture", NOW, {}), ok, Decimal("100"), Decimal(out))


def registry() -> CanonicalRegistry:
    return CanonicalRegistry(
        [
            CanonicalEntry("evm:1", "TEST", EVM_TOKEN, "https://example.org/fixture", "tester", "2026-10-01"),
            CanonicalEntry("solana", "RAY", SOL_MINT, "https://example.org/fixture", "tester", "2026-10-01"),
        ]
    )


CLEAN_SOL = {
    "metadata": {"symbol": "RAY", "name": "Raydium"},
    "mintable": {"status": "0", "authority": []},
    "freezable": {"status": "0", "authority": []},
    "closable": {"status": "0", "authority": []},
    "balance_mutable_authority": {"status": "0", "authority": []},
    "transfer_fee_upgradable": {"status": "0", "authority": []},
    "transfer_hook_upgradable": {"status": "0", "authority": []},
    "default_account_state_upgradable": {"status": "0", "authority": []},
    "metadata_mutable": {"status": "0", "metadata_upgrade_authority": []},
    "none_transferable": "0",
    "default_account_state": "1",
    "transfer_hook": [],
    "transfer_fee": {},
    "creators": [],
    "trusted_token": 1,
    "dex": [{"type": "standard", "tvl": "900000", "id": SOL_POOL, "dex_name": "Raydium", "burn_percent": 100.0}],
}


def goplus_sol(**overrides) -> Evidence:
    data = copy.deepcopy(CLEAN_SOL)
    for k, v in overrides.items():
        if v is ...:
            data.pop(k, None)
        else:
            data[k] = v
    return Evidence("goplus", "fixture", NOW, {"code": 1, "message": "OK", "result": {SOL_MINT: data}})


def sol_mint(program=TOKEN_PROGRAM, mint_authority=None, freeze_authority=None, extensions=()) -> SolanaMintFacts:
    raw = {"owner": program}
    return SolanaMintFacts(
        Evidence("solana-rpc", "fixture", NOW, raw),
        program,
        mint_authority,
        freeze_authority,
        6,
        1_000_000_000,
        extensions,
    )


def sol_holders(pct_each=Decimal("0.01"), pool_share=Decimal("0.5")) -> SolanaHolderFacts:
    supply = 1_000_000_000
    holders = [SolanaHolder("vault", "poolauth", RAYDIUM, int(supply * pool_share))]
    holders += [SolanaHolder(f"acct{i}", f"wallet{i}", SYSTEM_PROGRAM, int(supply * pct_each)) for i in range(15)]
    return SolanaHolderFacts(Evidence("solana-rpc", "fixture", NOW, {"supply_raw": supply}), tuple(holders))
