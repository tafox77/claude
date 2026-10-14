"""SQLite persistence: trades, equity history, open positions and bot state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .risk import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
    pnl REAL, reason TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, qty REAL, entry_price REAL, entry_ts INTEGER,
    cost REAL, high_water REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def record_trade(self, fill, pnl: float | None, reason: str, mode: str) -> None:
        self.db.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, fee, pnl, reason, mode)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fill.ts, fill.symbol, fill.side, fill.qty, fill.price, fill.fee, pnl, reason, mode),
        )
        self.db.commit()

    def record_equity(self, ts: int, equity: float, cash: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?,?,?)", (ts, equity, cash))
        self.db.commit()

    def save_position(self, p: Position) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO positions VALUES (:symbol,:qty,:entry_price,:entry_ts,"
            ":cost,:high_water)",
            asdict(p),
        )
        self.db.commit()

    def delete_position(self, symbol: str) -> None:
        self.db.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.db.commit()

    def load_positions(self) -> dict[str, Position]:
        rows = self.db.execute("SELECT * FROM positions").fetchall()
        return {r["symbol"]: Position(**dict(r)) for r in rows}

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def trades(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM trades ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.execute(sql).fetchall()

    def equity_curve(self) -> list[tuple[int, float]]:
        return [(r["ts"], r["equity"]) for r in self.db.execute("SELECT * FROM equity ORDER BY ts")]
