"""Independent risk gate.

Sizing uses BOTH exposure caps and stop-distance risk (with a fee/slippage allowance); sizes that
can't satisfy exchange minimums are rejected, never rounded up. Planned stop risk is not maximum
possible loss: prices can gap through stops.

Loss controls latch: once the daily-loss or high-water drawdown stop trips, it stays tripped across
restarts until a named reviewer resets it. Daily loss counts realized and marked-to-market
unrealized P&L including fees, anchored at midnight in the policy timezone; external cash flows
adjust anchors so deposits/withdrawals aren't mistaken for P&L.

The kill switch has two separate actions: block entries (A) and cancel/reconcile/flatten (B).
Entry restrictions never block exits. Nothing in this module accepts input from a model, and the
policy object is immutable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from spotguard.config import Policy
from spotguard.identities import ProductRules
from spotguard.storage import Store, iso

ZERO = Decimal(0)


@dataclass
class RiskState:
    day: str
    day_anchor_equity: str
    high_water: str
    latched: dict[str, dict] = field(default_factory=dict)  # kind -> {"at", "reason"}
    block_entries: bool = False
    block_reason: str = ""
    flatten_requested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EntryRequest:
    asset_key: str
    entry_price: Decimal  # limit price for the marketable entry
    stop: Decimal
    rules: ProductRules | None
    screening_ok: bool = True
    screening_reason: str = ""


@dataclass(frozen=True)
class ExposureView:
    equity: Decimal
    cash: Decimal
    position_values: dict[str, Decimal]  # asset_key -> marked value
    pending_entry_values: dict[str, Decimal]  # asset_key -> committed by open entry orders
    blocking_orders: tuple[bool, str] = (False, "")
    quote_ok: tuple[bool, str] = (True, "")


@dataclass
class RiskDecision:
    approved: bool
    qty: Decimal = ZERO
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class RiskEngine:
    def __init__(self, policy: Policy, store: Store, now: datetime):
        self.policy = policy
        self.store = store
        self.tz = ZoneInfo(policy.timezone)
        self.live_approval_ok = False  # set only by the runtime after a valid approval file and acceptance gates
        loaded = store.load_risk_state()
        if loaded is None:
            eq = str(policy.risk.virtual_equity)
            self.state = RiskState(self._day(now), eq, eq)
            self._save(("init", f"anchor={eq}"), now)
        else:
            self.state = RiskState(**loaded)

    # ----- persistence --------------------------------------------------------------------
    def _day(self, now: datetime) -> str:
        return now.astimezone(self.tz).date().isoformat()

    def _save(self, event: tuple[str, str] | None, now: datetime) -> None:
        self.store.save_risk_state(self.state.to_dict(), event, now)

    # ----- marks & latches ------------------------------------------------------------------
    def mark(self, equity: Decimal, now: datetime) -> list[str]:
        events = []
        changed = False
        day = self._day(now)
        if day != self.state.day:
            self.state.day = day
            self.state.day_anchor_equity = str(equity)
            changed = True
            events.append(f"new day {day}: anchor {equity}")
        if equity > Decimal(self.state.high_water):
            self.state.high_water = str(equity)
            changed = True
        anchor, hw = Decimal(self.state.day_anchor_equity), Decimal(self.state.high_water)
        r = self.policy.risk
        if anchor > 0 and (anchor - equity) / anchor >= r.daily_loss_stop_pct and "daily_loss" not in self.state.latched:
            events.append(self._latch("daily_loss", f"equity {equity} is {(anchor - equity) / anchor:.2%} below "
                                                    f"today's anchor {anchor}", now))  # fmt: skip
        if hw > 0 and (hw - equity) / hw >= r.drawdown_stop_pct and "drawdown" not in self.state.latched:
            events.append(self._latch("drawdown", f"equity {equity} is {(hw - equity) / hw:.2%} below high-water {hw}", now))
        if changed:
            self._save(None, now)
        return events

    def _latch(self, kind: str, reason: str, now: datetime) -> str:
        self.state.latched[kind] = {"at": iso(now), "reason": reason}
        self._save((f"latch:{kind}", reason), now)
        self.store.alert("critical", f"risk latch {kind}: {reason}", now)
        return f"LATCHED {kind}: {reason}"

    def latch(self, kind: str, reason: str, now: datetime) -> None:
        if kind not in self.state.latched:
            self._latch(kind, reason, now)

    def reset(self, kind: str, reviewed_by: str, note: str, now: datetime, reanchor_equity: Decimal | None = None) -> None:
        if not reviewed_by.strip() or not note.strip():
            raise ValueError("a reviewer name and a note are required to reset a risk latch")
        if kind not in self.state.latched:
            raise KeyError(f"{kind} is not latched")
        del self.state.latched[kind]
        if reanchor_equity is not None:
            if kind == "daily_loss":
                self.state.day_anchor_equity = str(reanchor_equity)
            elif kind == "drawdown":
                self.state.high_water = str(reanchor_equity)
        self._save((f"reset:{kind}", json.dumps({"by": reviewed_by, "note": note, "reanchor": str(reanchor_equity)})), now)

    def record_cash_flow(self, amount: Decimal, note: str, now: datetime) -> None:
        """External deposit (+) or withdrawal (-): shift anchors so it isn't counted as P&L."""
        self.state.day_anchor_equity = str(Decimal(self.state.day_anchor_equity) + amount)
        self.state.high_water = str(Decimal(self.state.high_water) + amount)
        self._save(("cash_flow", f"{amount} {note}"), now)

    # ----- kill switch ----------------------------------------------------------------------
    def set_block_entries(self, blocked: bool, actor: str, reason: str, now: datetime) -> None:
        if not actor.strip():
            raise ValueError("actor required")
        self.state.block_entries = blocked
        self.state.block_reason = reason if blocked else ""
        self._save(("kill_switch:block_entries", json.dumps({"blocked": blocked, "by": actor, "reason": reason})), now)

    def request_flatten(self, actor: str, now: datetime) -> None:
        if not actor.strip():
            raise ValueError("actor required")
        self.state.flatten_requested = True
        self.state.block_entries = True
        self.state.block_reason = self.state.block_reason or f"flatten requested by {actor}"
        self._save(("kill_switch:flatten", actor), now)

    def clear_flatten(self, actor: str, now: datetime) -> None:
        self.state.flatten_requested = False
        self._save(("kill_switch:flatten_cleared", actor), now)

    def entry_blockers(self) -> list[str]:
        out = [f"latched {k}: {v['reason']}" for k, v in self.state.latched.items()]
        if self.state.block_entries:
            out.append(f"entries blocked: {self.state.block_reason or 'manual'}")
        if self.state.flatten_requested:
            out.append("flatten in progress")
        if self.policy.mode == "live" and not self.live_approval_ok:
            out.append("live mode without a valid approval and passed acceptance gates")
        return out

    # ----- entries ----------------------------------------------------------------------------
    def check_entry(self, req: EntryRequest, view: ExposureView) -> RiskDecision:
        r = self.policy.risk
        reasons = self.entry_blockers()
        if view.blocking_orders[0]:
            reasons.append(view.blocking_orders[1])
        if not view.quote_ok[0]:
            reasons.append(f"market data: {view.quote_ok[1]}")
        if not req.screening_ok:
            reasons.append(f"screening: {req.screening_reason}")
        if req.asset_key.startswith("cex:") and not any(m.key == req.asset_key for m in self.policy.cex_allowlist):
            reasons.append("market is not on the reviewed allowlist")
        if req.rules is None:
            reasons.append("market precision rules not validated")
        if req.asset_key in view.position_values or req.asset_key in view.pending_entry_values:
            reasons.append("already holding or entering this asset (no averaging down or pyramiding)")
        occupied = set(view.position_values) | set(view.pending_entry_values)
        if len(occupied) >= r.max_positions:
            reasons.append(f"max {r.max_positions} positions")
        if not (ZERO < req.stop < req.entry_price):
            reasons.append(f"invalid stop {req.stop} for entry {req.entry_price}")
        if view.equity <= 0:
            reasons.append("non-positive equity")
        if reasons:
            return RiskDecision(False, reasons=reasons)

        e, price = view.equity, req.entry_price
        per_unit_risk = (price - req.stop) + price * r.cost_allowance_pct
        total_exposure = sum(view.position_values.values(), ZERO) + sum(view.pending_entry_values.values(), ZERO)
        caps = {
            "risk": e * r.risk_per_trade_pct / per_unit_risk,
            "asset": e * r.max_asset_exposure_pct / price,
            "total": (e * r.max_total_exposure_pct - total_exposure) / price,
            "cash": view.cash / (price * (1 + self.policy.execution.taker_fee_pct)),
        }
        group = r.group_of(req.asset_key)
        if group:
            members = dict(r.correlation_groups)[group]
            used = sum((v for k, v in {**view.position_values, **view.pending_entry_values}.items() if k in members), ZERO)
            caps["group"] = (e * r.max_group_exposure_pct - used) / price
        binding = min(caps, key=lambda k: caps[k])
        raw_qty = max(caps[binding], ZERO)
        rules = req.rules
        step = Decimal(rules.base_increment)
        qty = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        details = {k: str(v) for k, v in caps.items()} | {"binding": binding, "per_unit_risk": str(per_unit_risk),
                                                          "planned_risk": str(qty * per_unit_risk)}  # fmt: skip
        if qty <= 0 or qty < Decimal(rules.base_min_size) or qty * price < Decimal(rules.quote_min_size):
            return RiskDecision(False, reasons=[f"size {qty} impossible under {binding} cap and venue minimums "
                                                f"(min {rules.base_min_size}, min notional {rules.quote_min_size})"],
                                details=details)  # fmt: skip
        return RiskDecision(True, qty, [f"sized by {binding} cap"], details)

    @staticmethod
    def check_exit(qty: Decimal, held: Decimal) -> tuple[bool, str]:
        """Exits are never blocked by entry controls; only overselling is refused."""
        if qty <= 0:
            return False, "non-positive exit quantity"
        if qty > held:
            return False, f"exit {qty} exceeds held {held}"
        return True, "ok"
