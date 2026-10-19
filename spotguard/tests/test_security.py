from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW
from fixtures_security import (
    EVM_TOKEN,
    RAYDIUM,
    SOL_MINT,
    SOL_POOL,
    evm_rpc,
    goplus_evm,
    goplus_sol,
    pool,
    registry,
    sell,
    sol_holders,
    sol_mint,
)

from spotguard.identities import DexAssetId, IdentityError, UnsupportedChain
from spotguard.security.evm import evaluate_evm
from spotguard.security.identity import CanonicalEntry, CanonicalRegistry
from spotguard.security.schema import Verdict, flag01, number, ratio, timestamp
from spotguard.security.screener import Screener
from spotguard.security.solana import evaluate_solana

EVM = DexAssetId("evm:1", EVM_TOKEN)
SOL = DexAssetId("solana", SOL_MINT)


def evm(policy, goplus=None, rpc=None, pl=None, sl=None, symbol="TEST", reg=None, now=NOW):
    return evaluate_evm(
        EVM, symbol, policy.screening, policy.version, now,
        goplus if goplus is not None else goplus_evm(),
        rpc if rpc is not None else evm_rpc(),
        pl if pl is not None else pool(),
        sl if sl is not None else sell(),
        reg or registry(),
    )  # fmt: skip


def sol(policy, goplus=None, mint=None, holders=None, pl=None, sl=None, symbol="RAY", now=NOW):
    return evaluate_solana(
        SOL, symbol, policy.screening, policy.version, now,
        goplus if goplus is not None else goplus_sol(),
        mint if mint is not None else sol_mint(),
        holders if holders is not None else sol_holders(),
        pl if pl is not None else pool(SOL_POOL, RAYDIUM),
        sl if sl is not None else sell(),
        registry(),
    )  # fmt: skip


# ----- strict parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "true", "no", "", " ", None, True, False, "01", "1.0", 2, [], {}])
def test_false_like_and_malformed_flags_are_unknown(value):
    assert flag01(value) is None


def test_flags_accept_only_zero_and_one():
    assert flag01("1") is True and flag01("0") is False and flag01(1) is True and flag01(0) is False


@pytest.mark.parametrize("value", ["NaN", "nan", float("nan"), "inf", "-0.1", -1, "abc", None, True, "1.5"])
def test_bad_ratios_are_unknown(value):
    assert ratio(value) is None


def test_numbers_reject_negative_and_nonfinite():
    assert number("12.5") == Decimal("12.5")
    assert number("-3") is None and number("Infinity") is None and number(float("nan")) is None


def test_future_and_garbage_timestamps_rejected():
    assert timestamp(str(int((NOW + timedelta(days=1)).timestamp())), NOW) is None
    assert timestamp("not-a-date", NOW) is None
    assert timestamp("2026-10-01T00:00:00", NOW) is None  # naive: no timezone
    assert timestamp("2026-10-01T00:00:00Z", NOW) is not None
    assert timestamp(str(int(NOW.timestamp()) * 1000), NOW) == NOW


# ----- EVM ----------------------------------------------------------------------------------------


def test_evm_all_checks_pass_policy(policy):
    report = evm(policy)
    assert report.verdict is Verdict.PASS_POLICY, report.reasons()
    ok, why = report.allows_entry(NOW, policy.screening.max_evidence_age_s)
    assert ok and "not a safety guarantee" in why


def test_evm_honeypot_blocks(policy):
    assert evm(policy, goplus=goplus_evm(is_honeypot="1")).verdict is Verdict.BLOCK


@pytest.mark.parametrize("value", [..., "false", "", None, "yes"])
def test_evm_missing_or_false_like_honeypot_flag_is_unknown(policy, value):
    report = evm(policy, goplus=goplus_evm(is_honeypot=value))
    assert report.verdict is Verdict.UNKNOWN
    assert any("is_honeypot" in r for r in report.reasons())


def test_evm_sell_tax_nan_or_negative_is_unknown_and_high_tax_blocks(policy):
    assert evm(policy, goplus=goplus_evm(sell_tax="NaN")).verdict is Verdict.UNKNOWN
    assert evm(policy, goplus=goplus_evm(sell_tax="-0.01")).verdict is Verdict.UNKNOWN
    assert evm(policy, goplus=goplus_evm(sell_tax="0.05")).verdict is Verdict.BLOCK


def test_evm_scanner_outage_is_unknown(policy):
    report = evaluate_evm(EVM, "TEST", policy.screening, policy.version, NOW, None, evm_rpc(), pool(), sell(), registry())
    assert report.verdict is Verdict.UNKNOWN


def test_evm_provider_error_code_is_unknown(policy):
    from spotguard.security.schema import Evidence

    ev = Evidence("goplus", "x", NOW, {"code": 2004, "message": "rate limited", "result": {}})
    assert evm(policy, goplus=ev).verdict is Verdict.UNKNOWN


def test_evm_providers_disagree_on_proxy(policy):
    report = evm(policy, rpc=evm_rpc(implementation="0x" + "ab" * 20))
    assert report.verdict is Verdict.BLOCK
    assert any("disagree" in r or "provider says not a proxy" in r for r in report.reasons(Verdict.UNKNOWN))


def test_evm_privileged_owner_is_unknown(policy):
    report = evm(policy, goplus=goplus_evm(owner_address="0x" + "cd" * 20))
    assert report.verdict is Verdict.UNKNOWN


def test_evm_concentrated_holders_block(policy):
    whales = [{"address": f"0x{i:040x}", "percent": "0.05", "is_locked": 0} for i in range(3, 13)]
    assert evm(policy, goplus=goplus_evm(holders=whales)).verdict is Verdict.BLOCK


def test_evm_lp_lock_expiring_soon_does_not_count(policy):
    soon = int((NOW + timedelta(days=3)).timestamp())
    lp = [{"address": "0x" + "ef" * 20, "percent": "0.99", "is_locked": 1, "locked_detail": [{"end_time": str(soon)}]}]
    report = evm(policy, goplus=goplus_evm(lp_holders=lp))
    assert report.verdict is Verdict.BLOCK and any("lp_ownership" in r for r in report.reasons())


def test_evm_concentrated_liquidity_needs_own_analysis(policy):
    report = evm(policy, goplus=goplus_evm(dex=[{"name": "UniswapV3", "liquidity": "1e6", "pair": "0x" + "22" * 20}]))
    assert report.verdict is not Verdict.PASS_POLICY


def test_evm_suspected_honeypot_by_simulation(policy):
    assert evm(policy, sl=sell(ok=False)).verdict is Verdict.BLOCK
    assert evm(policy, sl=sell(out="70")).verdict is Verdict.BLOCK


def test_evm_young_or_thin_pool_blocks(policy):
    assert evm(policy, pl=pool(age_days=2)).verdict is Verdict.BLOCK
    assert evm(policy, pl=pool(liquidity="50000")).verdict is Verdict.BLOCK


def test_evm_pool_not_in_provider_list_is_unknown(policy):
    assert evm(policy, pl=pool(address="0x" + "99" * 20)).verdict is Verdict.UNKNOWN


def test_evm_unimplemented_independent_checks_are_unknown(policy):
    report = evaluate_evm(EVM, "TEST", policy.screening, policy.version, NOW, goplus_evm(), evm_rpc(), None, None,
                          registry())  # fmt: skip
    assert report.verdict is Verdict.UNKNOWN
    assert {"pool", "sell_simulation"} <= {f.check for f in report.findings if f.status is Verdict.UNKNOWN}


# ----- identity ------------------------------------------------------------------------------------


def test_lookalike_symbol_is_blocked(policy):
    fake = CanonicalRegistry([CanonicalEntry("evm:1", "TEST", "0x" + "aa" * 20, "fixture", "t", "2026-10-01")])
    report = evm(policy, reg=fake)
    assert report.verdict is Verdict.BLOCK and any("lookalike" in r for r in report.reasons())


def test_ticker_match_without_reviewed_reference_is_unknown(policy):
    report = evm(policy, reg=CanonicalRegistry([]))
    assert report.verdict is Verdict.UNKNOWN


def test_symbol_disagreement_between_sources_is_unknown(policy):
    report = evm(policy, rpc=evm_rpc(symbol="TESTX"))
    assert report.verdict is Verdict.UNKNOWN


def test_registry_rejects_entries_without_provenance(tmp_path):
    p = tmp_path / "reg.toml"
    p.write_text('[[token]]\nchain = "evm:1"\nsymbol = "X"\naddress = "0x' + "11" * 20 + '"\nreviewed_by = "a"\n'
                 'reviewed_at = "2026"\n')  # fmt: skip
    with pytest.raises(ValueError):
        CanonicalRegistry.load(p)


def test_identity_types_reject_collisions_and_bad_input():
    with pytest.raises(IdentityError):
        DexAssetId("evm:1", "0xdAC17F958D2ee523a2206206994597C13D831ec8")  # bad checksum
    with pytest.raises(IdentityError):
        DexAssetId("solana", SOL_MINT.lower())  # base58 is case-sensitive
    with pytest.raises(UnsupportedChain):
        DexAssetId("bitcoin", "bc1qxyz")
    a = DexAssetId("evm:1", EVM_TOKEN.upper().replace("0X", "0x"))
    assert a == DexAssetId("evm:1", EVM_TOKEN)


# ----- Solana -----------------------------------------------------------------------------------------


def test_solana_all_checks_pass_policy(policy):
    report = sol(policy)
    assert report.verdict is Verdict.PASS_POLICY, report.reasons()


def test_solana_active_authorities_block(policy):
    assert sol(policy, mint=sol_mint(mint_authority="Dev111")).verdict is Verdict.BLOCK
    assert sol(policy, mint=sol_mint(freeze_authority="Dev111")).verdict is Verdict.BLOCK


def test_solana_revoked_authority_but_provider_disagrees_is_not_pass(policy):
    report = sol(policy, goplus=goplus_sol(mintable={"status": "1", "authority": [{"address": "x"}]}))
    assert report.verdict is Verdict.UNKNOWN
    assert any("disagree" in r for r in report.reasons())


@pytest.mark.parametrize(
    "ext",
    [
        {"extension": "permanentDelegate", "state": {"delegate": "Del111"}},
        {"extension": "nonTransferable", "state": {}},
        {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
        {"extension": "transferHook", "state": {"programId": "Hook111", "authority": None}},
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500},
                                                      "olderTransferFee": {"transferFeeBasisPoints": 500}}},
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 0},
                                                      "olderTransferFee": {"transferFeeBasisPoints": 0},
                                                      "transferFeeConfigAuthority": "Auth111"}},
    ],
)  # fmt: skip
def test_solana_dangerous_token2022_extensions_block(policy, ext):
    from spotguard.security.solana import TOKEN_2022_PROGRAM

    assert sol(policy, mint=sol_mint(TOKEN_2022_PROGRAM, extensions=(ext,))).verdict is Verdict.BLOCK


def test_solana_unreviewed_extension_is_unknown(policy):
    from spotguard.security.solana import TOKEN_2022_PROGRAM

    report = sol(policy, mint=sol_mint(TOKEN_2022_PROGRAM, extensions=({"extension": "confidentialTransferMint"},)))
    assert report.verdict is Verdict.UNKNOWN


def test_solana_wrong_program_blocks(policy):
    assert sol(policy, mint=sol_mint(program="11111111111111111111111111111111")).verdict is Verdict.BLOCK


def test_solana_missing_provider_fields_are_unknown_not_false(policy):
    assert sol(policy, goplus=goplus_sol(closable=...)).verdict is Verdict.UNKNOWN
    assert sol(policy, goplus=goplus_sol(none_transferable="false")).verdict is Verdict.UNKNOWN


def test_solana_holder_concentration_excludes_pool_vaults(policy):
    assert sol(policy, holders=sol_holders(pct_each=Decimal("0.01"), pool_share=Decimal("0.8"))).verdict is Verdict.PASS_POLICY
    assert sol(policy, holders=sol_holders(pct_each=Decimal("0.03"))).verdict is Verdict.BLOCK


def test_solana_unreviewed_pool_program_blocks(policy):
    assert sol(policy, pl=pool(SOL_POOL, program="Fake11111111111111111111111111111")).verdict is Verdict.BLOCK


def test_solana_concentrated_pool_is_unknown(policy):
    assert sol(policy, pl=pool(SOL_POOL, program="whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc")).verdict is Verdict.UNKNOWN


# ----- freshness ------------------------------------------------------------------------------------


def test_stale_evidence_blocks_entry(policy):
    report = evm(policy)
    later = NOW + timedelta(seconds=float(policy.screening.max_evidence_age_s) + 1)
    ok, why = report.allows_entry(later, policy.screening.max_evidence_age_s)
    assert not ok and "older" in why


def test_future_dated_evidence_is_not_fresh(policy):
    report = evm(policy)
    ok, _ = report.allows_entry(NOW - timedelta(minutes=5), policy.screening.max_evidence_age_s)
    assert not ok


# ----- screener orchestration -------------------------------------------------------------------------


class SlowGoPlus:
    async def evm_token(self, chain_id, address, now):
        await asyncio.sleep(5)

    async def solana_token(self, mint, now):
        raise RuntimeError("HTTP 503")


async def test_screener_timeouts_and_errors_become_unknown(policy):
    s = Screener(policy, None, registry(), goplus=SlowGoPlus(), timeout_s=0.05)
    assert (await s.screen("evm:1", EVM_TOKEN, "TEST", NOW)).verdict is Verdict.UNKNOWN
    assert (await s.screen("solana", SOL_MINT, "RAY", NOW)).verdict is Verdict.UNKNOWN


async def test_screener_unsupported_chain_is_unknown(policy):
    s = Screener(policy, None, registry())
    report = await s.screen("bitcoin", "bc1q0000", "BTC", NOW)
    assert report.verdict is Verdict.UNKNOWN
    assert "Unsupported chain" in report.reasons()[0]


async def test_screener_chain_not_in_pinned_coverage_is_unknown(policy):
    class GP:
        async def evm_token(self, chain_id, address, now):
            return goplus_evm()

    s = Screener(policy, None, registry(), goplus=GP(), goplus_chains={"56"})
    report = await s.screen("evm:1", EVM_TOKEN, "TEST", NOW)
    assert report.verdict is Verdict.UNKNOWN


async def test_screener_persists_evidence_and_report(policy, tmp_path):
    from spotguard.storage import Store

    class GP:
        async def evm_token(self, chain_id, address, now):
            return goplus_evm(is_honeypot="1")

    store = Store(tmp_path / "db.sqlite")
    s = Screener(policy, store, registry(), goplus=GP())
    await s.screen("evm:1", EVM_TOKEN, "TEST", NOW)
    row = store.query("SELECT verdict, findings FROM security_reports")[0]
    assert row["verdict"] == "BLOCK" and "is_honeypot" in row["findings"]
    assert store.query("SELECT count(*) FROM security_evidence")[0][0] >= 1


def test_solana_unburned_lp_blocks_and_missing_is_unknown(policy):
    dex = [{"type": "standard", "tvl": "900000", "id": SOL_POOL, "burn_percent": 12.0}]
    assert sol(policy, goplus=goplus_sol(dex=dex)).verdict is Verdict.BLOCK
    assert sol(policy, goplus=goplus_sol(dex=...)).verdict is Verdict.UNKNOWN
    cl = [{"type": "concentrated", "tvl": "900000", "id": SOL_POOL}]
    assert sol(policy, goplus=goplus_sol(dex=cl)).verdict is Verdict.UNKNOWN


def test_evm_holder_concentration_excludes_pools_before_taking_top_ten(policy):
    """Regression: slicing the first ten entries before excluding pools undercounted concentration."""
    from fixtures_security import EVM_PAIR

    locked = [{"address": EVM_PAIR, "percent": "0.30", "is_locked": 0}] + [
        {"address": f"0x{i:040x}", "percent": "0.05", "is_locked": 1} for i in range(100, 109)
    ]
    wallets = [{"address": f"0x{i:040x}", "percent": "0.03", "is_locked": 0} for i in range(3, 13)]
    report = evm(policy, goplus=goplus_evm(holders=locked + wallets))  # 10 wallets x 3% = 30% > 20%
    assert any(f.check == "holder_concentration" and f.status is Verdict.BLOCK for f in report.findings)
