"""Illustrative momentum strategy (fixed baseline, not an edge claim).

Keeps time-weighted fast/slow averages of the mid price and proposes a long entry when the fast
average exceeds the slow one by min_edge_pct. Stops and targets are fixed percentages. Features
are updated incrementally so a decision costs O(1) per quote.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from spotguard.config import StrategyParams
from spotguard.market_data import Quote


@dataclass(frozen=True)
class Signal:
    asset_key: str
    entry_ref: Decimal  # best ask at decision time
    stop: Decimal
    target: Decimal
    strength: float
    reason: str


class _Ema:
    """Exponential average with a time constant in seconds (irregular sampling safe)."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self.value: float | None = None
        self.last_t: float | None = None

    def update(self, x: float, t: float) -> float:
        if self.value is None or self.last_t is None:
            self.value, self.last_t = x, t
            return x
        dt = max(0.0, t - self.last_t)
        alpha = 1 - math.exp(-dt / self.window_s) if self.window_s > 0 else 1.0
        self.value += alpha * (x - self.value)
        self.last_t = t
        return self.value


class MomentumStrategy:
    def __init__(self, params: StrategyParams):
        self.p = params
        self.fast: dict[str, _Ema] = {}
        self.slow: dict[str, _Ema] = {}
        self.first_t: dict[str, float] = {}

    def on_quote(self, q: Quote, t_s: float) -> Signal | None:
        mid = q.mid
        if mid is None or q.best_ask is None:
            return None
        key = q.market_key
        fast = self.fast.setdefault(key, _Ema(self.p.fast_window_s)).update(float(mid), t_s)
        slow = self.slow.setdefault(key, _Ema(self.p.slow_window_s)).update(float(mid), t_s)
        start = self.first_t.setdefault(key, t_s)
        if t_s - start < self.p.slow_window_s:  # warm-up: averages not meaningful yet
            return None
        edge = (fast - slow) / slow if slow else 0.0
        if edge < float(self.p.min_edge_pct):
            return None
        ask = q.best_ask
        return Signal(
            key,
            ask,
            stop=ask * (1 - self.p.stop_pct),
            target=ask * (1 + self.p.target_pct),
            strength=edge,
            reason=f"fast/slow momentum {edge:.4%} >= {self.p.min_edge_pct:.4%}",
        )
