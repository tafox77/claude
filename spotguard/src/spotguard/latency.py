"""Latency measurement. Stages are recorded separately and never merged:

* quote_to_decision: quote receipt (monotonic) → strategy + risk decision complete (local compute)
* decision_to_submit: decision → adapter submit call starts
* submit_to_ack: adapter round trip (network + venue for live; simulated for paper)
* submit_to_fill: submit → first fill observed

Targets are benchmarks (local p95 < 100 ms excluding network/scanner time), not promised fill speed.
"""

from __future__ import annotations

from collections import defaultdict, deque


class LatencyStats:
    def __init__(self, keep: int = 100_000):
        self.samples: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=keep))

    def record(self, stage: str, ns: int) -> None:
        if ns >= 0:
            self.samples[stage].append(ns)

    @staticmethod
    def _pct(sorted_ns: list[int], p: float) -> float:
        if not sorted_ns:
            return float("nan")
        k = min(len(sorted_ns) - 1, max(0, round(p / 100 * (len(sorted_ns) - 1))))
        return sorted_ns[k] / 1e6

    def summary(self) -> dict[str, dict[str, float]]:
        out = {}
        for stage, values in self.samples.items():
            s = sorted(values)
            out[stage] = {
                "n": len(s),
                "p50_ms": self._pct(s, 50),
                "p95_ms": self._pct(s, 95),
                "p99_ms": self._pct(s, 99),
            }
        return out
