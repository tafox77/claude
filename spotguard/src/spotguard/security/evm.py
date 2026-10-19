"""EVM token screening: GoPlus token security + independent RPC state + pool and sell evidence.

Field meanings follow GoPlus's token_security schema (official SDK v0.2.6): flags are "1"/"0"
strings and "no return means unknown". Missing or malformed values become UNKNOWN, never false.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from spotguard.config import ScreeningPolicy
from spotguard.identities import ChainKind, DexAssetId
from spotguard.security.facts import EvmRpcFacts, PoolFacts, SellSimulation
from spotguard.security.identity import CanonicalRegistry, SymbolObservation, verify_identity
from spotguard.security.schema import (
    Evidence,
    FindingSink,
    SecurityReport,
    flag01,
    is_burn_address,
    number,
    ratio,
    timestamp,
)

# Every one of these must be "0". "1" blocks; absent/malformed is UNKNOWN.
MUST_BE_FALSE = {
    "is_honeypot": "token may be unsellable (honeypot)",
    "cannot_sell_all": "holders cannot sell their full balance",
    "cannot_buy": "token cannot be bought directly",
    "owner_change_balance": "owner can change holder balances",
    "hidden_owner": "hidden owner can manipulate the contract",
    "can_take_back_ownership": "renounced ownership can be reclaimed",
    "selfdestruct": "contract can self-destruct",
    "transfer_pausable": "transfers can be paused",
    "is_blacklisted": "blacklist can block wallets from trading",
    "is_whitelisted": "whitelist restricts who can trade",
    "slippage_modifiable": "taxes can be changed",
    "personal_slippage_modifiable": "per-wallet taxes can be set",
    "trading_cooldown": "trading cooldown restricts selling",
    "is_mintable": "supply can be minted",
    "external_call": "transfers call external contracts",
    "anti_whale_modifiable": "transaction limits can be changed",
    "is_proxy": "upgradeable proxy: code can change after screening",
}


def _goplus_result(ev: Evidence | None, address: str) -> tuple[dict | None, str]:
    if ev is None:
        return None, "provider unavailable"
    raw = ev.raw
    if not isinstance(raw, dict) or raw.get("code") != 1:
        return None, f"provider error (code={raw.get('code') if isinstance(raw, dict) else '?'})"
    result = raw.get("result")
    if not isinstance(result, dict):
        return None, "provider returned no result"
    matches = [v for k, v in result.items() if isinstance(k, str) and k.lower() == address]
    if len(matches) != 1 or not isinstance(matches[0], dict) or not matches[0]:
        return None, "provider has no data for this address"
    return matches[0], ""


def evaluate_evm(
    asset: DexAssetId,
    claimed_symbol: str,
    policy: ScreeningPolicy,
    policy_version: str,
    now: datetime,
    goplus: Evidence | None,
    rpc: EvmRpcFacts | None,
    pool: PoolFacts | None,
    sell: SellSimulation | None,
    registry: CanonicalRegistry,
) -> SecurityReport:
    if asset.spec.kind is not ChainKind.EVM:
        raise ValueError("evaluate_evm called for a non-EVM asset")
    report = SecurityReport(asset.key, policy_version, now)
    report.evidence.extend(e for e in (goplus, rpc and rpc.evidence, pool and pool.evidence, sell and sell.evidence) if e)
    sink = FindingSink(report, now)
    data, why = _goplus_result(goplus, asset.address)
    src = "goplus"

    # --- provider risk flags ---------------------------------------------------------------
    if data is None:
        sink.unknown("provider", why, goplus, src)
    else:
        open_source = flag01(data.get("is_open_source"))
        if open_source is None:
            sink.unknown("is_open_source", "missing or malformed", goplus)
        elif not open_source:
            sink.block("is_open_source", "source is not verified; other checks can't be trusted", goplus)
        else:
            sink.ok("is_open_source", "verified source", goplus)
        for key, meaning in MUST_BE_FALSE.items():
            value = flag01(data.get(key))
            if value is None:
                sink.unknown(key, f"missing or malformed ({data.get(key)!r})", goplus)
            elif value:
                sink.block(key, meaning, goplus)
            else:
                sink.ok(key, "not present", goplus)
        # Supplemental fields where GoPlus defines absence as "no conclusive result".
        if flag01(data.get("is_airdrop_scam")) is True:
            sink.block("is_airdrop_scam", "flagged as airdrop scam", goplus)
        if flag01(data.get("is_true_token")) is False:
            sink.block("is_true_token", "flagged as a fake token", goplus)
        fake = data.get("fake_token")
        if isinstance(fake, dict) and fake.get("value") == 1:
            sink.block("fake_token", f"imitates {fake.get('true_token_address')}", goplus)

        same_creator = number(data.get("honeypot_with_same_creator"))
        if same_creator is None:
            sink.unknown("honeypot_with_same_creator", "missing or malformed", goplus)
        elif same_creator > 0:
            sink.block("honeypot_with_same_creator", f"creator deployed {same_creator} honeypot(s)", goplus)
        else:
            sink.ok("honeypot_with_same_creator", "none known", goplus)

        for key, limit in (("buy_tax", policy.max_buy_tax), ("sell_tax", policy.max_sell_tax)):
            tax = ratio(data.get(key))
            if tax is None:
                sink.unknown(key, f"missing or malformed ({data.get(key)!r})", goplus)
            elif tax > limit:
                sink.block(key, f"{tax:.2%} exceeds {limit:.2%}", goplus)
            else:
                sink.ok(key, f"{tax:.2%}", goplus)

        owner = data.get("owner_address")
        if owner is None:
            sink.unknown("ownership", "owner not reported", goplus)
        elif owner == "" or is_burn_address(str(owner)):
            sink.ok("ownership", "no owner reported (renounced ownership alone is not sufficient)", goplus)
        else:
            sink.unknown("ownership", f"privileged owner {owner}; role reachability analysis not implemented", goplus)

        _holders(sink, data, pool, policy, goplus)
        _liquidity_ownership(sink, data, policy, now, goplus)

    # --- independent RPC state ----------------------------------------------------------------
    if rpc is None:
        sink.unknown("rpc_state", "independent RPC state unavailable")
    else:
        ev = rpc.evidence
        if not rpc.has_code:
            sink.block("rpc_code", "no contract code at this address", ev)
        else:
            sink.ok("rpc_code", "contract code present", ev)
        if rpc.implementation or rpc.admin:
            reported = flag01(data.get("is_proxy")) if data else None
            if reported is False:
                sink.unknown("proxy_consistency", "RPC shows EIP-1967 proxy slots but provider says not a proxy", ev)
            sink.block("rpc_proxy", f"EIP-1967 implementation={rpc.implementation} admin={rpc.admin}", ev)
        else:
            sink.ok("rpc_proxy", "EIP-1967 slots empty", ev)
        if rpc.decimals is None or not 0 <= rpc.decimals <= 36:
            sink.unknown("rpc_decimals", f"decimals unreadable ({rpc.decimals})", ev)
        else:
            sink.ok("rpc_decimals", str(rpc.decimals), ev)

    _pool(sink, pool, data, policy, now)
    _sell(sink, sell, policy)

    observations = []
    if data is not None:
        observations.append(SymbolObservation("goplus", data.get("token_symbol"), goplus))
    if rpc is not None:
        observations.append(SymbolObservation("evm_rpc", rpc.symbol, rpc.evidence))
    verify_identity(asset, claimed_symbol, registry, observations, sink, now)
    return report


def _holders(sink: FindingSink, data: dict, pool: PoolFacts | None, policy: ScreeningPolicy, ev: Evidence) -> None:
    holders = data.get("holders")
    if not isinstance(holders, list) or not holders:
        sink.unknown("holder_concentration", "holder list missing", ev)
        return
    pools = {str(d.get("pair", "")).lower() for d in data.get("dex") or [] if isinstance(d, dict)}
    if pool is not None:
        pools.add(pool.pool_address.lower())
    counted = []
    for h in holders:
        if not isinstance(h, dict):
            sink.unknown("holder_concentration", "malformed holder entry", ev)
            return
        pct = ratio(h.get("percent"))
        locked = flag01(h.get("is_locked"))
        address = str(h.get("address", "")).lower()
        if pct is None or locked is None or not address:
            sink.unknown("holder_concentration", f"malformed holder entry {h!r}", ev)
            return
        if locked or is_burn_address(address) or address in pools:
            continue
        counted.append(pct)
    # Exclude pools/locks/burns first, then take the ten largest remaining holders.
    total = sum(sorted(counted, reverse=True)[:10], Decimal(0))
    if total > policy.max_top10_holder_pct:
        sink.block("holder_concentration", f"top-10 non-pool holders own {total:.1%}", ev)
    else:
        sink.ok(
            "holder_concentration",
            f"top-10 non-pool holders own {total:.1%} (wallet clustering beyond provider tags not analysed)",
            ev,
        )


def _liquidity_ownership(sink: FindingSink, data: dict, policy: ScreeningPolicy, now: datetime, ev: Evidence) -> None:
    lp = data.get("lp_holders")
    dexes = data.get("dex")
    if not isinstance(lp, list) or not lp or not isinstance(dexes, list):
        sink.unknown("lp_ownership", "LP holder data missing", ev)
        return
    if any(isinstance(h, dict) and h.get("nft_list") for h in lp) or any(
        isinstance(d, dict) and "v3" in str(d.get("name", "")).lower() for d in dexes
    ):
        sink.unknown("lp_ownership", "concentrated-liquidity positions need position-level analysis (not implemented)", ev)
        return
    min_until = now + timedelta(days=float(policy.min_lp_lock_remaining_days))
    secured = Decimal(0)
    for h in lp:
        pct = ratio(h.get("percent")) if isinstance(h, dict) else None
        locked = flag01(h.get("is_locked")) if isinstance(h, dict) else None
        if pct is None or locked is None:
            sink.unknown("lp_ownership", f"malformed LP holder {h!r}", ev)
            return
        if is_burn_address(str(h.get("address", ""))):
            secured += pct
        elif locked:
            ends = [timestamp(d.get("end_time"), now, max_future_s=10**10) for d in h.get("locked_detail") or []]
            if ends and all(e is not None and e >= min_until for e in ends):
                secured += pct
    if secured < policy.min_lp_locked_pct:
        sink.block("lp_ownership", f"only {secured:.1%} of LP burned or locked past {min_until:%Y-%m-%d}", ev)
    else:
        sink.ok("lp_ownership", f"{secured:.1%} of LP burned/locked (a lock alone is not sufficient)", ev)


def _pool(sink: FindingSink, pool: PoolFacts | None, data: dict | None, policy: ScreeningPolicy, now: datetime) -> None:
    if pool is None:
        sink.unknown("pool", "independent pool verification (depth, age, factory) not available")
        return
    ev = pool.evidence
    if pool.concentrated:
        sink.unknown("pool", "concentrated-liquidity pool depth needs tick-level analysis (not implemented)", ev)
        return
    pairs = {str(d.get("pair", "")).lower() for d in (data or {}).get("dex") or [] if isinstance(d, dict)}
    if pool.pool_address.lower() not in pairs:
        sink.unknown("pool_authenticity", "pool not corroborated by the provider's pair list", ev)
    else:
        sink.ok("pool_authenticity", f"pool {pool.pool_address} corroborated", ev)
    if pool.liquidity_usd < policy.min_liquidity_usd:
        sink.block("pool_liquidity", f"${pool.liquidity_usd:,.0f} < ${policy.min_liquidity_usd:,.0f}", ev)
    else:
        sink.ok("pool_liquidity", f"${pool.liquidity_usd:,.0f}", ev)
    age_days = Decimal((now - pool.created_at).total_seconds()) / 86400
    if age_days < 0:
        sink.unknown("pool_age", "pool creation time is in the future", ev)
    elif age_days < policy.min_age_days:
        sink.block("pool_age", f"{age_days:.1f} days < {policy.min_age_days}", ev)
    else:
        sink.ok("pool_age", f"{age_days:.0f} days", ev)


def _sell(sink: FindingSink, sell: SellSimulation | None, policy: ScreeningPolicy) -> None:
    if sell is None:
        sink.unknown("sell_simulation", "no balance-delta sell simulation (a route quote alone is not enough)")
    elif not sell.succeeded:
        sink.block("sell_simulation", "simulated sell failed: possibly unsellable", sell.evidence)
    elif sell.effective_cost > policy.max_sell_tax + Decimal("0.01"):
        sink.block("sell_simulation", f"simulated sell lost {sell.effective_cost:.1%}", sell.evidence)
    else:
        sink.ok("sell_simulation", f"simulated sell cost {sell.effective_cost:.2%} (one moment only)", sell.evidence)
