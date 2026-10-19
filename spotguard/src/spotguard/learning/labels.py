"""Labels: net forward return after costs over a fixed horizon.

A label for bar t exists only once bar t+h has closed; the last h bars are NaN, and
`label_available_at` gives the earliest moment a label may be used for training.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np


def net_forward_returns(close: np.ndarray, spread: np.ndarray, horizon: int, fee_pct: float) -> np.ndarray:
    """Return of buying at the ask at t and selling at the bid at t+h, minus fees on both legs."""
    n = len(close)
    out = np.full(n, np.nan)
    for t in range(n - horizon):
        buy = close[t] * (1 + spread[t] / 2)
        sell = close[t + horizon] * (1 - spread[t + horizon] / 2)
        out[t] = sell / buy - 1 - 2 * fee_pct
    return out


def labels_from_returns(r: np.ndarray) -> np.ndarray:
    return np.where(np.isfinite(r), (r > 0).astype(float), np.nan)


def label_available_at(bar_start: datetime, horizon: int, interval_s: int) -> datetime:
    return bar_start + timedelta(seconds=interval_s * (horizon + 1))
