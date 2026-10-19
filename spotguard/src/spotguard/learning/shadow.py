"""Shadow evaluation and drift monitoring.

The shadow runner builds bars from live/replayed quotes, scores them with a registered model and
writes immutable prediction rows; labels are added only after the horizon has passed. It holds no
reference to the order manager or risk limits, so it cannot trade. The drift monitor compares
recent feature distributions to the training reference (PSI) and, on a material anomaly, switches
the system to no-new-entries via the risk engine's kill switch (action A) and records the alarm.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import datetime

import numpy as np

from spotguard.learning.features import FEATURES, WARMUP, Bar, BarBuilder, feature_matrix
from spotguard.learning.labels import label_available_at
from spotguard.market_data import Quote
from spotguard.storage import Store, iso


def psi(reference_q: list[float], sample: np.ndarray) -> float:
    """Population stability index of `sample` against decile edges from the training reference."""
    edges = np.array(reference_q)
    expected = np.full(len(edges) + 1, 1 / (len(edges) + 1))
    counts = np.bincount(np.searchsorted(edges, sample), minlength=len(edges) + 1)
    actual = np.clip(counts / max(1, len(sample)), 1e-4, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


class DriftMonitor:
    def __init__(self, reference: dict, window: int = 200, threshold: float = 0.25):
        self.reference = reference
        self.window = window
        self.threshold = threshold
        self.rows: deque[np.ndarray] = deque(maxlen=window)
        self.alarmed = False  # latches: one alarm until a reviewer resets the monitor

    def reset(self) -> None:
        self.alarmed = False
        self.rows.clear()

    def add(self, x: np.ndarray) -> dict | None:
        self.rows.append(x)
        if self.alarmed or len(self.rows) < self.window:
            return None
        data = np.array(self.rows)
        scores = {name: psi(self.reference[name]["quantiles"], data[:, i]) for i, name in enumerate(FEATURES)}
        worst = max(scores, key=lambda k: scores[k])
        if scores[worst] > self.threshold:
            self.alarmed = True
            return {"feature": worst, "psi": scores[worst], "threshold": self.threshold}
        return None


class ShadowRunner:
    def __init__(self, store: Store, model_id: str, model, horizon: int, interval_s: int, drift: DriftMonitor | None,
                 on_anomaly=None):  # fmt: skip
        self.store = store
        self.model_id = model_id
        self.model = model
        self.horizon = horizon
        self.interval_s = interval_s
        self.builder = BarBuilder(interval_s)
        self.history: dict[str, list[Bar]] = {}
        self.trimmed: dict[str, int] = {}  # bars dropped from the front of each history (absolute index offset)
        self.pending: dict[str, deque] = {}
        self.drift = drift
        self.on_anomaly = on_anomaly
        self.anomalies: list[dict] = []

    def on_quote(self, q: Quote, now: datetime) -> None:
        bar = self.builder.on_quote(q, now)
        if bar is not None:
            self._on_bar(bar)

    def _on_bar(self, bar: Bar) -> None:
        hist = self.history.setdefault(bar.market_key, [])
        hist.append(bar)
        self._label(bar.market_key)
        if len(hist) > 2000:
            del hist[:500]
            self.trimmed[bar.market_key] = self.trimmed.get(bar.market_key, 0) + 500
        if len(hist) <= WARMUP:
            return
        tail = hist[-(WARMUP + 1) :]
        X = feature_matrix(*(np.array([getattr(b, f) for b in tail]) for f in ("close", "high", "low", "spread",
                                                                             "imbalance")))  # fmt: skip
        x = X[-1]
        if not np.isfinite(x).all():
            self._anomaly({"reason": "non-finite features", "at": iso(bar.start)})
            return
        prob = float(self.model.predict_proba(x.reshape(1, -1))[0, 1])
        pred_id = uuid.uuid4().hex
        with self.store.tx() as c:
            c.execute("INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (pred_id, self.model_id, bar.market_key, iso(bar.start), self.horizon,
                       json.dumps(dict(zip(FEATURES, map(float, x), strict=True))), str(prob)))  # fmt: skip
        absolute = self.trimmed.get(bar.market_key, 0) + len(hist) - 1
        self.pending.setdefault(bar.market_key, deque()).append((pred_id, absolute, bar))
        if self.drift is not None:
            alarm = self.drift.add(x)
            if alarm:
                self._anomaly(alarm)

    def _label(self, key: str) -> None:
        hist = self.history[key]
        offset = self.trimmed.get(key, 0)
        queue = self.pending.get(key, deque())
        while queue:
            pred_id, absolute, bar = queue[0]
            idx = absolute - offset
            # Only label once bar idx + horizon has closed (it is in history and a newer bar exists).
            if len(hist) - 1 < idx + self.horizon + 1:
                break
            queue.popleft()
            if idx < 0:
                continue  # its entry bar was trimmed before it could be labelled
            entry, exit_ = hist[idx].close, hist[idx + self.horizon].close
            ret = exit_ / entry - 1
            available = label_available_at(bar.start, self.horizon, self.interval_s)
            with self.store.tx() as c:
                c.execute("INSERT OR IGNORE INTO labels VALUES (?, ?, ?, ?)",
                          (pred_id, int(ret > 0), str(ret), iso(available)))  # fmt: skip

    def _anomaly(self, detail: dict) -> None:
        self.anomalies.append(detail)
        if self.on_anomaly is not None:
            self.on_anomaly(detail)
