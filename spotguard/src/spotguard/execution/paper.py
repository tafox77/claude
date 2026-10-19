"""Paper venue: simulated fills against the live (or replayed) L2 book.

Models: marketable limit orders walking real book depth (partial fills when depth runs out),
IOC/GTC time-in-force, quantity/price increments and minimum sizes, maker/taker fees, latency,
and injectable failures (rejects, lost submits, acks lost after acceptance, fills racing cancels).
It never fabricates a fill from a stale, crossed or empty book.

Paper fills are optimistic relative to live trading: queue position, hidden liquidity, other
participants reacting, and venue outages are not modelled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_UP, Decimal

from spotguard.config import ExecutionPolicy, MarketPolicy
from spotguard.execution.base import AdapterError, OrderRequest, VenueFill, VenueOrderView, VenueStatus
from spotguard.identities import ProductRules
from spotguard.market_data import Quote, quote_usable


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return step > 0 and (value / step) == (value / step).to_integral_value()


@dataclass
class FailurePlan:
    """Inject failures for the Nth submitted order (1-based) or for every order if n is None."""

    kind: str  # reject | drop_before_accept | timeout_after_accept | fill_during_cancel | lookup_error | cancel_timeout
    n: int | None = None
    used: bool = False

    def matches(self, count: int) -> bool:
        return not self.used and (self.n is None or self.n == count)


@dataclass
class _VenueOrder:
    req: OrderRequest
    status: VenueStatus
    fills: list[VenueFill] = field(default_factory=list)
    reason: str = ""
    exchange_order_id: str = ""

    @property
    def filled(self) -> Decimal:
        return sum((f.qty for f in self.fills), Decimal(0))

    def view(self) -> VenueOrderView:
        return VenueOrderView(self.req.client_order_id, self.exchange_order_id, self.status, list(self.fills), self.reason)


class PaperVenue:
    label = "PAPER"

    def __init__(
        self,
        quotes: Callable[[str], Quote | None],
        rules: dict[str, ProductRules],
        execution: ExecutionPolicy,
        market: MarketPolicy,
        mono_ns: Callable[[], int],
        wall: Callable[[], datetime],
        initial_cash: dict[str, Decimal],
        latency_s: float = 0.0,
        failures: list[FailurePlan] | None = None,
    ):
        self.quotes = quotes
        self.rules = rules
        self.execution = execution
        self.market = market
        self.mono_ns = mono_ns
        self.wall = wall
        self.latency_s = latency_s
        self.failures = failures or []
        self.orders: dict[str, _VenueOrder] = {}
        self.cash = dict(initial_cash)
        self.holdings: dict[str, Decimal] = {}
        self.submits = 0

    def restore(self, cash: dict[str, Decimal], holdings: dict[str, Decimal]) -> None:
        """Resume a durable paper account from the persisted ledger after a restart."""
        self.cash = dict(cash)
        self.holdings = dict(holdings)

    def _failure(self, kind: str) -> FailurePlan | None:
        for f in self.failures:
            if f.kind == kind and f.matches(self.submits):
                f.used = True
                return f
        return None

    async def submit(self, req: OrderRequest) -> VenueOrderView:
        self.submits += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self._failure("drop_before_accept"):
            raise AdapterError("connection reset before the venue received the order")
        if req.client_order_id in self.orders:  # venue-side idempotency on client order id
            return self.orders[req.client_order_id].view()
        order = _VenueOrder(req, VenueStatus.OPEN, exchange_order_id=f"paper-{self.submits}")
        self.orders[req.client_order_id] = order
        if self._failure("reject"):
            order.status, order.reason = VenueStatus.REJECTED, "injected rejection"
            return order.view()
        problem = self._validate(req)
        if problem:
            order.status, order.reason = VenueStatus.REJECTED, problem
            return order.view()
        if self._failure("fill_during_cancel"):
            order.reason = "resting (fill will race the cancel)"
            return order.view()
        self._match(order, taker=True)
        if req.time_in_force == "IOC" and order.status is VenueStatus.OPEN:
            order.status = VenueStatus.CANCELED if order.filled < req.qty else VenueStatus.FILLED
        if self._failure("timeout_after_accept"):
            raise AdapterError("ack lost after the venue accepted the order")
        return order.view()

    def _validate(self, req: OrderRequest) -> str:
        rules = self.rules.get(req.market_key)
        if rules is None:
            return "market not validated"
        if req.qty <= 0 or req.limit_price <= 0:
            return "non-positive quantity or price"
        if not is_multiple(req.qty, Decimal(rules.base_increment)):
            return f"quantity {req.qty} not a multiple of {rules.base_increment}"
        if not is_multiple(req.limit_price, Decimal(rules.price_increment)):
            return f"price {req.limit_price} not a multiple of {rules.price_increment}"
        if req.qty < Decimal(rules.base_min_size):
            return f"quantity below minimum {rules.base_min_size}"
        if req.qty * req.limit_price < Decimal(rules.quote_min_size):
            return f"notional below minimum {rules.quote_min_size}"
        if req.side == "sell" and req.qty > self.holdings.get(req.market_key, Decimal(0)):
            return "insufficient balance to sell"
        quote = self.quotes(req.market_key)
        ok, why = quote_usable(quote, self.mono_ns(), self.wall(), self.market)
        if not ok and why.startswith("spread") and quote is not None:
            ok = True  # a wide spread is a risk decision, not a venue rejection
        if not ok:
            return f"no usable book ({why})"
        return ""

    def _fee(self, market_key: str, notional: Decimal, maker: bool) -> Decimal:
        """Fee in quote currency, rounded up to the quote increment (venues round fees against you)."""
        rate = self.execution.maker_fee_pct if maker else self.execution.taker_fee_pct
        step = Decimal(self.rules[market_key].quote_increment)
        return (notional * rate / step).to_integral_value(rounding=ROUND_UP) * step

    def _match(self, order: _VenueOrder, taker: bool) -> None:
        req = order.req
        quote = self.quotes(req.market_key)
        if quote is None:
            return
        levels = quote.asks if req.side == "buy" else quote.bids
        remaining = req.qty - order.filled
        quote_ccy = req.market_key.rsplit("-", 1)[1]
        for price, size in levels:
            if remaining <= 0:
                break
            if (req.side == "buy" and price > req.limit_price) or (req.side == "sell" and price < req.limit_price):
                break
            # Account for depth already consumed by this order's earlier fills at this level.
            used = sum((f.qty for f in order.fills if f.price == price), Decimal(0))
            take = min(remaining, size - used)
            if take <= 0:
                continue
            notional = take * price
            fee = self._fee(req.market_key, notional, maker=not taker)
            if req.side == "buy" and notional + fee > self.cash.get(quote_ccy, Decimal(0)):
                order.reason = "insufficient cash for remainder"
                break
            n = len(order.fills) + 1
            order.fills.append(VenueFill(f"{req.client_order_id}-f{n}", take, price, fee, "taker" if taker else "maker",
                                         self.wall()))  # fmt: skip
            sign = 1 if req.side == "buy" else -1
            self.cash[quote_ccy] = self.cash.get(quote_ccy, Decimal(0)) - sign * notional - fee
            self.holdings[req.market_key] = self.holdings.get(req.market_key, Decimal(0)) + sign * take
            remaining -= take
        if order.filled >= req.qty:
            order.status = VenueStatus.FILLED

    def on_quote(self, market_key: str) -> None:
        """Match resting GTC orders when the book moves (maker fills)."""
        ok, _ = quote_usable(self.quotes(market_key), self.mono_ns(), self.wall(), self.market)
        if not ok:
            return
        for order in self.orders.values():
            if order.req.market_key == market_key and order.status is VenueStatus.OPEN and order.req.time_in_force == "GTC":
                self._match(order, taker=False)

    async def cancel(self, client_order_id: str) -> VenueOrderView | None:
        if self._failure("cancel_timeout"):
            raise AdapterError("cancel request timed out")
        order = self.orders.get(client_order_id)
        if order is None:
            return None
        if order.status is VenueStatus.OPEN and order.reason.startswith("resting (fill will race"):
            self._match(order, taker=True)  # the order trades before the cancel lands
            if order.status is VenueStatus.OPEN:
                order.status = VenueStatus.CANCELED
            order.reason = "fill raced the cancel"
            return order.view()
        if order.status is VenueStatus.OPEN:
            order.status = VenueStatus.CANCELED
        return order.view()

    async def lookup(self, client_order_id: str) -> VenueOrderView | None:
        if self._failure("lookup_error"):
            raise AdapterError("lookup timed out")
        order = self.orders.get(client_order_id)
        return order.view() if order else None

    async def balances(self) -> dict[str, Decimal]:
        out = dict(self.cash)
        out.update({k: v for k, v in self.holdings.items() if v})
        return out
