"""Paper trading on live public Coinbase data (no API keys, no real orders).

Startup validates each allowlisted market against Coinbase's public product listing
(GET https://api.coinbase.com/api/v3/brokerage/market/products/{product_id}); any mismatch, alias,
or non-online status aborts. Then the WebSocket feed, strategy, supervisor and marker run as
independent tasks until stopped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from spotguard.config import Policy
from spotguard.execution.paper import PaperVenue
from spotguard.identities import ProductRules, validate_coinbase_product
from spotguard.market_data import CoinbaseFeed, CoinbaseL2Handler, FeedEvent
from spotguard.runtime import QuoteBoard, Runtime
from spotguard.storage import Store

log = logging.getLogger(__name__)
PRODUCTS_URL = "https://api.coinbase.com/api/v3/brokerage/market/products/{product_id}"


async def fetch_rules(policy: Policy, get: Callable[[str], Awaitable[dict]] | None = None) -> dict[str, ProductRules]:
    async def default_get(url: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()

    get = get or default_get
    out = {}
    for m in policy.cex_allowlist:
        product = await get(PRODUCTS_URL.format(product_id=m.market_id))
        out[m.key] = validate_coinbase_product(m, product, datetime.now(UTC))
    return out


async def run_paper(policy: Policy, db_path: str, stop: asyncio.Event, connect=None, get=None, shadow_factory=None) -> Runtime:
    if policy.mode != "paper":
        raise RuntimeError("run_paper only runs in paper mode")
    rules = await fetch_rules(policy, get)
    board = QuoteBoard()
    store = Store(db_path)
    wall = lambda: datetime.now(UTC)  # noqa: E731
    venue = PaperVenue(board, rules, policy.execution, policy.market, time.monotonic_ns, wall,
                       {"USD": policy.risk.virtual_equity})  # fmt: skip
    rt = Runtime(policy, store, venue, board, rules, time.monotonic_ns, wall)
    if shadow_factory is not None:
        rt.shadow = shadow_factory(rt)
    venue.restore({"USD": store.balance("USD")}, {k: p.qty for k, p in store.positions().items()})
    problems = await rt.startup()
    for p in problems:
        log.error(p)

    def on_event(ev: FeedEvent, handler: CoinbaseL2Handler) -> None:
        if ev.kind == "book" and ev.market_key:
            board.update(handler.books[ev.market_key].quote())
            venue.on_quote(ev.market_key)
        elif ev.kind in ("gap", "error", "resync", "alert"):
            store.alert("warning", f"feed {ev.kind}: {ev.detail}", wall())

    feed = CoinbaseFeed(list(policy.cex_allowlist), policy.market, on_event, connect=connect)
    tasks = [
        asyncio.create_task(feed.run(stop), name="feed"),
        asyncio.create_task(rt.strategy_loop(stop), name="strategy"),
        asyncio.create_task(rt.supervisor_loop(stop), name="supervisor"),
        asyncio.create_task(rt.mark_loop(stop), name="marks"),
    ]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    rt.feed_stats = feed.stats  # type: ignore[attr-defined]
    store.record_latency([(k, int(v["p95_ms"] * 1e6), wall().isoformat()) for k, v in rt.latency.summary().items()])
    store.close()
    return rt
