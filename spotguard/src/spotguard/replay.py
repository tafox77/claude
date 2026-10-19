"""Deterministic offline replay through the real runtime (paper venue, simulated clock).

The synthetic data generator writes Coinbase Advanced Trade level2-format messages (seeded random
walk with regime changes) and deliberately injects feed anomalies: a sequence gap, a duplicate
message, a crossed update and a stall. These are FABRICATED market data for testing the pipeline,
not historical prices, and say nothing about real-world profitability.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from spotguard.config import Policy
from spotguard.execution.paper import PaperVenue
from spotguard.identities import CexMarketId, ProductRules, validate_coinbase_product
from spotguard.market_data import CoinbaseL2Handler
from spotguard.runtime import QuoteBoard, Runtime
from spotguard.storage import Store

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def fixture_product(m: CexMarketId) -> dict:
    """Product listing in Coinbase's documented shape, with the increments Coinbase publishes for BTC/ETH-USD."""
    return {
        "product_id": m.market_id, "base_currency_id": m.base_id, "quote_currency_id": m.quote_id,
        "product_type": "SPOT", "status": "online", "trading_disabled": False, "is_disabled": False,
        "cancel_only": False, "limit_only": False, "view_only": False, "auction_mode": False, "alias": "",
        "base_increment": "0.00000001", "quote_increment": "0.01", "price_increment": "0.01",
        "base_min_size": "0.00000001", "quote_min_size": "1",
    }  # fmt: skip


def _ts(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def generate(path: str | Path, markets: list[CexMarketId], minutes: int = 180, seed: int = 7,
             anomalies: bool = True) -> int:  # fmt: skip
    rng = random.Random(seed)
    mids = {m.market_id: (100_000.0 if m.base_id == "BTC" else 4_000.0) for m in markets}
    drift = {k: 0.0 for k in mids}
    last_levels: dict[str, tuple[set, set]] = {}
    seq = 0
    lines = 0
    with open(path, "w") as fh:

        def emit(offset_ms: int, msg: dict) -> None:
            nonlocal lines
            fh.write(json.dumps({"recv_offset_ms": offset_ms, "msg": msg}) + "\n")
            lines += 1

        def levels(pid: str) -> tuple[list, list]:
            mid = mids[pid]
            tick = 0.01
            half = max(tick, round(mid * 0.00002, 2))
            bids = [(round(mid - half - i * mid * 0.0001, 2), round(rng.uniform(0.05, 2.0), 8)) for i in range(10)]
            asks = [(round(mid + half + i * mid * 0.0001, 2), round(rng.uniform(0.05, 2.0), 8)) for i in range(10)]
            return bids, asks

        def book_msg(pid: str, at: datetime, kind: str, crossed: bool = False) -> dict:
            nonlocal seq
            bids, asks = levels(pid)
            if crossed:
                bids[0] = (asks[0][0] + 1, bids[0][1])
            updates = [
                {"side": "bid", "event_time": _ts(at), "price_level": f"{p:.2f}", "new_quantity": f"{q:.8f}"} for p, q in bids
            ]
            updates += [{"side": "offer", "event_time": _ts(at), "price_level": f"{p:.2f}", "new_quantity": f"{q:.8f}"}
                        for p, q in asks]  # fmt: skip
            if kind == "update" and pid in last_levels:
                old_b, old_a = last_levels[pid]
                updates += [
                    {"side": "bid", "event_time": _ts(at), "price_level": f"{p:.2f}", "new_quantity": "0"}
                    for p in old_b - {p for p, _ in bids}
                ]
                updates += [{"side": "offer", "event_time": _ts(at), "price_level": f"{p:.2f}", "new_quantity": "0"}
                            for p in old_a - {p for p, _ in asks}]  # fmt: skip
            last_levels[pid] = ({p for p, _ in bids}, {p for p, _ in asks})
            msg = {"channel": "l2_data", "client_id": "", "timestamp": _ts(at), "sequence_num": seq,
                   "events": [{"type": kind, "product_id": pid, "updates": updates}]}  # fmt: skip
            seq += 1
            return msg

        for pid in mids:
            emit(0, book_msg(pid, START, "snapshot"))
        total_s = minutes * 60
        gap_at, dup_at, cross_at, stall_at = (total_s * f for f in (0.30, 0.45, 0.60, 0.75)) if anomalies else (-1,) * 4
        t = 0.0
        while t < total_s:
            t += 0.5
            if stall_at > 0 and stall_at <= t < stall_at + 6:  # 6 s with no messages: books go stale
                continue
            at = START + timedelta(seconds=t)
            if int(t) % 600 == 0 and t == int(t):
                for k in drift:
                    drift[k] = rng.choice([-1, 0, 0, 1]) * 0.00004
            for pid in mids:
                mids[pid] *= 1 + drift[pid] + rng.gauss(0, 0.00012)
                offset = int(t * 1000) + rng.randint(5, 40)  # receipt jitter after exchange time
                if gap_at > 0 and abs(t - gap_at) < 0.25:
                    seq += 3  # lost messages
                    emit(offset, book_msg(pid, at, "update"))
                    emit(offset + 5, book_msg(pid, at, "snapshot"))  # what a reconnect delivers
                    for other in mids:
                        if other != pid:
                            emit(offset + 6, book_msg(other, at, "snapshot"))
                    continue
                if cross_at > 0 and abs(t - cross_at) < 0.25 and pid == next(iter(mids)):
                    emit(offset, book_msg(pid, at, "update", crossed=True))
                    seq = 0  # reconnect resets the connection sequence
                    for other in mids:
                        emit(offset + 5, book_msg(other, at, "snapshot"))
                    continue
                msg = book_msg(pid, at, "update")
                emit(offset, msg)
                if dup_at > 0 and abs(t - dup_at) < 0.25:
                    emit(offset + 1, msg)  # duplicated delivery
            if t == int(t):
                emit(int(t * 1000) + 1, {"channel": "heartbeats", "timestamp": _ts(at), "sequence_num": seq,
                                         "events": [{"current_time": _ts(at), "heartbeat_counter": int(t)}]})  # fmt: skip
                seq += 1
    return lines


@dataclass
class SimClock:
    offset_ms: int = 0
    base_mono: int = 10**12

    def mono_ns(self) -> int:
        return self.base_mono + self.offset_ms * 1_000_000

    def now(self) -> datetime:
        return START + timedelta(milliseconds=self.offset_ms)


@dataclass
class ReplayResult:
    messages: int
    feed_events: dict[str, int]
    decisions: int
    approved: int
    rejections: dict[str, int]
    fills: int
    closed_trades: int
    realized_pnl: Decimal
    fees: Decimal
    final_equity: Decimal
    open_positions: dict[str, str]
    latency: dict
    alerts: int
    digest: str
    latches: list[str] = field(default_factory=list)
    shadow: dict | None = None

    def report(self) -> str:
        lines = [
            "OFFLINE PAPER REPLAY (synthetic data; not evidence of real-world performance)",
            f"  messages            {self.messages}   feed events {self.feed_events}",
            f"  signals evaluated   {self.decisions}   approved {self.approved}",
            f"  rejection reasons   {self.rejections}",
            f"  fills               {self.fills}   closed round trips {self.closed_trades}",
            f"  realized P&L        {self.realized_pnl:.2f} USD   fees {self.fees:.2f} USD",
            f"  final equity        {self.final_equity:.2f} USD   open positions {self.open_positions}",
            f"  risk latches        {self.latches or 'none'}   alerts {self.alerts}",
            "  local latency (ms)  " + ", ".join(
                f"{k}: p50 {v['p50_ms']:.3f} p95 {v['p95_ms']:.3f} p99 {v['p99_ms']:.3f} (n={v['n']})"
                for k, v in sorted(self.latency.items())),
            f"  shadow model        {self.shadow or 'not running'}",
            f"  deterministic digest {self.digest}",
        ]  # fmt: skip
        return "\n".join(lines)


async def run_replay(policy: Policy, data_path: str | Path, db_path: str | Path, shadow_factory=None) -> ReplayResult:
    markets = list(policy.cex_allowlist)
    clock = SimClock()
    rules: dict[str, ProductRules] = {m.key: validate_coinbase_product(m, fixture_product(m), clock.now())
                                      for m in markets}  # fmt: skip
    board = QuoteBoard()
    store = Store(db_path)
    venue = PaperVenue(board, rules, policy.execution, policy.market, clock.mono_ns, clock.now,
                       {"USD": policy.risk.virtual_equity})  # fmt: skip
    rt = Runtime(policy, store, venue, board, rules, clock.mono_ns, clock.now)
    if shadow_factory is not None:
        rt.shadow = shadow_factory(rt)
    venue.restore({"USD": store.balance("USD")}, {k: p.qty for k, p in store.positions().items()})
    await rt.startup(acquire_leader=True)
    handler = CoinbaseL2Handler(markets)
    feed_events: dict[str, int] = {}
    next_tick_ms = 0
    messages = 0
    with open(data_path) as fh:
        for line in fh:
            row = json.loads(line)
            clock.offset_ms = row["recv_offset_ms"]
            while next_tick_ms <= clock.offset_ms:  # supervisor + marks on simulated time
                await rt.supervisor.tick()
                rt.mark()
                next_tick_ms += 1000
            messages += 1
            for ev in handler.handle(row["msg"], clock.mono_ns(), clock.now()):
                feed_events[ev.kind] = feed_events.get(ev.kind, 0) + 1
                if ev.kind == "book" and ev.market_key:
                    board.update(handler.books[ev.market_key].quote())
                    venue.on_quote(ev.market_key)
            if handler.needs_resync:
                handler.on_reconnect()  # the recording contains the snapshots a reconnect would deliver
            while rt.queue.pending:
                await rt.on_market(await rt.queue.get())
    await rt.supervisor.tick()
    rt.mark()
    fills = store.query("SELECT asset_key, side, qty, price, fee, at FROM fills ORDER BY at, rowid")
    # Client order ids are random UUIDs, so the digest covers the economic content of every fill.
    digest = hashlib.sha256(json.dumps([tuple(r) for r in fills]).encode()).hexdigest()[:16]
    rejections = {k[len("reject: ") :]: v for k, v in rt.decision_counts.items() if k.startswith("reject: ")}
    realized = sum((D(r[0]) for r in store.query("SELECT realized_pnl FROM positions")), Decimal(0))
    result = ReplayResult(
        messages=messages,
        feed_events=feed_events,
        decisions=rt.decision_counts["approved"] + rt.decision_counts["rejected"],
        approved=rt.decision_counts["approved"],
        rejections=rejections,
        fills=len(fills),
        closed_trades=store.query("SELECT count(*) FROM orders WHERE purpose != 'entry' AND state = 'FILLED'")[0][0],
        realized_pnl=realized,
        fees=store.total_fees(),
        final_equity=rt.equity(),
        open_positions={k: str(p.qty) for k, p in store.positions().items()},
        latency=rt.latency.summary(),
        alerts=store.query("SELECT count(*) FROM alerts")[0][0],
        digest=digest,
        latches=sorted(rt.risk.state.latched),
    )
    if rt.shadow is not None:
        result.shadow = {
            "predictions": store.query("SELECT count(*) FROM predictions")[0][0],
            "labels": store.query("SELECT count(*) FROM labels")[0][0],
            "anomalies": len(rt.shadow.anomalies),
            "entries_blocked": rt.risk.state.block_entries,
        }
    store.close()
    return result


def D(v) -> Decimal:
    return Decimal(str(v))
