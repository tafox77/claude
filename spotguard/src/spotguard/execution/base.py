"""Types shared by the order manager and execution adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}
OPEN_STATES = tuple(
    s.value for s in (OrderState.CREATED, OrderState.SUBMITTING, OrderState.ACKNOWLEDGED, OrderState.PARTIAL,
                      OrderState.CANCEL_PENDING, OrderState.UNKNOWN)
)  # fmt: skip

ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.SUBMITTING, OrderState.REJECTED, OrderState.UNKNOWN},  # UNKNOWN: crash recovery
    OrderState.SUBMITTING: {OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED,
                            OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.CANCELED,
                              OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.PARTIAL: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.CANCELED,
                         OrderState.UNKNOWN},
    OrderState.CANCEL_PENDING: {OrderState.CANCELED, OrderState.FILLED, OrderState.PARTIAL, OrderState.CANCEL_PENDING,
                                OrderState.UNKNOWN},
    OrderState.UNKNOWN: {OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED,
                         OrderState.REJECTED, OrderState.UNKNOWN, OrderState.CANCEL_PENDING},
}  # fmt: skip


def can_transition(a: OrderState, b: OrderState) -> bool:
    return b in ALLOWED.get(a, set())


class VenueStatus(StrEnum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"  # includes IOC remainder expiry
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    market_key: str
    side: str  # buy | sell
    qty: Decimal
    limit_price: Decimal
    time_in_force: str  # IOC | GTC


@dataclass(frozen=True)
class VenueFill:
    fill_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    liquidity: str  # maker | taker
    at: datetime


@dataclass
class VenueOrderView:
    """What the venue says about an order (submit ack or lookup)."""

    client_order_id: str
    exchange_order_id: str | None
    status: VenueStatus
    fills: list[VenueFill] = field(default_factory=list)
    reason: str = ""


class AdapterError(RuntimeError):
    """A request whose outcome is unknown (timeout, disconnect). Never treat as rejection."""


class ExecutionAdapter(Protocol):
    label: str  # "PAPER", "SANDBOX", "LIVE", "SIMULATION"

    async def submit(self, req: OrderRequest) -> VenueOrderView: ...

    async def cancel(self, client_order_id: str) -> VenueOrderView | None: ...

    async def lookup(self, client_order_id: str) -> VenueOrderView | None: ...  # None: venue has no such order

    async def balances(self) -> dict[str, Decimal]: ...
