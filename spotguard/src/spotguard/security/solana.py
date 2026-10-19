"""Solana SPL / Token-2022 screening.

Solana semantics are not EVM semantics: risk lives in mint/freeze authorities, Token-2022 extensions
and program ownership, not in contract source code. Authority and extension facts come from the
chain itself (getAccountInfo jsonParsed); GoPlus's Solana endpoint (schema per official SDK v0.2.6,
beta) is a second, independent source and disagreements block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from spotguard.config import ScreeningPolicy
from spotguard.identities import ChainKind, DexAssetId
from spotguard.security.facts import PoolFacts, SellSimulation, SolanaHolderFacts, SolanaMintFacts
from spotguard.security.identity import CanonicalRegistry, SymbolObservation, verify_identity
from spotguard.security.schema import Evidence, FindingSink, SecurityReport, flag01, number

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Pool programs whose vaults are excluded from holder concentration and accepted as pool owners.
# Reviewed list; extend deliberately.
POOL_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("raydium_amm_v4", False),
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ("raydium_cpmm", False),
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": ("raydium_clmm", True),
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": ("orca_whirlpool", True),
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": ("meteora_dlmm", True),
}

# Token-2022 extensions: dangerous ones block; reviewed metadata-only ones pass; anything else is UNKNOWN.
BENIGN_EXTENSIONS = {"metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer", "tokenGroup",
                     "tokenGroupMember"}  # fmt: skip

# GoPlus Solana objects of shape {"status": "1"|"0", "authority": [...]}: "1" means the power exists.
GOPLUS_STATUS_MUST_BE_OFF = {
    "closable": "mint can be closed",
    "balance_mutable_authority": "an authority can change holder balances",
    "transfer_fee_upgradable": "transfer fee can be changed",
    "transfer_hook_upgradable": "transfer hook can be changed",
    "default_account_state_upgradable": "default account state can be changed (e.g. to frozen)",
}


def _goplus_result(ev: Evidence | None, mint: str) -> tuple[dict | None, str]:
    if ev is None:
        return None, "provider unavailable"
    raw = ev.raw
    if not isinstance(raw, dict) or raw.get("code") != 1:
        return None, f"provider error (code={raw.get('code') if isinstance(raw, dict) else '?'})"
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get(mint), dict) or not result[mint]:
        return None, "provider has no data for this mint"
    return result[mint], ""


def _status(obj) -> bool | None:
    return flag01(obj.get("status")) if isinstance(obj, dict) else None


def evaluate_solana(
    asset: DexAssetId,
    claimed_symbol: str,
    policy: ScreeningPolicy,
    policy_version: str,
    now: datetime,
    goplus: Evidence | None,
    mint: SolanaMintFacts | None,
    holders: SolanaHolderFacts | None,
    pool: PoolFacts | None,
    sell: SellSimulation | None,
    registry: CanonicalRegistry,
) -> SecurityReport:
    if asset.spec.kind is not ChainKind.SOLANA:
        raise ValueError("evaluate_solana called for a non-Solana asset")
    report = SecurityReport(asset.key, policy_version, now)
    report.evidence.extend(
        e for e in (goplus, mint and mint.evidence, holders and holders.evidence, pool and pool.evidence,
                    sell and sell.evidence) if e
    )  # fmt: skip
    sink = FindingSink(report, now)
    data, why = _goplus_result(goplus, asset.address)

    # --- on-chain mint state (authoritative) ----------------------------------------------------
    if mint is None:
        sink.unknown("mint_account", "on-chain mint state unavailable")
    else:
        ev = mint.evidence
        if mint.owner_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            sink.block("token_program", f"owned by {mint.owner_program}, not an SPL token program", ev)
        else:
            sink.ok("token_program", "Token-2022" if mint.owner_program == TOKEN_2022_PROGRAM else "SPL Token", ev)
        for check, authority, goplus_key, meaning in (
            ("mint_authority", mint.mint_authority, "mintable", "new supply can be minted"),
            ("freeze_authority", mint.freeze_authority, "freezable", "holder accounts can be frozen"),
        ):
            if authority:
                sink.block(check, f"{meaning} by {authority}", ev)
            else:
                sink.ok(check, "revoked on-chain", ev)
            if data is not None:
                reported = _status(data.get(goplus_key))
                if reported is None:
                    sink.unknown(f"{goplus_key}_consistency", "provider field missing or malformed", goplus)
                elif reported != bool(authority):
                    sink.unknown(f"{goplus_key}_consistency", "provider and chain disagree", goplus)
        _extensions(sink, mint, data, policy, goplus)

    # --- provider checks --------------------------------------------------------------------------
    if data is None:
        sink.unknown("provider", why, goplus, "goplus")
    else:
        for key, meaning in GOPLUS_STATUS_MUST_BE_OFF.items():
            value = _status(data.get(key))
            if value is None:
                sink.unknown(key, "missing or malformed", goplus)
            elif value:
                sink.block(key, meaning, goplus)
            else:
                sink.ok(key, "not present", goplus)
        mutable = _status(data.get("metadata_mutable"))
        if mutable is None:
            sink.unknown("metadata_mutable", "missing or malformed", goplus)
        elif mutable and not policy.allow_mutable_metadata:
            sink.block("metadata_mutable", "name/symbol can be changed by the update authority", goplus)
        else:
            sink.ok("metadata_mutable", "immutable" if not mutable else "mutable (allowed by policy)", goplus)
        non_transferable = flag01(data.get("none_transferable"))
        if non_transferable is None:
            sink.unknown("non_transferable", "missing or malformed", goplus)
        elif non_transferable:
            sink.block("non_transferable", "token cannot be transferred", goplus)
        else:
            sink.ok("non_transferable", "transferable", goplus)
        state = data.get("default_account_state")
        if state == "2":
            sink.block("default_account_state", "new accounts start frozen", goplus)
        elif state in ("0", "1"):
            sink.ok("default_account_state", f"state {state}", goplus)
        else:
            sink.unknown("default_account_state", f"missing or malformed ({state!r})", goplus)
        hooks = data.get("transfer_hook")
        if not isinstance(hooks, list):
            sink.unknown("transfer_hook", "missing or malformed", goplus)
        elif hooks:
            sink.block("transfer_hook", f"external transfer hook(s) {[h.get('address') for h in hooks]}", goplus)
        else:
            sink.ok("transfer_hook", "none", goplus)
        _lp_burn(sink, data, policy, goplus)
        creators = data.get("creators")
        if isinstance(creators, list) and any(isinstance(c, dict) and c.get("malicious_address") == 1 for c in creators):
            sink.block("creators", "a creator address is flagged malicious", goplus)

    _holder_concentration(sink, holders, policy)
    _pool(sink, pool, policy, now)
    if sell is None:
        sink.unknown("sell_simulation", "no balance-delta sell simulation (a route quote alone is not enough)")
    elif not sell.succeeded:
        sink.block("sell_simulation", "simulated sell failed: possibly unsellable", sell.evidence)
    elif sell.effective_cost > policy.max_sell_tax + Decimal("0.01"):
        sink.block("sell_simulation", f"simulated sell lost {sell.effective_cost:.1%}", sell.evidence)
    else:
        sink.ok("sell_simulation", f"cost {sell.effective_cost:.2%} at simulation time only", sell.evidence)

    observations = []
    if data is not None:
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        observations.append(SymbolObservation("goplus_metadata", meta.get("symbol"), goplus))
    verify_identity(asset, claimed_symbol, registry, observations, sink, now)
    return report


def _extensions(sink: FindingSink, mint: SolanaMintFacts, data: dict | None, policy: ScreeningPolicy, goplus) -> None:
    ev = mint.evidence
    fee_seen = False
    for ext in mint.extensions:
        name = ext.get("extension") if isinstance(ext, dict) else None
        state = ext.get("state") if isinstance(ext, dict) and isinstance(ext.get("state"), dict) else {}
        if not isinstance(name, str):
            sink.unknown("extensions", f"malformed extension entry {ext!r}", ev)
        elif name == "permanentDelegate":
            if state.get("delegate"):
                sink.block("permanent_delegate", f"{state['delegate']} can move or burn anyone's tokens", ev)
        elif name == "nonTransferable":
            sink.block("non_transferable_ext", "non-transferable mint", ev)
        elif name == "defaultAccountState":
            if state.get("accountState") == "frozen":
                sink.block("default_frozen", "new token accounts start frozen", ev)
            elif state.get("accountState") != "initialized":
                sink.unknown("default_account_state_ext", f"state {state.get('accountState')!r}", ev)
        elif name == "transferHook":
            if state.get("programId") or state.get("authority"):
                sink.block(
                    "transfer_hook_ext",
                    f"hook program {state.get('programId')} / authority {state.get('authority')}",
                    ev,
                )
        elif name == "transferFeeConfig":
            fee_seen = True
            bps = []
            for k in ("newerTransferFee", "olderTransferFee"):
                v = (state.get(k) or {}).get("transferFeeBasisPoints")
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    sink.unknown("transfer_fee", f"{k} malformed", ev)
                    break
                bps.append(v)
            else:
                fee = Decimal(max(bps)) / 10_000
                if fee > policy.max_sell_tax:
                    sink.block("transfer_fee", f"{fee:.2%} transfer fee", ev)
                else:
                    sink.ok("transfer_fee", f"{fee:.2%}", ev)
            if state.get("transferFeeConfigAuthority"):
                sink.block("transfer_fee_authority", "fee can be raised by its authority", ev)
        elif name == "mintCloseAuthority":
            if state.get("closeAuthority"):
                sink.block("mint_close_authority", "mint can be closed", ev)
        elif name in ("pausableConfig", "pausable"):
            sink.block("pausable", "transfers can be paused", ev)
        elif name not in BENIGN_EXTENSIONS:
            sink.unknown("extensions", f"extension {name!r} has not been reviewed", ev)
    if data is not None and not fee_seen:
        fee_obj = data.get("transfer_fee")
        current = fee_obj.get("current_fee_rate") if isinstance(fee_obj, dict) else None
        if isinstance(current, dict) and current.get("fee_rate") not in (None, "", "0"):
            sink.unknown("transfer_fee_consistency", "provider reports a fee but the mint has none", goplus)
    sink.ok("extensions_reviewed", f"{len(mint.extensions)} extension(s) examined", ev)


def _lp_burn(sink: FindingSink, data: dict, policy: ScreeningPolicy, goplus) -> None:
    """LP burn share of the deepest standard pool, from GoPlus dex[].burn_percent.

    Assumption (unverified against live data): burn_percent is 0-100. Concentrated pools have no
    fungible LP to burn and need position-level analysis, so they stay UNKNOWN.
    """
    dexes = data.get("dex")
    if not isinstance(dexes, list) or not dexes:
        sink.unknown("lp_burn", "pool list missing", goplus)
        return
    standard = [d for d in dexes if isinstance(d, dict) and d.get("type") == "standard"]
    if not standard:
        sink.unknown("lp_burn", "no standard (fungible-LP) pool; concentrated liquidity not analysed", goplus)
        return
    deepest = max(standard, key=lambda d: number(d.get("tvl")) or Decimal(0))
    burned = number(deepest.get("burn_percent"))
    if burned is None or burned > 100:
        sink.unknown("lp_burn", f"burn_percent missing or malformed ({deepest.get('burn_percent')!r})", goplus)
    elif burned / 100 < policy.min_lp_locked_pct:
        sink.block("lp_burn", f"only {burned}% of LP burned in pool {deepest.get('id')}: liquidity can be pulled", goplus)
    else:
        sink.ok("lp_burn", f"{burned}% of LP burned (a burn alone is not sufficient)", goplus)


def _holder_concentration(sink: FindingSink, holders: SolanaHolderFacts | None, policy: ScreeningPolicy) -> None:
    if holders is None or not holders.holders:
        sink.unknown("holder_concentration", "holder distribution unavailable")
        return
    ev = holders.evidence
    total_raw = sum(h.amount_raw for h in holders.holders)
    supply_raw = ev.raw.get("supply_raw") if isinstance(ev.raw, dict) else None
    if not isinstance(supply_raw, int) or supply_raw <= 0 or total_raw > supply_raw:
        sink.unknown("holder_concentration", "token supply unavailable or inconsistent", ev)
        return
    counted = []
    for h in holders.holders:
        if h.owner == INCINERATOR or h.owner_program in POOL_PROGRAMS:
            continue
        counted.append(h.amount_raw)
    top10 = Decimal(sum(sorted(counted, reverse=True)[:10])) / supply_raw
    if top10 > policy.max_top10_holder_pct:
        sink.block("holder_concentration", f"top-10 non-pool holders own {top10:.1%}", ev)
    else:
        sink.ok("holder_concentration", f"top-10 non-pool holders own {top10:.1%} (unknown programs counted)", ev)


def _pool(sink: FindingSink, pool: PoolFacts | None, policy: ScreeningPolicy, now: datetime) -> None:
    if pool is None:
        sink.unknown("pool", "independent pool verification (program, depth, age) not available")
        return
    ev = pool.evidence
    program = POOL_PROGRAMS.get(pool.owner_program)
    if program is None:
        sink.block("pool_program", f"pool owned by unreviewed program {pool.owner_program}", ev)
        return
    sink.ok("pool_program", program[0], ev)
    if pool.concentrated or program[1]:
        sink.unknown("pool_depth", "concentrated-liquidity depth needs tick-level analysis (not implemented)", ev)
    elif pool.liquidity_usd < policy.min_liquidity_usd:
        sink.block("pool_liquidity", f"${pool.liquidity_usd:,.0f} < ${policy.min_liquidity_usd:,.0f}", ev)
    else:
        sink.ok("pool_liquidity", f"${pool.liquidity_usd:,.0f}", ev)
    age_days = Decimal((now - pool.created_at.astimezone(UTC)).total_seconds()) / 86400
    if age_days < 0:
        sink.unknown("pool_age", "creation time in the future", ev)
    elif age_days < policy.min_age_days:
        sink.block("pool_age", f"{age_days:.1f} days < {policy.min_age_days}", ev)
    else:
        sink.ok("pool_age", f"{age_days:.0f} days", ev)
