"""SQLite (WAL) storage with versioned migrations, a single writer, and a single trading leader.

All money/quantity values are Decimal strings. Fills are applied atomically together with the
order, position and cash updates, and duplicate fill ids are ignored, so replaying the same
exchange events is safe.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"  # shipped as package data
ZERO = Decimal(0)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat()


def D(value: Any) -> Decimal:
    return Decimal(str(value))


class LeaderLockError(RuntimeError):
    pass


class LedgerError(RuntimeError):
    """An update would create negative cash, oversell a position, or otherwise corrupt the ledger."""


@dataclass
class Position:
    asset_key: str
    qty: Decimal
    cost_basis: Decimal
    stop: Decimal | None
    target: Decimal | None
    opened_at: str
    entry_intent_id: str | None
    realized_pnl: Decimal

    @property
    def avg_entry(self) -> Decimal:
        return self.cost_basis / self.qty if self.qty else ZERO


class Store:
    def __init__(self, path: str | Path, migrations_dir: Path = MIGRATIONS_DIR):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._leader_fd: int | None = None
        self.migrate(migrations_dir)

    # ----- infrastructure -----------------------------------------------------------------
    def migrate(self, migrations_dir: Path) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
            applied = {r[0] for r in c.execute("SELECT version FROM schema_version")}
        for file in sorted(migrations_dir.glob("*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version in applied:
                continue
            with self.tx() as c:
                for statement in file.read_text().split(";\n"):
                    if statement.strip():
                        c.execute(statement)
                c.execute("INSERT INTO schema_version VALUES (?, ?)", (version, iso(utcnow())))

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (BEGIN IMMEDIATE)."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def acquire_leader(self) -> None:
        """Exclusive OS lock so only one process trades against this database."""
        if self.path == ":memory:":
            return
        fd = os.open(self.path + ".leader", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LeaderLockError(f"Another spotguard process is already trading with {self.path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._leader_fd = fd

    def close(self) -> None:
        if self._leader_fd is not None:
            fcntl.flock(self._leader_fd, fcntl.LOCK_UN)
            os.close(self._leader_fd)
            self._leader_fd = None
        self.conn.close()

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, args))

    # ----- events, alerts, controls ---------------------------------------------------------
    def record_event(self, event_id: str, kind: str, payload: dict, at: datetime) -> bool:
        """Insert an event once. Returns False for a duplicate (already processed) event id."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)", (event_id, kind, json.dumps(payload), iso(at)))
            return cur.rowcount == 1

    def alert(self, level: str, message: str, at: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO alerts (at, level, message) VALUES (?, ?, ?)", (iso(at), level, message))

    def set_control(self, name: str, value: str, actor: str, at: datetime) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO controls VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at, actor=excluded.actor",
                (name, value, iso(at), actor),
            )

    def get_control(self, name: str, default: str = "") -> str:
        rows = self.query("SELECT value FROM controls WHERE name = ?", (name,))
        return rows[0][0] if rows else default

    # ----- balances & positions -----------------------------------------------------------------
    def balance(self, currency: str) -> Decimal:
        rows = self.query("SELECT amount FROM balances WHERE currency = ?", (currency,))
        return D(rows[0][0]) if rows else ZERO

    def set_initial_balance(self, currency: str, amount: Decimal, at: datetime) -> None:
        with self.tx() as c:
            if c.execute("SELECT 1 FROM balances WHERE currency = ?", (currency,)).fetchone():
                return
            c.execute("INSERT INTO balances VALUES (?, ?)", (currency, str(amount)))
            c.execute(
                "INSERT INTO cash_flows (at, currency, amount, note) VALUES (?, ?, ?, ?)",
                (iso(at), currency, str(amount), "initial virtual equity"),
            )

    def positions(self) -> dict[str, Position]:
        out = {}
        for r in self.query("SELECT * FROM positions WHERE CAST(qty AS REAL) > 0"):
            out[r["asset_key"]] = Position(
                r["asset_key"],
                D(r["qty"]),
                D(r["cost_basis"]),
                D(r["stop"]) if r["stop"] is not None else None,
                D(r["target"]) if r["target"] is not None else None,
                r["opened_at"],
                r["entry_intent_id"],
                D(r["realized_pnl"]),
            )
        return out

    def verify_ledger(self, quote_currency: str) -> list[str]:
        """Rebuild cash and positions from the cash-flow and fill journals and compare with the ledger.

        Catches partial writes or manual edits. Fills are applied atomically, so in normal operation this
        always agrees; a mismatch means the database was modified outside the application.
        """
        problems = []
        cash = sum((D(r[0]) for r in self.query("SELECT amount FROM cash_flows WHERE currency = ?", (quote_currency,))), ZERO)
        held: dict[str, Decimal] = {}
        for r in self.query("SELECT asset_key, side, qty, price, fee FROM fills ORDER BY at, rowid"):
            notional, fee = D(r["qty"]) * D(r["price"]), D(r["fee"])
            if r["side"] == "buy":
                cash -= notional + fee
                held[r["asset_key"]] = held.get(r["asset_key"], ZERO) + D(r["qty"])
            else:
                cash += notional - fee
                held[r["asset_key"]] = held.get(r["asset_key"], ZERO) - D(r["qty"])
        if self.query("SELECT 1 FROM balances WHERE currency = ?", (quote_currency,)) and cash != self.balance(quote_currency):
            problems.append(f"ledger cash {self.balance(quote_currency)} != journal {cash}")
        positions = self.positions()
        for key in set(held) | set(positions):
            ledger = positions[key].qty if key in positions else ZERO
            if held.get(key, ZERO) != ledger:
                problems.append(f"{key}: ledger {ledger} != journal {held.get(key, ZERO)}")
        return problems

    def set_protection(self, asset_key: str, stop: Decimal | None, target: Decimal | None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE positions SET stop = ?, target = ? WHERE asset_key = ?",
                (str(stop) if stop is not None else None, str(target) if target is not None else None, asset_key),
            )

    # ----- intents & orders ---------------------------------------------------------------------
    def insert_intent(self, row: dict) -> None:
        with self.tx() as c:
            row = {"stop": None, "target": None, **row}
            c.execute(
                "INSERT INTO intents (intent_id, created_at, asset_key, side, purpose, qty, limit_price, reason, "
                "policy_version, model_version, decision, stop, target) VALUES (:intent_id, :created_at, :asset_key, "
                ":side, :purpose, :qty, :limit_price, :reason, :policy_version, :model_version, :decision, :stop, :target)",
                row,
            )

    def insert_order(self, row: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO orders (client_order_id, intent_id, asset_key, side, purpose, qty, limit_price, "
                "time_in_force, state, created_at, updated_at) VALUES (:client_order_id, :intent_id, :asset_key, "
                ":side, :purpose, :qty, :limit_price, :time_in_force, :state, :created_at, :updated_at)",
                row,
            )
            c.execute(
                "INSERT INTO order_events (client_order_id, from_state, to_state, at, detail) VALUES (?, NULL, ?, ?, ?)",
                (row["client_order_id"], row["state"], row["created_at"], "created"),
            )

    def order(self, client_order_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return rows[0] if rows else None

    def orders_in(self, states: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in states)
        return self.query(f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY created_at", states)

    def transition(
        self,
        client_order_id: str,
        from_states: tuple[str, ...],
        to_state: str,
        at: datetime,
        detail: str = "",
        **fields,
    ) -> bool:
        """Compare-and-set state change. Returns False if the order isn't in one of from_states."""
        with self.tx() as c:
            row = c.execute("SELECT state FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
            if row is None or row[0] not in from_states:
                return False
            sets = ["state = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
            c.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE client_order_id = ?",
                (to_state, iso(at), *fields.values(), client_order_id),
            )
            c.execute(
                "INSERT INTO order_events (client_order_id, from_state, to_state, at, detail) VALUES (?, ?, ?, ?, ?)",
                (client_order_id, row[0], to_state, iso(at), detail),
            )
            return True

    def apply_fill(
        self,
        fill_id: str,
        client_order_id: str,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        liquidity: str,
        at: datetime,
        quote_currency: str,
    ) -> bool:
        """Atomically record a fill and update order, position and cash. Duplicate fill ids are no-ops."""
        if qty <= 0 or price <= 0 or fee < 0:
            raise LedgerError(f"invalid fill qty={qty} price={price} fee={fee}")
        with self.tx() as c:
            order = c.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
            if order is None:
                raise LedgerError(f"fill {fill_id} for unknown order {client_order_id}")
            if c.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone():
                return False
            filled = D(order["filled_qty"]) + qty
            if filled > D(order["qty"]):
                raise LedgerError(f"fill {fill_id} would overfill order {client_order_id}")
            asset, side = order["asset_key"], order["side"]
            notional = qty * price
            cash_row = c.execute("SELECT amount FROM balances WHERE currency = ?", (quote_currency,)).fetchone()
            cash = D(cash_row[0]) if cash_row else ZERO
            pos = c.execute("SELECT * FROM positions WHERE asset_key = ?", (asset,)).fetchone()
            held = D(pos["qty"]) if pos else ZERO
            basis = D(pos["cost_basis"]) if pos else ZERO
            realized = D(pos["realized_pnl"]) if pos else ZERO
            if side == "buy":
                new_cash = cash - notional - fee
                if new_cash < 0:
                    raise LedgerError(f"fill {fill_id} would make {quote_currency} cash negative")
                new_qty, new_basis = held + qty, basis + notional + fee
            else:
                if qty > held:
                    raise LedgerError(f"fill {fill_id} would oversell {asset}: {qty} > {held}")
                new_cash = cash + notional - fee
                released = basis * qty / held
                new_qty, new_basis = held - qty, basis - released
                realized += notional - fee - released
            c.execute(
                "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fill_id, client_order_id, asset, side, str(qty), str(price), str(fee), liquidity, iso(at)),
            )
            c.execute(
                "UPDATE orders SET filled_qty = ?, filled_notional = ?, fees = ?, updated_at = ? WHERE client_order_id = ?",
                (
                    str(filled),
                    str(D(order["filled_notional"]) + notional),
                    str(D(order["fees"]) + fee),
                    iso(at),
                    client_order_id,
                ),
            )
            c.execute(
                "INSERT INTO balances VALUES (?, ?) ON CONFLICT(currency) DO UPDATE SET amount = excluded.amount",
                (quote_currency, str(new_cash)),
            )
            # Protection comes from the intent that opened the position, in this same transaction.
            # A position opened from flat takes its intent's stop/target; a closed position clears them,
            # so a later re-entry never inherits a stale stop.
            stop = pos["stop"] if pos is not None and held > 0 else None
            target = pos["target"] if pos is not None and held > 0 else None
            if side == "buy" and held == 0:
                intent = c.execute("SELECT stop, target FROM intents WHERE intent_id = ?", (order["intent_id"],)).fetchone()
                stop, target = (intent["stop"], intent["target"]) if intent else (None, None)
            if new_qty == 0:
                stop = target = None
            if pos is None:
                c.execute(
                    "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (asset, str(new_qty), str(new_basis), stop, target, iso(at), order["intent_id"], str(realized), iso(at)),
                )
            else:
                opened = pos["opened_at"] if held > 0 else iso(at)
                entry = pos["entry_intent_id"] if held > 0 else order["intent_id"]
                if new_qty == 0:
                    new_basis = ZERO
                c.execute(
                    "UPDATE positions SET qty = ?, cost_basis = ?, realized_pnl = ?, opened_at = ?, "
                    "entry_intent_id = ?, stop = ?, target = ?, updated_at = ? WHERE asset_key = ?",
                    (str(new_qty), str(new_basis), str(realized), opened, entry, stop, target, iso(at), asset),
                )
            return True

    # ----- risk state & marks -------------------------------------------------------------------
    def load_risk_state(self) -> dict | None:
        rows = self.query("SELECT state FROM risk_state WHERE id = 1")
        return json.loads(rows[0][0]) if rows else None

    def save_risk_state(self, state: dict, event: tuple[str, str] | None, at: datetime) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO risk_state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (json.dumps(state),),
            )
            if event:
                c.execute("INSERT INTO risk_events (at, kind, detail) VALUES (?, ?, ?)", (iso(at), *event))

    def mark_equity(self, at: datetime, equity: Decimal, cash: Decimal, exposure: Decimal, fees: Decimal) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO equity_marks VALUES (?, ?, ?, ?, ?)",
                (iso(at), str(equity), str(cash), str(exposure), str(fees)),
            )

    def total_fees(self) -> Decimal:
        return sum((D(r[0]) for r in self.query("SELECT fee FROM fills")), ZERO)

    def record_latency(self, rows: list[tuple[str, int, str]]) -> None:
        if not rows:
            return
        with self.tx() as c:
            c.executemany("INSERT INTO latency VALUES (?, ?, ?)", rows)
