from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import NOW
from helpers import BTC, Clock, Quotes, book, rules

from spotguard.execution.base import OrderState
from spotguard.execution.orders import Intent, OrderManager, fill_summary
from spotguard.execution.paper import FailurePlan, PaperVenue
from spotguard.risk import RiskEngine
from spotguard.storage import Store
from spotguard.supervisor import ExitSupervisor

USD = Decimal("10000")


class Env:
    def __init__(self, policy, tmp_path, failures=None, depth=("0.5", "0.5", "1")):
        self.policy = policy
        self.clock = Clock()
        self.quotes = Quotes()
        self.quotes.set(book(self.clock, depth=depth))
        self.store = Store(tmp_path / "db.sqlite")
        self.store.set_initial_balance("USD", USD, NOW)
        self.venue = PaperVenue(self.quotes, {BTC.key: rules()}, policy.execution, policy.market, self.clock.mono_ns,
                                self.clock.now, {"USD": USD}, failures=failures)  # fmt: skip
        self.orders = OrderManager(self.store, self.venue, "USD", 0.5, self.clock.now, not_found_grace_s=30)
        self.risk = RiskEngine(policy, self.store, NOW)
        self.sup = ExitSupervisor(policy, self.store, self.orders, self.risk, self.quotes, {BTC.key: rules()},
                                  self.clock.mono_ns, self.clock.now)  # fmt: skip

    def buy(self, qty="0.004", limit="100010", tif="IOC"):
        return Intent(BTC.key, "buy", "entry", Decimal(qty), Decimal(limit), tif, "test", self.policy.version,
                      stop=Decimal("99000"), target=Decimal("102000"))  # fmt: skip

    def state(self, cid):
        return OrderState(self.store.order(cid)["state"])

    def refresh(self, **kw):
        self.quotes.set(book(self.clock, **kw))


async def test_marketable_limit_fills_with_fee_accounting(policy, tmp_path):
    env = Env(policy, tmp_path)
    cid = await env.orders.place(env.buy("0.004"))
    assert env.state(cid) is OrderState.FILLED
    filled, avg = fill_summary(env.store, cid)
    assert filled == Decimal("0.004") and avg == Decimal("100010")
    fee = Decimal("0.004") * 100010 * policy.execution.taker_fee_pct  # 2.40024 -> rounded up to 2.41
    assert env.store.balance("USD") == USD - Decimal("400.04") - Decimal("2.41")
    assert fee < Decimal("2.41")


async def test_partial_fill_when_depth_runs_out_ioc_remainder_canceled(policy, tmp_path):
    env = Env(policy, tmp_path, depth=("0.001", "0.001", "0.001"))
    cid = await env.orders.place(env.buy("0.004", limit="100020"))  # only two levels within limit
    assert env.state(cid) is OrderState.CANCELED
    filled, avg = fill_summary(env.store, cid)
    assert filled == Decimal("0.002") and avg == Decimal("100015")
    assert env.store.positions()[BTC.key].qty == Decimal("0.002")


async def test_slippage_bound_prevents_fill(policy, tmp_path):
    env = Env(policy, tmp_path)
    cid = await env.orders.place(env.buy("0.004", limit="100000"))  # below best ask
    assert env.state(cid) is OrderState.CANCELED and fill_summary(env.store, cid)[0] == 0


@pytest.mark.parametrize("qty,limit,why", [("0.000000001", "100010", "multiple"), ("0.004", "100010.001", "multiple"),
                                           ("0.000005", "100010", "notional")])  # fmt: skip
async def test_rounding_and_minimums_rejected(policy, tmp_path, qty, limit, why):
    env = Env(policy, tmp_path)
    cid = await env.orders.place(env.buy(qty, limit))
    assert env.state(cid) is OrderState.REJECTED
    assert why in env.store.order(cid)["last_error"]


async def test_rejected_order(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("reject", 1)])
    assert env.state(await env.orders.place(env.buy())) is OrderState.REJECTED


async def test_ambiguous_submit_timeout_reconciles_by_client_id_without_retry(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("timeout_after_accept", 1)])
    cid = await env.orders.place(env.buy())
    assert env.state(cid) is OrderState.UNKNOWN
    assert env.orders.blocks_new_exposure()[0]  # UNKNOWN blocks additional exposure
    assert env.store.positions() == {}  # nothing applied until reconciled
    assert await env.orders.reconcile(cid) is OrderState.FILLED
    assert await env.orders.reconcile(cid) is OrderState.FILLED  # idempotent
    assert env.store.positions()[BTC.key].qty == Decimal("0.004")
    assert env.venue.submits == 1  # never resubmitted
    assert not env.orders.blocks_new_exposure()[0]


async def test_lost_submit_becomes_rejected_only_after_grace(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("drop_before_accept", 1)])
    cid = await env.orders.place(env.buy())
    assert await env.orders.reconcile(cid) is OrderState.UNKNOWN  # venue doesn't know it yet: keep blocking
    env.clock.advance(31)
    assert await env.orders.reconcile(cid) is OrderState.REJECTED


async def test_lookup_failure_keeps_unknown(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("timeout_after_accept", 1), FailurePlan("lookup_error")])
    cid = await env.orders.place(env.buy())
    assert await env.orders.reconcile(cid) is OrderState.UNKNOWN


async def test_fill_racing_cancel_is_applied(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("fill_during_cancel", 1)])
    cid = await env.orders.place(env.buy(tif="GTC"))
    assert env.state(cid) is OrderState.ACKNOWLEDGED
    final = await env.orders.cancel(cid)
    assert final is OrderState.FILLED  # the cancel "succeeded" too late; fills win
    assert env.store.positions()[BTC.key].qty == Decimal("0.004")


async def test_cancel_timeout_is_unknown_then_reconciled(policy, tmp_path):
    env = Env(policy, tmp_path, failures=[FailurePlan("cancel_timeout")])
    env.quotes.set(book(env.clock, ask="100100"))
    cid = await env.orders.place(env.buy(limit="100010", tif="GTC"))
    assert await env.orders.cancel(cid) is OrderState.UNKNOWN
    assert await env.orders.reconcile(cid) is OrderState.ACKNOWLEDGED  # still resting at the venue


async def test_stale_book_never_fabricates_a_fill(policy, tmp_path):
    env = Env(policy, tmp_path)
    env.clock.advance(5)
    cid = await env.orders.place(env.buy())
    assert env.state(cid) is OrderState.REJECTED and "usable book" in env.store.order(cid)["last_error"]


async def test_duplicate_fill_events_are_ignored(policy, tmp_path):
    env = Env(policy, tmp_path)
    cid = await env.orders.place(env.buy())
    view = await env.venue.lookup(cid)
    env.orders._absorb(cid, view)
    env.orders._absorb(cid, view)
    assert env.store.positions()[BTC.key].qty == Decimal("0.004")
    assert env.store.query("SELECT count(*) FROM fills")[0][0] == 1
    assert env.store.record_event("ev-1", "test", {}, NOW) and not env.store.record_event("ev-1", "test", {}, NOW)


async def test_crash_between_persist_and_send_is_never_resent(policy, tmp_path):
    env = Env(policy, tmp_path)
    intent = env.buy()
    env.store.insert_intent(
        {
            "intent_id": intent.intent_id,
            "created_at": NOW.isoformat(),
            "asset_key": BTC.key,
            "side": "buy",
            "purpose": "entry",
            "qty": "0.004",
            "limit_price": "100010",
            "reason": "x",
            "policy_version": "p",
            "model_version": None,
            "decision": "{}",
        }
    )
    env.store.insert_order({"client_order_id": "sg-crashed", "intent_id": intent.intent_id, "asset_key": BTC.key,
                            "side": "buy", "purpose": "entry", "qty": "0.004", "limit_price": "100010",
                            "time_in_force": "IOC", "state": "CREATED", "created_at": NOW.isoformat(),
                            "updated_at": NOW.isoformat()})  # fmt: skip
    restarted = OrderManager(Store(tmp_path / "db.sqlite"), env.venue, "USD", 0.5, env.clock.now)
    await restarted.reconcile_all()
    assert restarted.store.order("sg-crashed")["state"] == "UNKNOWN"
    env.clock.advance(31)
    await restarted.reconcile_all()
    assert restarted.store.order("sg-crashed")["state"] == "REJECTED" and env.venue.submits == 0


# ----- exits -----------------------------------------------------------------------------------


async def open_position(env):
    cid = await env.orders.place(env.buy("0.004"))
    env.store.set_protection(BTC.key, Decimal("99000"), Decimal("102000"))
    assert env.state(cid) is OrderState.FILLED


async def test_stop_exit_and_gap_through_stop_fills_at_market_not_stop(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.refresh(bid="97000", ask="97010", depth=("1", "1", "1"))  # gapped far below the 99000 stop
    await env.sup.tick()
    assert BTC.key not in env.store.positions()
    sell = env.store.query("SELECT price FROM fills WHERE side = 'sell'")[0][0]
    assert Decimal(sell) == Decimal("97000")  # realized below the stop: planned risk is not max loss


async def test_target_and_max_hold(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.clock.advance(policy.strategy.max_hold_s + 1)
    env.refresh()
    await env.sup.tick()
    assert BTC.key not in env.store.positions()


async def test_exits_work_while_entries_blocked_and_latched(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.risk.latch("daily_loss", "test", NOW)
    env.risk.request_flatten("tester", NOW)
    await env.sup.tick()
    assert BTC.key not in env.store.positions()
    await env.sup.tick()
    assert not env.risk.state.flatten_requested and env.risk.state.block_entries  # entries stay blocked


async def test_unsellable_position_alerts_and_is_kept(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.risk.request_flatten("tester", NOW)
    from helpers import book as mk

    q = mk(env.clock, bid="100000", ask="100010")
    from dataclasses import replace

    env.quotes.set(replace(q, bids=((Decimal("50000"), Decimal("1")),)))  # only a bid 50% below
    await env.sup.tick()
    assert env.store.positions()[BTC.key].qty == Decimal("0.004")
    assert any("UNSELLABLE" in a for a in env.sup.alerts)


async def test_partial_exit_leaves_managed_residual(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.refresh(bid="98000", ask="98010", depth=("0.001", "0.0005", "0"))
    await env.sup.tick()
    assert env.store.positions()[BTC.key].qty == Decimal("0.0025")
    env.refresh(bid="98000", ask="98010", depth=("1", "1", "1"))
    await env.sup.tick()
    assert BTC.key not in env.store.positions()


async def test_stale_feed_delays_exit_with_alert_then_recovers(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.refresh(bid="98000", ask="98010")
    env.clock.advance(5)  # book now stale
    await env.sup.tick()
    assert BTC.key in env.store.positions() and any("unusable" in a for a in env.sup.alerts)
    env.refresh(bid="98000", ask="98010")
    await env.sup.tick()
    assert BTC.key not in env.store.positions()


async def test_restart_with_open_position_keeps_managing_it(policy, tmp_path):
    env = Env(policy, tmp_path)
    await open_position(env)
    env.store.close()
    env2 = Env.__new__(Env)
    env2.__dict__.update(env.__dict__)
    env2.store = Store(tmp_path / "db.sqlite")
    env2.orders = OrderManager(env2.store, env.venue, "USD", 0.5, env.clock.now)
    env2.risk = RiskEngine(policy, env2.store, NOW)
    env2.sup = ExitSupervisor(policy, env2.store, env2.orders, env2.risk, env.quotes, {BTC.key: rules()},
                              env.clock.mono_ns, env.clock.now)  # fmt: skip
    assert env2.store.positions()[BTC.key].stop == Decimal("99000")
    env2.refresh(bid="98900", ask="98910")
    await env2.sup.tick()
    assert BTC.key not in env2.store.positions()


async def test_leader_lock_prevents_second_trader(tmp_path):
    from spotguard.storage import LeaderLockError

    a, b = Store(tmp_path / "db.sqlite"), Store(tmp_path / "db.sqlite")
    a.acquire_leader()
    with pytest.raises(LeaderLockError):
        b.acquire_leader()
    a.close()
    b.acquire_leader()


# ----- regressions from the code review ------------------------------------------------------------


async def test_entry_filled_via_reconcile_gets_protection_atomically(policy, tmp_path):
    """Regression: an entry whose fill arrived through reconciliation had no stop or target."""
    env = Env(policy, tmp_path, failures=[FailurePlan("timeout_after_accept", 1)])
    cid = await env.orders.place(env.buy())
    assert env.state(cid) is OrderState.UNKNOWN
    await env.orders.reconcile(cid)
    pos = env.store.positions()[BTC.key]
    assert (pos.stop, pos.target) == (Decimal("99000"), Decimal("102000"))


async def test_reentry_does_not_inherit_stale_protection(policy, tmp_path):
    env = Env(policy, tmp_path)
    await env.orders.place(env.buy())
    env.refresh(bid="98900", ask="98910")
    await env.sup.tick()  # stopped out
    assert BTC.key not in env.store.positions()
    env.refresh()
    second = Intent(BTC.key, "buy", "entry", Decimal("0.004"), Decimal("100010"), "IOC", "t", policy.version,
                    stop=Decimal("99500"), target=Decimal("101000"))  # fmt: skip
    await env.orders.place(second)
    pos = env.store.positions()[BTC.key]
    assert (pos.stop, pos.target) == (Decimal("99500"), Decimal("101000"))


async def test_wide_spread_does_not_block_stop_exit(policy, tmp_path):
    """Regression: exits priced off the mid made a stop unsellable whenever the spread was wide."""
    env = Env(policy, tmp_path)
    await open_position(env)
    env.refresh(bid="98800", ask="99800", depth=("1", "1", "1"))  # ~1% spread, bid below the stop
    await env.sup.tick()
    assert BTC.key not in env.store.positions()


async def test_flatten_resolves_cancel_of_order_venue_never_received(policy, tmp_path):
    """Regression: cancelling a never-received UNKNOWN order stuck in CANCEL_PENDING forever."""
    env = Env(policy, tmp_path, failures=[FailurePlan("drop_before_accept", 1)])
    cid = await env.orders.place(env.buy())
    env.risk.request_flatten("tester", NOW)
    await env.sup.tick()
    assert env.orders.blocks_new_exposure()[0]  # pending cancel is as uncertain as UNKNOWN
    env.clock.advance(31)
    await env.sup.tick()
    await env.sup.tick()
    assert env.state(cid) is OrderState.REJECTED
    assert not env.risk.state.flatten_requested
    assert not env.store.query("SELECT 1 FROM alerts WHERE message LIKE 'illegal transition%'")
