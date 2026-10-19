"""Bar construction and features. Every feature at bar t uses only bars <= t."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from spotguard.market_data import Quote

FEATURES = ["ret_1", "ret_5", "ret_15", "vol_15", "range_1", "spread", "imbalance", "ema_gap"]
WARMUP = 30


@dataclass
class Bar:
    market_key: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    spread: float
    imbalance: float
    n: int


class BarBuilder:
    """Aggregates quotes into fixed-interval mid-price bars per market."""

    def __init__(self, interval_s: int = 60):
        self.interval = timedelta(seconds=interval_s)
        self.current: dict[str, Bar] = {}

    def on_quote(self, q: Quote, now: datetime) -> Bar | None:
        mid = q.mid
        if mid is None or not q.bids or not q.asks:
            return None
        epoch = datetime.fromtimestamp(0, now.tzinfo)
        start = epoch + ((now - epoch) // self.interval) * self.interval
        m = float(mid)
        spread = float(q.spread_pct or 0)
        bid_sz, ask_sz = float(q.bids[0][1]), float(q.asks[0][1])
        imb = (bid_sz - ask_sz) / (bid_sz + ask_sz) if bid_sz + ask_sz > 0 else 0.0
        bar = self.current.get(q.market_key)
        closed = None
        if bar is not None and bar.start != start:
            closed = bar
            bar = None
        if bar is None:
            self.current[q.market_key] = Bar(q.market_key, start, m, m, m, m, spread, imb, 1)
        else:
            bar.high, bar.low, bar.close = max(bar.high, m), min(bar.low, m), m
            bar.spread += (spread - bar.spread) / (bar.n + 1)
            bar.imbalance += (imb - bar.imbalance) / (bar.n + 1)
            bar.n += 1
        return closed


def feature_matrix(close: np.ndarray, high: np.ndarray, low: np.ndarray, spread: np.ndarray,
                   imbalance: np.ndarray) -> np.ndarray:  # fmt: skip
    """Features for every bar (rows before WARMUP are NaN). Causal: uses data up to each row only."""
    n = len(close)
    out = np.full((n, len(FEATURES)), np.nan)
    logc = np.log(close)
    ema_fast = np.empty(n)
    ema_slow = np.empty(n)
    a_f, a_s = 2 / (5 + 1), 2 / (30 + 1)
    for i in range(n):
        ema_fast[i] = close[i] if i == 0 else ema_fast[i - 1] + a_f * (close[i] - ema_fast[i - 1])
        ema_slow[i] = close[i] if i == 0 else ema_slow[i - 1] + a_s * (close[i] - ema_slow[i - 1])
    for i in range(WARMUP, n):
        r1 = np.diff(logc[i - 15 : i + 1])
        out[i] = [
            logc[i] - logc[i - 1],
            logc[i] - logc[i - 5],
            logc[i] - logc[i - 15],
            float(np.std(r1)),
            (high[i] - low[i]) / close[i],
            spread[i],
            imbalance[i],
            (ema_fast[i] - ema_slow[i]) / close[i],
        ]
    return out


def bars_to_arrays(bars: list[Bar]) -> dict[str, np.ndarray]:
    return {
        "close": np.array([b.close for b in bars], dtype=float),
        "high": np.array([b.high for b in bars], dtype=float),
        "low": np.array([b.low for b in bars], dtype=float),
        "spread": np.array([b.spread for b in bars], dtype=float),
        "imbalance": np.array([b.imbalance for b in bars], dtype=float),
    }
