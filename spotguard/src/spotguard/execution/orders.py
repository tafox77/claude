"""Order manager: persisted intents and client order ids, state machine, reconciliation.

Rules:
* Every order is persisted (CREATED) before it is sent. Client order ids are unique and stored.
* A timeout or disconnect during submit/cancel makes the order UNKNOWN, never "failed".
  UNKNOWN orders are reconciled by client id and fills before anything else is retried, and any
  UNKNOWN order blocks new exposure.
* A cancel request is never assumed to succeed; fills that arrive during cancellation are applied.
* Fills are idempotent (deduplicated by venue fill id) and update order, position and cash atomically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from spotguard.execution.base import (
    OPEN_STATES,
    AdapterError,
    ExecutionAdapter,
    OrderRequest,
    OrderState,
    VenueOrderView,
    VenueStatus,
    can_transition,
)
from spotguard.storage import D, LedgerError, Store, iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Intent:
    asset_key: str
    side: str
    purpose: str  # entry | exit | emergency_exit
    qty: Decimal
    limit_price: Decimal
    time_in_force: str
    reason: str
    policy_version: str
    model_version: str | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    decision: dict = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: "int-" + uuid.uuid4().hex)


class OrderManager:
    def __init__(
        self,
        store: Store,
        adapter: ExecutionAdapter,
        quote_currency: str,
        ack_timeout_s: float,
        clock: Callable[[], datetime],
        not_found_grace_s: float = 30.0,
    ):
        self.store = store
        self.adapter = adapter
        self.quote_currency = quote_currency
        self.ack_timeout_s = ack_timeout_s
        self.clock = clock
        self.not_found_grace_s = not_found_grace_s
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, cid: str) -> asyncio.Lock:
        return self._locks.setdefault(cid, asyncio.Lock())

    # ----- queries --------------------------------------------------------------------------
    def open_orders(self, purpose: str | None = None) -> list:
        rows = self.store.orders_in(OPEN_STATES)
        return [r for r in rows if purpose is None or r["purpose"] == purpose]

    def unknown_orders(self) -> list:
        return self.store.orders_in((OrderState.UNKNOWN.value,))

    def blocks_new_exposure(self) -> tuple[bool, str]:
        # A cancel whose outcome isn't confirmed is as uncertain as an UNKNOWN order.
        unsure = self.store.orders_in((OrderState.UNKNOWN.value, OrderState.CANCEL_PENDING.value))
        if unsure:
            return True, f"{len(unsure)} order(s) UNKNOWN or awaiting cancel confirmation"
        return False, ""

    def has_open_order(self, asset_key: str) -> bool:
        return any(r["asset_key"] == asset_key for r in self.open_orders())

    # ----- placing ----------------------------------------------------------------------------
    async def place(self, intent: Intent) -> str:
        now = self.clock()
        cid = "sg-" + uuid.uuid4().hex
        self.store.insert_intent(
            {
                "intent_id": intent.intent_id,
                "created_at": iso(now),
                "asset_key": intent.asset_key,
                "side": intent.side,
                "purpose": intent.purpose,
                "qty": str(intent.qty),
                "limit_price": str(intent.limit_price),
                "reason": intent.reason,
                "policy_version": intent.policy_version,
                "model_version": intent.model_version,
                "decision": json.dumps(intent.decision, default=str),
                "stop": str(intent.stop) if intent.stop is not None else None,
                "target": str(intent.target) if intent.target is not None else None,
            }
        )
        self.store.insert_order(
            {
                "client_order_id": cid,
                "intent_id": intent.intent_id,
                "asset_key": intent.asset_key,
                "side": intent.side,
                "purpose": intent.purpose,
                "qty": str(intent.qty),
                "limit_price": str(intent.limit_price),
                "time_in_force": intent.time_in_force,
                "state": OrderState.CREATED.value,
                "created_at": iso(now),
                "updated_at": iso(now),
            }
        )
        async with self._lock(cid):
            self._move(cid, OrderState.SUBMITTING, "sending")
            req = OrderRequest(cid, intent.asset_key, intent.side, intent.qty, intent.limit_price, intent.time_in_force)
            try:
                view = await asyncio.wait_for(self.adapter.submit(req), self.ack_timeout_s)
            except (TimeoutError, AdapterError, OSError) as exc:
                self._move(cid, OrderState.UNKNOWN, f"submit outcome unknown: {type(exc).__name__}: {exc}")
                self.store.alert("warning", f"order {cid} outcome unknown; reconciling before any retry", self.clock())
                return cid
            self._absorb(cid, view)
        return cid

    # ----- reconciliation ---------------------------------------------------------------------
    async def reconcile(self, cid: str) -> OrderState:
        async with self._lock(cid):
            row = self.store.order(cid)
            if row is None:
                raise KeyError(cid)
            state = OrderState(row["state"])
            if state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED):
                return state
            try:
                view = await asyncio.wait_for(self.adapter.lookup(cid), self.ack_timeout_s)
            except (TimeoutError, AdapterError, OSError) as exc:
                if state is not OrderState.UNKNOWN and state is not OrderState.CANCEL_PENDING:
                    self._move(cid, OrderState.UNKNOWN, f"lookup failed: {exc}")
                return OrderState(self.store.order(cid)["state"])
            if view is None:
                age = (self.clock() - datetime.fromisoformat(row["created_at"])).total_seconds()
                if age >= self.not_found_grace_s:
                    # The venue has no record after the grace period. Without fills the order never arrived;
                    # with fills already booked, the venue lost the remainder (e.g. a paper venue restart).
                    if D(row["filled_qty"]) > 0:
                        self._move(cid, OrderState.CANCELED, "venue has no record; remainder treated as canceled")
                    else:
                        self._move(cid, OrderState.REJECTED, "venue has no record of this client order id")
                return OrderState(self.store.order(cid)["state"])
            self._absorb(cid, view)
            return OrderState(self.store.order(cid)["state"])

    async def reconcile_all(self) -> None:
        for row in self.open_orders():
            if row["state"] == OrderState.CREATED.value:
                # Persisted but never sent (crash between insert and send): it cannot exist at the venue
                # unless lookup finds it, so look it up rather than resend.
                self._move(row["client_order_id"], OrderState.UNKNOWN, "found CREATED at startup")
            await self.reconcile(row["client_order_id"])

    # ----- cancel -----------------------------------------------------------------------------
    async def cancel(self, cid: str) -> OrderState:
        async with self._lock(cid):
            row = self.store.order(cid)
            state = OrderState(row["state"])
            if state not in (
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIAL,
                OrderState.UNKNOWN,
                OrderState.CANCEL_PENDING,
            ):
                return state
            self._move(cid, OrderState.CANCEL_PENDING, "cancel requested")
            try:
                view = await asyncio.wait_for(self.adapter.cancel(cid), self.ack_timeout_s)
            except (TimeoutError, AdapterError, OSError) as exc:
                self._move(cid, OrderState.UNKNOWN, f"cancel outcome unknown: {exc}")
                return OrderState.UNKNOWN
            if view is not None:
                self._absorb(cid, view)
        # Never trust the cancel response alone: confirm with a lookup (fills may have raced the cancel).
        return await self.reconcile(cid)

    async def cancel_all(self, purpose: str | None = None) -> None:
        for row in self.open_orders(purpose):
            await self.cancel(row["client_order_id"])

    # ----- internals --------------------------------------------------------------------------
    def _move(self, cid: str, to: OrderState, detail: str, **fields) -> bool:
        row = self.store.order(cid)
        current = OrderState(row["state"])
        if current == to and to not in (OrderState.PARTIAL, OrderState.CANCEL_PENDING, OrderState.UNKNOWN):
            return False
        if not can_transition(current, to):
            log.error("illegal order transition %s -> %s for %s (%s)", current, to, cid, detail)
            self.store.alert("error", f"illegal transition {current.value}->{to.value} for {cid}: {detail}", self.clock())
            return False
        return self.store.transition(cid, (current.value,), to.value, self.clock(), detail, **fields)

    def _absorb(self, cid: str, view: VenueOrderView) -> None:
        for f in view.fills:
            try:
                self.store.apply_fill(f.fill_id, cid, f.qty, f.price, f.fee, f.liquidity, f.at, self.quote_currency)
            except LedgerError as exc:
                self.store.alert("critical", f"ledger rejected fill {f.fill_id}: {exc}", self.clock())
                self._move(cid, OrderState.UNKNOWN, f"ledger error: {exc}")
                return
        row = self.store.order(cid)
        filled, qty = D(row["filled_qty"]), D(row["qty"])
        extra = {"exchange_order_id": view.exchange_order_id} if view.exchange_order_id else {}
        if view.status is VenueStatus.REJECTED and filled == 0:
            self._move(cid, OrderState.REJECTED, view.reason or "rejected by venue", last_error=view.reason, **extra)
        elif view.status is VenueStatus.FILLED or filled >= qty:
            if filled < qty:
                self._move(cid, OrderState.UNKNOWN, "venue says filled but fills are missing", **extra)
            else:
                self._move(cid, OrderState.FILLED, "filled", **extra)
        elif view.status is VenueStatus.CANCELED:
            self._move(cid, OrderState.CANCELED, f"canceled with {filled} filled", **extra)
        elif filled > 0:
            self._move(cid, OrderState.PARTIAL, f"{filled}/{qty} filled", **extra)
        elif OrderState(row["state"]) is not OrderState.CANCEL_PENDING:
            self._move(cid, OrderState.ACKNOWLEDGED, "accepted", **extra)


def remaining_qty(row) -> Decimal:
    return D(row["qty"]) - D(row["filled_qty"])


def fill_summary(store: Store, cid: str) -> tuple[Decimal, Decimal]:
    row = store.order(cid)
    filled = D(row["filled_qty"])
    avg = D(row["filled_notional"]) / filled if filled else Decimal(0)
    return filled, avg


__all__ = ["Intent", "OrderManager", "remaining_qty", "fill_summary"]
