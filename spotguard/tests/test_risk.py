from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW
from helpers import BTC, ETH, rules

from spotguard.risk import EntryRequest, ExposureView, RiskEngine
from spotguard.storage import Store

EQ = Decimal("10000")


def engine(policy, tmp_path):
    return RiskEngine(policy, Store(tmp_path / "db.sqlite"), NOW)


def view(**kw):
    base = dict(equity=EQ, cash=EQ, position_values={}, pending_entry_values={})
    base.update(kw)
    return ExposureView(**base)


def req(key=BTC.key, price="100000", stop="99400", r=None):
    return EntryRequest(key, Decimal(price), Decimal(stop), r if r is not None else rules())


def test_sizing_uses_both_risk_and_exposure_caps(policy, tmp_path):
    d = engine(policy, tmp_path).check_entry(req(), view())
    assert d.approved
    # risk cap: 10000*0.0025 / (600 + 300) = 0.0277; asset cap: 500/100000 = 0.005 -> exposure binds
    assert d.details["binding"] == "asset" and d.qty == Decimal("0.00500000")
    (tmp_path / "b").mkdir()
    wide_stop = engine(policy, tmp_path / "b").check_entry(req(stop="90000"), view())
    assert wide_stop.details["binding"] == "risk" and wide_stop.qty < Decimal("0.005")


def test_impossible_size_is_rejected_not_rounded_up(policy, tmp_path):
    big_min = dataclasses.replace(rules(), base_min_size="0.01")
    d = engine(policy, tmp_path).check_entry(req(r=big_min), view())
    assert not d.approved and "impossible" in d.reasons[0]


def test_total_and_group_exposure_and_position_limits(policy, tmp_path):
    e = engine(policy, tmp_path)
    eth_held = view(position_values={ETH.key: Decimal("500")})
    d = e.check_entry(req(), eth_held)
    assert d.approved and d.details["binding"] in ("asset", "group")
    group_full = view(position_values={ETH.key: Decimal("1000")})
    assert not e.check_entry(req(), group_full).approved  # correlated group cap reached
    three = view(position_values={"cex:coinbase:A-USD": Decimal(1), "cex:coinbase:B-USD": Decimal(1),
                                  "cex:coinbase:C-USD": Decimal(1)})  # fmt: skip
    assert "max 3 positions" in e.check_entry(req(), three).reasons


def test_no_averaging_down(policy, tmp_path):
    d = engine(policy, tmp_path).check_entry(req(), view(position_values={BTC.key: Decimal("100")}))
    assert not d.approved and "averaging down" in " ".join(d.reasons)


def test_unknown_orders_stale_quotes_and_screening_block_entries(policy, tmp_path):
    e = engine(policy, tmp_path)
    assert not e.check_entry(req(), view(blocking_orders=(True, "1 UNKNOWN"))).approved
    assert not e.check_entry(req(), view(quote_ok=(False, "stale"))).approved
    blocked = EntryRequest(BTC.key, Decimal("100000"), Decimal("99400"), rules(), False, "UNKNOWN")
    assert not e.check_entry(blocked, view()).approved
    assert not e.check_entry(req(key="cex:coinbase:DOGE-USD"), view()).approved  # not allowlisted


def test_daily_loss_latches_and_persists_across_restart(policy, tmp_path):
    e = engine(policy, tmp_path)
    e.mark(EQ, NOW)
    events = e.mark(Decimal("9790"), NOW + timedelta(hours=1))  # -2.1% incl. fees and unrealized
    assert any("daily_loss" in x for x in events)
    restarted = RiskEngine(policy, Store(tmp_path / "db.sqlite"), NOW + timedelta(hours=2))
    assert "daily_loss" in restarted.state.latched
    assert not restarted.check_entry(req(), view(equity=Decimal("9790"))).approved
    # A new day does not clear the latch; only a named review does.
    restarted.mark(Decimal("9790"), NOW + timedelta(days=1))
    assert "daily_loss" in restarted.state.latched
    with pytest.raises(ValueError):
        restarted.reset("daily_loss", "", "x", NOW)
    restarted.reset("daily_loss", "alice", "reviewed losses", NOW + timedelta(days=1))
    assert restarted.check_entry(req(), view(equity=Decimal("9790"), cash=Decimal("9790"))).approved


def test_drawdown_from_high_water(policy, tmp_path):
    e = engine(policy, tmp_path)
    e.mark(Decimal("11000"), NOW)
    e.mark(Decimal("10800"), NOW + timedelta(days=1))
    e.mark(Decimal("10600"), NOW + timedelta(days=2))
    e.mark(Decimal("10440"), NOW + timedelta(days=3))  # 5.1% below 11000, never 2% in a single day
    assert "drawdown" in e.state.latched and "daily_loss" not in e.state.latched


def test_cash_flows_do_not_count_as_pnl(policy, tmp_path):
    e = engine(policy, tmp_path)
    e.mark(EQ, NOW)
    e.record_cash_flow(Decimal("-1000"), "withdrawal", NOW)
    assert e.mark(Decimal("9000"), NOW + timedelta(minutes=1)) == []


def test_kill_switch_two_actions(policy, tmp_path):
    e = engine(policy, tmp_path)
    e.set_block_entries(True, "bob", "news", NOW)
    assert not e.check_entry(req(), view()).approved
    assert e.state.flatten_requested is False
    e.request_flatten("bob", NOW)
    assert e.state.flatten_requested and e.state.block_entries
    ok, _ = RiskEngine.check_exit(Decimal("1"), Decimal("1"))
    assert ok  # exits remain possible while entries are blocked
    assert not RiskEngine.check_exit(Decimal("2"), Decimal("1"))[0]


def test_policy_and_limits_are_immutable(policy):
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.risk.max_positions = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.risk = None  # type: ignore[misc]


def test_live_mode_blocked_without_approval(policy, tmp_path):
    live = dataclasses.replace(policy, mode="live")
    e = RiskEngine(live, Store(tmp_path / "db.sqlite"), NOW)
    assert not e.check_entry(req(), view()).approved


def test_planned_risk_covers_a_clean_stop_out(policy, tmp_path):
    """Regression: sizing allowed 0.3% for costs while a round trip costs 1.2% in fees alone."""
    d = engine(policy, tmp_path).check_entry(req(stop="90000"), view())  # risk cap binds
    qty, planned = d.qty, Decimal(d.details["planned_risk"])
    fee = policy.execution.taker_fee_pct
    exit_price = Decimal("90000") * (1 - policy.execution.max_exit_slippage_pct)
    loss = qty * Decimal("100000") * (1 + fee) - qty * exit_price * (1 - fee)
    assert planned >= loss
    assert planned <= EQ * policy.risk.risk_per_trade_pct + Decimal("0.01")
