"""Trading runtime: wires feed → quote board → strategy → risk gate → order manager → adapter,
with the exit supervisor, equity/latch marking and control polling as independent tasks.

Design points:
* The strategy queue is bounded and conflating (one pending slot per market): if the strategy
  falls behind, it evaluates the latest book instead of a backlog of stale ones.
* Exits never wait on the strategy queue, screening or learning; the supervisor reads the quote
  board directly on its own schedule.
* One entry at a time per asset (per-asset lock), so concurrent signals can't double-enter.
* At startup: leader lock, order reconciliation, and balance reconciliation against the venue;
  any mismatch blocks entries until reviewed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from spotguard.config import Policy
from spotguard.execution.base import ExecutionAdapter
from spotguard.execution.orders import Intent, OrderManager
from spotguard.identities import ProductRules
from spotguard.latency import LatencyStats
from spotguard.market_data import Quote, quote_usable
from spotguard.risk import EntryRequest, ExposureView, RiskEngine
from spotguard.storage import D, Store
from spotguard.strategies.momentum import MomentumStrategy, Signal
from spotguard.supervisor import ExitSupervisor, round_price

log = logging.getLogger(__name__)


class QuoteBoard:
    """Latest quote per market; written by the feed, read by everyone else."""

    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.listeners: list[Callable[[str], None]] = []

    def __call__(self, key: str) -> Quote | None:
        return self.quotes.get(key)

    def update(self, quote: Quote) -> None:
        self.quotes[quote.market_key] = quote
        for fn in self.listeners:
            fn(quote.market_key)


class ConflatingQueue:
    """Bounded queue of market keys with at most one pending entry per key."""

    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self.pending: dict[str, None] = {}
        self.event = asyncio.Event()
        self.dropped = 0

    def put(self, key: str) -> None:
        if key in self.pending:
            return  # already queued: the consumer will read the latest book anyway
        if len(self.pending) >= self.maxsize:
            self.dropped += 1
            return
        self.pending[key] = None
        self.event.set()

    async def get(self) -> str:
        while not self.pending:
            self.event.clear()
            await self.event.wait()
        key = next(iter(self.pending))
        del self.pending[key]
        return key


@dataclass
class Runtime:
    policy: Policy
    store: Store
    adapter: ExecutionAdapter
    board: QuoteBoard
    rules: dict[str, ProductRules]
    mono_ns: Callable[[], int]
    wall: Callable[[], datetime]
    quote_currency: str = "USD"
    model_version: str | None = None
    screening_gate: Callable[[str, datetime], tuple[bool, str]] | None = None  # DEX assets only
    latency: LatencyStats = field(default_factory=LatencyStats)
    queue: ConflatingQueue = field(default_factory=ConflatingQueue)
    shadow: object | None = None  # learning.shadow.ShadowRunner; observes, never trades

    def __post_init__(self) -> None:
        now = self.wall()
        self.store.set_initial_balance(self.quote_currency, self.policy.risk.virtual_equity, now)
        self.risk = RiskEngine(self.policy, self.store, now)
        self.orders = OrderManager(self.store, self.adapter, self.quote_currency,
                                   float(self.policy.execution.order_ack_timeout_s), self.wall)  # fmt: skip
        self.strategy = MomentumStrategy(self.policy.strategy)
        self.supervisor = ExitSupervisor(self.policy, self.store, self.orders, self.risk, self.board, self.rules,
                                         self.mono_ns, self.wall, deteriorated=self.deterioration)  # fmt: skip
        self._asset_locks: dict[str, asyncio.Lock] = {}
        self.board.listeners.append(self.queue.put)
        # Bounded: a long paper run must not grow memory with every rejected signal.
        self.decisions: deque[dict] = deque(maxlen=5000)
        self.decision_counts: Counter[str] = Counter()
        self._seen_controls: dict[str, tuple[str, str, str]] | None = None

    # ----- startup -----------------------------------------------------------------------------
    async def startup(self, acquire_leader: bool = True) -> list[str]:
        if acquire_leader:
            self.store.acquire_leader()
        await self.orders.reconcile_all()
        return await self.reconcile_balances()

    def deterioration(self, asset_key: str) -> str | None:
        """Why a held asset should be exited early, if anything has deteriorated.

        CEX: the market was removed from the reviewed allowlist. DEX: the latest stored screening report
        is BLOCK. (Venue product status is validated at startup only; it is not re-polled.)
        """
        if asset_key.startswith("cex:"):
            if not any(m.key == asset_key for m in self.policy.cex_allowlist):
                return "market no longer on the reviewed allowlist"
            return None
        rows = self.store.query(
            "SELECT verdict FROM security_reports WHERE asset_key = ? ORDER BY created_at DESC LIMIT 1", (asset_key,)
        )
        if rows and rows[0][0] == "BLOCK":
            return "latest screening verdict is BLOCK"
        return None

    async def reconcile_balances(self) -> list[str]:
        problems = self.store.verify_ledger(self.quote_currency)  # journal-replay check, meaningful in every mode
        venue = await self.adapter.balances()
        cash = self.store.balance(self.quote_currency)
        if venue.get(self.quote_currency, Decimal(0)) != cash:
            problems.append(f"{self.quote_currency}: ledger {cash} != venue {venue.get(self.quote_currency)}")

        def venue_qty(key: str) -> Decimal:
            # Paper venues report by asset key; live venues report by currency code (e.g. "BTC").
            base = key.split(":")[-1].split("-")[0] if key.startswith("cex:") else key
            return venue.get(key, venue.get(base, Decimal(0)))

        positions = self.store.positions()
        for key, pos in positions.items():
            if venue_qty(key) != pos.qty:
                problems.append(f"{key}: ledger {pos.qty} != venue {venue_qty(key)}")
        known = set(positions) | {k.split(":")[-1].split("-")[0] for k in positions if k.startswith("cex:")}
        for key, qty in venue.items():
            if key != self.quote_currency and qty and key not in known:
                problems.append(f"{key}: venue holds {qty} the ledger doesn't know about")
        if problems:
            # A latch (not a pause): it persists and only a named reviewer can clear it.
            self.risk.latch("reconciliation", "; ".join(problems), self.wall())
        return problems

    # ----- accounting ---------------------------------------------------------------------------
    def position_values(self) -> dict[str, Decimal]:
        out = {}
        for key, pos in self.store.positions().items():
            q = self.board(key)
            mark = q.best_bid if q and q.best_bid else pos.avg_entry
            out[key] = pos.qty * mark
        return out

    def equity(self) -> Decimal:
        return self.store.balance(self.quote_currency) + sum(self.position_values().values(), Decimal(0))

    def mark(self) -> list[str]:
        self.poll_controls()
        return self.risk.mark(self.equity(), self.wall())

    def _control_rows(self) -> dict[str, tuple[str, str, str]]:
        return {r["name"]: (r["value"], r["updated_at"], r["actor"]) for r in self.store.query("SELECT * FROM controls")}

    def poll_controls(self) -> None:
        """Apply dashboard/CLI controls only when they CHANGE (edge-triggered).

        A leftover 'unblock' row from an earlier session must never silently undo a later block,
        and automatic protections (latches) can't be cleared from here at all.
        """
        now = self.wall()
        rows = self._control_rows()
        if self._seen_controls is None:  # first poll: remember current rows; don't replay old actions
            self._seen_controls = rows
            return
        for name, row in rows.items():
            if self._seen_controls.get(name) == row:
                continue
            self._seen_controls[name] = row
            value, _, actor = row
            if name == "block_entries" and value in ("1", "0"):
                self.risk.set_block_entries(value == "1", actor or "operator", f"set by {actor}", now)
            elif name == "flatten" and value == "requested":
                self.risk.request_flatten(actor or "operator", now)
                self.store.set_control("flatten", "accepted", actor or "operator", now)
                self._seen_controls["flatten"] = self._control_rows()["flatten"]

    # ----- entries --------------------------------------------------------------------------------
    async def on_market(self, key: str) -> None:
        q = self.board(key)
        if q is None:
            return
        start = time.monotonic_ns()
        t_s = self.mono_ns() / 1e9
        if self.shadow is not None:
            self.shadow.on_quote(q, self.wall())  # type: ignore[attr-defined]
        signal = self.strategy.on_quote(q, t_s)
        if signal is None:
            return
        lock = self._asset_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return  # an entry for this asset is already in flight
        async with lock:
            await self._enter(signal, q, start)

    async def _enter(self, signal: Signal, q: Quote, start_ns: int) -> None:
        now = self.wall()
        rules = self.rules.get(signal.asset_key)
        price_step = Decimal(rules.price_increment) if rules else Decimal("0.01")
        limit = round_price(signal.entry_ref * (1 + self.policy.execution.max_entry_slippage_pct), price_step, "down")
        screening = (True, "CEX market: allowlist + venue validation")
        if signal.asset_key.startswith("dex:"):
            screening = self.screening_gate(signal.asset_key, now) if self.screening_gate else (False, "no screener")
        pending = {r["asset_key"]: D(r["qty"]) * D(r["limit_price"]) for r in self.orders.open_orders("entry")}
        view = ExposureView(
            equity=self.equity(),
            cash=self.store.balance(self.quote_currency),
            position_values=self.position_values(),
            pending_entry_values=pending,
            blocking_orders=self.orders.blocks_new_exposure(),
            quote_ok=quote_usable(q, self.mono_ns(), now, self.policy.market),
        )
        decision = self.risk.check_entry(EntryRequest(signal.asset_key, limit, signal.stop, rules, *screening), view)
        decided = time.monotonic_ns()
        # quote_to_decision uses the runtime's monotonic clock (simulated during replay) for queueing delay
        # plus real compute time for the decision itself.
        self.latency.record("decision_compute", decided - start_ns)
        self.latency.record("quote_to_decision", max(0, self.mono_ns() - q.recv_mono_ns) + (decided - start_ns))
        record = {"at": now.isoformat(), "asset": signal.asset_key, "approved": decision.approved,
                  "qty": str(decision.qty), "reasons": decision.reasons, "signal": signal.reason}  # fmt: skip
        self.decisions.append(record)
        self.decision_counts["approved" if decision.approved else "rejected"] += 1
        for reason in [] if decision.approved else decision.reasons:
            self.decision_counts["reject: " + reason.split(":")[0][:60]] += 1
        if not decision.approved:
            return
        intent = Intent(signal.asset_key, "buy", "entry", decision.qty, limit, self.policy.execution.entry_time_in_force,
                        signal.reason, self.policy.version, self.model_version, signal.stop, signal.target,
                        {"risk": decision.details, "strength": signal.strength})  # fmt: skip
        submit_ns = time.monotonic_ns()
        self.latency.record("decision_to_submit", submit_ns - decided)
        cid = await self.orders.place(intent)
        acked = time.monotonic_ns()
        self.latency.record("submit_to_ack", acked - submit_ns)
        row = self.store.order(cid)
        if D(row["filled_qty"]) > 0:  # stop/target were already applied atomically with the fill
            self.latency.record("submit_to_fill", acked - submit_ns)

    # ----- loops ------------------------------------------------------------------------------------
    async def strategy_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            key = await self.queue.get()
            try:
                await self.on_market(key)
            except Exception:
                log.exception("strategy step failed for %s", key)

    async def supervisor_loop(self, stop: asyncio.Event, interval_s: float = 0.25) -> None:
        while not stop.is_set():
            try:
                await self.supervisor.tick()
            except Exception:
                log.exception("supervisor tick failed")
            await asyncio.sleep(interval_s)

    async def mark_loop(self, stop: asyncio.Event, interval_s: float = 1.0) -> None:
        last_store = 0.0
        while not stop.is_set():
            try:
                for event in self.mark():
                    log.warning(event)
                if time.monotonic() - last_store > 10:
                    last_store = time.monotonic()
                    exposure = sum(self.position_values().values(), Decimal(0))
                    self.store.mark_equity(self.wall(), self.equity(), self.store.balance(self.quote_currency),
                                           exposure, self.store.total_fees())  # fmt: skip
            except Exception:
                log.exception("mark failed")
            await asyncio.sleep(interval_s)
