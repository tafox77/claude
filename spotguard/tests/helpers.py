"""Shared fakes: a controllable clock, book builders and product rules (mock data, not venue data)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from conftest import NOW

from spotguard.identities import CexMarketId, ProductRules
from spotguard.market_data import BookStatus, OrderBook, Quote

BTC = CexMarketId("coinbase", "BTC-USD", "BTC", "USD")
ETH = CexMarketId("coinbase", "ETH-USD", "ETH", "USD")


@dataclass
class Clock:
    mono: int = 1_000_000_000_000
    wall: datetime = NOW

    def advance(self, seconds: float) -> None:
        self.mono += int(seconds * 1e9)
        self.wall += timedelta(seconds=seconds)

    def mono_ns(self) -> int:
        return self.mono

    def now(self) -> datetime:
        return self.wall


def rules(market: CexMarketId = BTC) -> ProductRules:
    return ProductRules(market, "0.00000001", "0.01", "0.01", "0.00000001", "1", NOW)


def book(clock: Clock, market: CexMarketId = BTC, bid="100000", ask="100010", depth=("0.5", "0.5", "1"),
         step="10") -> Quote:  # fmt: skip
    b = OrderBook(market.key)
    for i, size in enumerate(depth):
        b.apply("bid", Decimal(bid) - i * Decimal(step), Decimal(size))
        b.apply("ask", Decimal(ask) + i * Decimal(step), Decimal(size))
    b.status = BookStatus.LIVE
    b.recv_mono_ns = clock.mono
    b.recv_wall = clock.wall
    b.exchange_ts = clock.wall
    return b.quote()


class Quotes:
    """Mutable quote source keyed by market."""

    def __init__(self):
        self.q: dict[str, Quote] = {}

    def __call__(self, key: str) -> Quote | None:
        return self.q.get(key)

    def set(self, quote: Quote) -> None:
        self.q[quote.market_key] = quote


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
