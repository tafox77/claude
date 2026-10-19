from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from conftest import NOW

from spotguard.execution.cex import REQUIRED_GATES, CoinbaseLiveAdapter, LiveApproval, LiveDisabled
from spotguard.execution.dex import (
    UNLIMITED_APPROVAL,
    ChainView,
    ConstantProductPool,
    DecodedTx,
    SwapIntent,
    TransactionPolicy,
    TxStatus,
    TxTracker,
    to_raw,
)

WALLET, ROUTER, USDC, TOKEN = "Wallet1", "JUP6Router", "USDC", "TOKEN"
POL = TransactionPolicy(frozenset({ROUTER}), frozenset({ROUTER}), max_priority_fee=100_000)


def intent(**kw):
    base = dict(intent_id="i1", chain="solana", wallet=WALLET, token_in=USDC, token_out=TOKEN, amount_in=Decimal("50"),
                decimals_in=6, quoted_out=Decimal("1000"), decimals_out=9, max_slippage=Decimal("0.01"))  # fmt: skip
    base.update(kw)
    return SwapIntent(**base)


def tx(**kw):
    base = dict(chain="solana", signer=WALLET, programs=(ROUTER,), recipient=WALLET, token_in=USDC, token_out=TOKEN,
                amount_in_raw=50_000_000, min_out_raw=990_000_000_000, approval_raw=None, approval_spender=None,
                priority_fee=5_000, recent_blockhash="hash", last_valid_block_height=1000,
                simulated_deltas={USDC: -50_000_000, TOKEN: 995_000_000_000})  # fmt: skip
    base.update(kw)
    return DecodedTx(**base)


CHAIN = ChainView(current_block_height=900, next_nonce=None)


def test_valid_swap_passes():
    assert POL.validate(tx(), intent(), CHAIN) == []


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"recipient": "Attacker"}, "unexpected recipient"),
        ({"programs": (ROUTER, "Drainer111")}, "not allowlisted"),
        ({"amount_in_raw": 50_000_000_000}, "amount_in"),  # decimals confusion (9 instead of 6)
        ({"min_out_raw": 900_000_000_000}, "slippage"),
        ({"priority_fee": 10**9}, "priority fee"),
        ({"last_valid_block_height": 800}, "expired"),
        ({"simulated_deltas": {USDC: -50_000_000, TOKEN: 995_000_000_000, "SOL": -(10**9)}}, "drains"),
        ({"simulated_deltas": {USDC: -50_000_000, TOKEN: 1}}, "received"),
        ({"simulated_deltas": {}}, "no simulated"),
    ],
)
def test_solana_policy_violations(change, needle):
    v = POL.validate(tx(**change), intent(), CHAIN)
    assert any(needle in x for x in v), v


def test_decimal_precision_mismatch_refused():
    with pytest.raises(ValueError):
        to_raw(Decimal("1.0000001"), 6)
    assert any("precision" in x for x in POL.validate(tx(), intent(amount_in=Decimal("50.0000001")), CHAIN))


def evm_tx(**kw):
    return tx(chain="evm:1", recent_blockhash=None, last_valid_block_height=None, nonce=7, **kw)


def evm_intent():
    return intent(chain="evm:1")


def test_evm_unlimited_and_oversized_approvals_refused():
    v = POL.validate(evm_tx(approval_raw=UNLIMITED_APPROVAL, approval_spender=ROUTER), evm_intent(),
                     ChainView(0, next_nonce=7))  # fmt: skip
    assert any("unlimited" in x for x in v)
    v = POL.validate(evm_tx(approval_raw=50_000_000, approval_spender="Evil"), evm_intent(), ChainView(0, 7))
    assert any("spender" in x for x in v)
    assert POL.validate(evm_tx(approval_raw=50_000_000, approval_spender=ROUTER), evm_intent(), ChainView(0, 7)) == []


def test_evm_nonce_collisions():
    assert any("nonce" in x for x in POL.validate(evm_tx(), evm_intent(), ChainView(0, next_nonce=8)))
    assert any("nonce" in x for x in POL.validate(evm_tx(), evm_intent(), ChainView(0, 7, frozenset({7}))))


def test_tracker_prevents_duplicate_submission_and_handles_reorg():
    t = TxTracker(finality_depth=32)
    t.submit("i1", "sigA")
    with pytest.raises(RuntimeError):
        t.submit("i1", "sigB")  # second tx for the same intent while the first may still land
    t.on_included("i1", "blockX", 100)
    events = t.on_head(110, {100: "blockY"})  # block 100 replaced: reorg
    assert t.by_intent["i1"].status is TxStatus.PENDING and "reorg" in events[0]
    t.on_included("i1", "blockZ", 105)
    t.on_head(140, {105: "blockZ"})
    assert t.by_intent["i1"].status is TxStatus.FINAL
    t2 = TxTracker(32)
    t2.submit("i2", "s")
    t2.on_expired("i2")
    t2.submit("i2", "s2")  # only after expiry may the intent be rebuilt


def test_constant_product_simulation_impact_grows_with_size():
    pool = ConstantProductPool(Decimal("1000000"), Decimal("1000000"))
    assert pool.price_impact(Decimal("1000")) < pool.price_impact(Decimal("100000"))
    out = pool.swap(Decimal("1000"))
    assert out < Decimal("1000") and pool.reserve_in == Decimal("1001000")


# ----- live gate ---------------------------------------------------------------------------------


def approval(policy, **kw):
    base = dict(venue="coinbase", markets=("BTC-USD",), max_capital=Decimal("500"), policy_version=policy.version,
                model_id=None, approved_by="Alice", approved_at="2026-10-19", gates_passed=REQUIRED_GATES)  # fmt: skip
    base.update(kw)
    return LiveApproval(**base)


ENV_OK = {"SPOTGUARD_LIVE_ENABLE": "I_ACCEPT_REAL_MONEY_RISK"}


def test_live_disabled_by_default(policy):
    with pytest.raises(LiveDisabled):
        approval(policy).verify(policy, None, env={})  # paper policy + no env flag


@pytest.mark.parametrize(
    "change",
    [{"markets": ("DOGE-USD",)}, {"policy_version": "other"}, {"model_id": "abc"}, {"approved_by": " "},
     {"gates_passed": REQUIRED_GATES[:-1]}, {"max_capital": Decimal("1e9")}, {"venue": "binance"}],
)  # fmt: skip
def test_live_approval_mismatches_refused(policy, change):
    live = dataclasses.replace(policy, mode="live")
    with pytest.raises(LiveDisabled):
        approval(live, **{"policy_version": live.version, **change}).verify(live, None, env=ENV_OK)


def test_live_approval_complete_passes_verification_only(policy, monkeypatch):
    live = dataclasses.replace(policy, mode="live")
    approval(live, policy_version=live.version).verify(live, None, env=ENV_OK)
    monkeypatch.delenv("SPOTGUARD_LIVE_ENABLE", raising=False)
    with pytest.raises(LiveDisabled):  # constructing the adapter re-verifies against the real environment
        CoinbaseLiveAdapter(live, approval(live, policy_version=live.version), None, exchange=object())


class FakeCcxt:
    """Mock of the ccxt client surface used by the adapter (NOT a Coinbase integration test)."""

    def __init__(self):
        self.orders = []

    def create_order(self, symbol, type_, side, amount, price, params):
        o = {"id": "ex1", "clientOrderId": params["client_order_id"], "status": "closed", "symbol": symbol}
        self.orders.append(o)
        return o

    def fetch_my_trades(self, symbol, since, limit, params):
        return [{"id": "t1", "order": "ex1", "amount": 0.001, "price": 100000.0, "fee": {"cost": 0.6},
                 "takerOrMaker": "taker", "timestamp": NOW.timestamp() * 1000}]  # fmt: skip

    def fetch_orders(self, symbol, since, limit, params):
        return self.orders


async def test_live_adapter_maps_orders_with_mock_client(policy, monkeypatch):
    from spotguard.execution.base import OrderRequest, VenueStatus

    live = dataclasses.replace(policy, mode="live")
    monkeypatch.setenv("SPOTGUARD_LIVE_ENABLE", "I_ACCEPT_REAL_MONEY_RISK")
    ad = CoinbaseLiveAdapter(live, approval(live, policy_version=live.version), None, exchange=FakeCcxt())
    view = await ad.submit(OrderRequest("sg-1", "cex:coinbase:BTC-USD", "buy", Decimal("0.001"), Decimal("100000"), "IOC"))
    assert view.status is VenueStatus.FILLED and view.fills[0].fee == Decimal("0.6")
    assert (await ad.lookup("sg-1")).exchange_order_id == "ex1"
    with pytest.raises(LiveDisabled):
        await ad.submit(OrderRequest("sg-2", "cex:coinbase:ETH-USD", "buy", Decimal(1), Decimal(1), "IOC"))
