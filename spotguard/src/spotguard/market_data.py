"""Market data: L2 order books from the Coinbase Advanced Trade WebSocket (or a recorded replay).

Message format (wss://advanced-trade-ws.coinbase.com, channel "level2" → messages on "l2_data"):
    {"channel": "l2_data", "timestamp": "...Z", "sequence_num": N,
     "events": [{"type": "snapshot"|"update", "product_id": "BTC-USD",
                 "updates": [{"side": "bid"|"offer", "event_time": "...", "price_level": "...",
                              "new_quantity": "..."}]}]}
new_quantity is the full size at that level ("0" removes it). sequence_num increases by one per
message on a connection; any jump is a gap and the book is rebuilt from a fresh snapshot.

Receipt timing uses time.monotonic_ns() and is kept separate from exchange timestamps. Books that
are stale, crossed, skewed, gapped or awaiting a snapshot are not quotable: no decisions and no
paper fills are made from them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from spotguard.config import MarketPolicy
from spotguard.identities import CexMarketId

log = logging.getLogger(__name__)
COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"


class BookStatus(StrEnum):
    AWAITING_SNAPSHOT = "AWAITING_SNAPSHOT"
    LIVE = "LIVE"
    GAP = "GAP"
    CROSSED = "CROSSED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class Quote:
    """Immutable top-of-book view handed to strategy, risk and the paper exchange."""

    market_key: str
    bids: tuple[tuple[Decimal, Decimal], ...]  # best first
    asks: tuple[tuple[Decimal, Decimal], ...]
    status: BookStatus
    exchange_ts: datetime | None
    recv_mono_ns: int
    recv_wall: datetime
    seq: int

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.best_ask - self.best_bid) / mid  # type: ignore[operator]

    def age_s(self, now_mono_ns: int) -> float:
        return (now_mono_ns - self.recv_mono_ns) / 1e9


def quote_usable(q: Quote | None, now_mono_ns: int, now_wall: datetime, policy: MarketPolicy) -> tuple[bool, str]:
    """Whether a quote may be used for decisions or simulated fills."""
    if q is None:
        return False, "no quote"
    if q.status is not BookStatus.LIVE:
        return False, f"book {q.status.value}"
    if not q.bids or not q.asks:
        return False, "one side of the book is empty"
    if q.best_bid >= q.best_ask:  # type: ignore[operator]
        return False, "crossed book"
    age = q.age_s(now_mono_ns)
    if age > float(policy.max_quote_age_s) or age < 0:
        return False, f"quote age {age:.2f}s exceeds {policy.max_quote_age_s}s"
    if q.exchange_ts is None:
        return False, "no exchange timestamp (clock skew can't be checked)"
    skew = abs((q.recv_wall - q.exchange_ts).total_seconds())
    if skew > float(policy.max_clock_skew_s):
        return False, f"exchange/local clock skew {skew:.1f}s"
    spread = q.spread_pct
    if spread is None or spread > policy.max_spread_pct:
        return False, f"spread {spread} exceeds {policy.max_spread_pct}"
    return True, "ok"


class OrderBook:
    def __init__(self, market_key: str):
        self.market_key = market_key
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.status = BookStatus.AWAITING_SNAPSHOT
        self.exchange_ts: datetime | None = None
        self.recv_mono_ns = 0
        self.recv_wall = datetime.fromtimestamp(0, UTC)
        self.seq = -1

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.status = BookStatus.AWAITING_SNAPSHOT

    def apply(self, side: str, price: Decimal, qty: Decimal) -> None:
        book = self.bids if side == "bid" else self.asks
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty

    def crossed(self) -> bool:
        return bool(self.bids and self.asks and max(self.bids) >= min(self.asks))

    def quote(self, depth: int = 50) -> Quote:
        bids = tuple(sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth])
        asks = tuple(sorted(self.asks.items(), key=lambda kv: kv[0])[:depth])
        return Quote(self.market_key, bids, asks, self.status, self.exchange_ts, self.recv_mono_ns, self.recv_wall,
                     self.seq)  # fmt: skip


@dataclass
class FeedEvent:
    kind: str  # "book", "gap", "duplicate", "heartbeat", "error", "alert", "resync"
    market_key: str | None = None
    detail: str = ""


_FRACTION = re.compile(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    # Coinbase sends nanoseconds; datetime keeps microseconds. Trim only the fraction digits.
    m = _FRACTION.match(text)
    if m:
        text = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else None


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


class CoinbaseL2Handler:
    """Pure message handler (no I/O) so feed logic is fully testable."""

    def __init__(self, markets: list[CexMarketId]):
        self.by_product = {m.market_id: m for m in markets}
        self.books = {m.key: OrderBook(m.key) for m in markets}
        self.last_seq: int | None = None
        self.last_message_mono_ns = 0
        self.needs_resync = False

    def quotes(self) -> dict[str, Quote]:
        return {k: b.quote() for k, b in self.books.items()}

    def invalidate_all(self, status: BookStatus) -> None:
        for book in self.books.values():
            book.reset()
            book.status = status
        self.needs_resync = True

    def on_reconnect(self) -> None:
        self.last_seq = None
        self.needs_resync = False
        for book in self.books.values():
            book.reset()

    def handle(self, raw: str | bytes | dict, recv_mono_ns: int, recv_wall: datetime) -> list[FeedEvent]:
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            return [FeedEvent("alert", detail="malformed JSON message")]
        if not isinstance(msg, dict):
            return [FeedEvent("alert", detail="non-object message")]
        self.last_message_mono_ns = recv_mono_ns
        if msg.get("type") == "error":
            self.invalidate_all(BookStatus.INVALID)
            return [FeedEvent("error", detail=str(msg.get("message", "unknown error")))]
        seq = msg.get("sequence_num")
        if isinstance(seq, int) and not isinstance(seq, bool):
            if self.last_seq is not None:
                if seq <= self.last_seq:
                    return [FeedEvent("duplicate", detail=f"seq {seq} <= {self.last_seq}")]
                if seq != self.last_seq + 1:
                    expected = self.last_seq + 1
                    self.last_seq = seq
                    self.invalidate_all(BookStatus.GAP)
                    return [FeedEvent("gap", detail=f"expected seq {expected}, got {seq}")]
            self.last_seq = seq
        channel = msg.get("channel")
        if channel == "heartbeats":
            return [FeedEvent("heartbeat")]
        if channel != "l2_data":
            return []
        exchange_ts = _parse_ts(msg.get("timestamp"))
        out: list[FeedEvent] = []
        for event in msg.get("events") or []:
            if not isinstance(event, dict):
                out.append(FeedEvent("alert", detail="malformed event"))
                continue
            product = event.get("product_id")
            market = self.by_product.get(product)
            if market is None:
                # e.g. subscribing to BTC-USDC can return BTC-USD (aliases): never silently relabel.
                out.append(FeedEvent("alert", detail=f"unexpected product {product!r} (alias?) ignored"))
                continue
            book = self.books[market.key]
            kind = event.get("type")
            if kind == "snapshot":
                book.reset()
            elif kind != "update" or book.status is not BookStatus.LIVE:
                continue  # updates before a snapshot (or after a gap) can't be applied
            bad = False
            for u in event.get("updates") or []:
                side = u.get("side") if isinstance(u, dict) else None
                price = _dec(u.get("price_level")) if isinstance(u, dict) else None
                qty = _dec(u.get("new_quantity")) if isinstance(u, dict) else None
                if side not in ("bid", "offer") or price is None or price == 0 or qty is None:
                    bad = True
                    break
                book.apply("bid" if side == "bid" else "ask", price, qty)
            book.exchange_ts = exchange_ts
            book.recv_mono_ns = recv_mono_ns
            book.recv_wall = recv_wall
            book.seq = seq if isinstance(seq, int) else book.seq
            if bad:
                book.reset()
                book.status = BookStatus.INVALID
                self.needs_resync = True
                out.append(FeedEvent("resync", market.key, "malformed level update"))
            elif book.crossed():
                book.status = BookStatus.CROSSED
                self.needs_resync = True
                out.append(FeedEvent("resync", market.key, "crossed book"))
            else:
                book.status = BookStatus.LIVE
                out.append(FeedEvent("book", market.key))
        return out


def subscribe_messages(markets: list[CexMarketId]) -> list[dict]:
    products = [m.market_id for m in markets]
    return [
        {"type": "subscribe", "product_ids": products, "channel": "level2"},
        {"type": "subscribe", "product_ids": products, "channel": "heartbeats"},
    ]


@dataclass
class FeedStats:
    connects: int = 0
    gaps: int = 0
    duplicates: int = 0
    resyncs: int = 0
    messages: int = 0
    last_error: str = ""


@dataclass
class CoinbaseFeed:
    """Reconnecting WebSocket client. Public market data; no API key needed (Coinbase marks this beta)."""

    markets: list[CexMarketId]
    policy: MarketPolicy
    on_event: Callable[[FeedEvent, CoinbaseL2Handler], None]
    url: str = COINBASE_WS
    handler: CoinbaseL2Handler = field(init=False)
    stats: FeedStats = field(default_factory=FeedStats)
    connect: Callable[..., Any] | None = None  # injectable for tests

    def __post_init__(self) -> None:
        self.handler = CoinbaseL2Handler(self.markets)

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        connect = self.connect or websockets.connect
        backoff = 1.0
        timeout = float(self.policy.heartbeat_timeout_s)
        while not stop.is_set():
            try:
                async with connect(self.url, max_size=2**24, ping_interval=20) as ws:
                    self.stats.connects += 1
                    self.handler.on_reconnect()
                    for sub in subscribe_messages(self.markets):  # must arrive within 5 s of connecting
                        await ws.send(json.dumps(sub))
                    backoff = 1.0
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout)
                        self.stats.messages += 1
                        for ev in self.handler.handle(raw, time.monotonic_ns(), datetime.now(UTC)):
                            if ev.kind == "gap":
                                self.stats.gaps += 1
                            elif ev.kind == "duplicate":
                                self.stats.duplicates += 1
                            elif ev.kind == "resync":
                                self.stats.resyncs += 1
                            self.on_event(ev, self.handler)
                        if self.handler.needs_resync:
                            log.warning("feed needs resync; reconnecting for a fresh snapshot")
                            break
                await asyncio.sleep(0.5)  # brief pause before resubscribing after a resync
            except (TimeoutError, OSError, Exception) as exc:  # noqa: BLE001 - any failure → backoff + reconnect
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.handler.invalidate_all(BookStatus.INVALID)
                self.on_event(FeedEvent("error", detail=self.stats.last_error), self.handler)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 60.0)
