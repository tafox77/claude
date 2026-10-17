"""Online classifier that keeps learning from new bars and only trades once it proves an edge.

It predicts P(a trade entered now ends profitable after costs). Every batch is scored
*before* it is learned (prequential evaluation), so its track record is out-of-sample.
The model is trusted to trade only when, over the recent evaluation window, both hold:
  * its log-loss beats a baseline that predicts the recent win rate, and
  * the trades it would have signalled made money on average after costs.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler

from cryptobot.config import ModelConfig

EPS = 1e-6


def _log_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


class OnlineModel:
    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.scaler = StandardScaler()
        # A small constant step keeps probabilities calibrated and lets the model keep
        # adapting as market behaviour drifts (a decaying rate would freeze it over time).
        self.clf = SGDClassifier(
            loss="log_loss", alpha=1e-4, learning_rate="constant", eta0=cfg.learning_rate, random_state=0
        )
        self.n_seen = 0
        self.n_pos = 0
        self.model_losses: deque[float] = deque(maxlen=cfg.eval_window)
        self.base_losses: deque[float] = deque(maxlen=cfg.eval_window)
        self.recent_labels: deque[float] = deque(maxlen=cfg.eval_window)
        # (label, net return) of samples the model would have traded on
        self.signals: deque[tuple[float, float]] = deque(maxlen=cfg.eval_window)

    @property
    def fitted(self) -> bool:
        return self.n_seen > 0 and hasattr(self.clf, "coef_")

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if not self.fitted:
            return np.full(len(X), 0.5)
        return self.clf.predict_proba(self.scaler.transform(X))[:, 1]

    def learn(self, X: np.ndarray, y: np.ndarray, returns: np.ndarray | None = None) -> None:
        X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
        r = np.asarray(returns, dtype=float) if returns is not None else np.where(y > 0, 1.0, -1.0)
        mask = np.isfinite(X).all(axis=1) & np.isfinite(y) & np.isfinite(r)
        X, y, r = X[mask], y[mask], r[mask]
        if len(X) == 0:
            return
        if self.fitted and len(self.recent_labels) >= 50:
            p = self.predict_proba(X)
            base_rate = float(np.mean(self.recent_labels))
            self.model_losses.extend(_log_loss(p, y))
            self.base_losses.extend(_log_loss(np.full(len(y), base_rate), y))
            take = p >= self.cfg.entry_threshold
            self.signals.extend(zip(y[take], r[take], strict=True))
        self.n_seen += len(y)
        self.n_pos += int(y.sum())
        self.recent_labels.extend(y)
        self.scaler.partial_fit(X)
        self.clf.partial_fit(self.scaler.transform(X), y.astype(int), classes=[0, 1])

    def edge(self) -> float:
        """Baseline log-loss minus model log-loss over the eval window (> 0 means better)."""
        if len(self.model_losses) < 100:
            return float("-inf")
        return float(np.mean(self.base_losses) - np.mean(self.model_losses))

    def signal_return(self) -> float:
        """Average net return of recent trades the model would have taken."""
        if len(self.signals) < self.cfg.min_signals:
            return float("nan")
        return float(np.mean([r for _, r in self.signals]))

    def is_trustworthy(self) -> bool:
        sr = self.signal_return()
        return (
            self.n_seen >= self.cfg.min_train_samples and self.edge() > self.cfg.min_edge and np.isfinite(sr) and sr > 0
        )

    def stats(self) -> dict[str, float]:
        return {
            "samples": self.n_seen,
            "win_rate": self.n_pos / self.n_seen if self.n_seen else float("nan"),
            "edge": self.edge(),
            "precision_at_entry": float(np.mean([y for y, _ in self.signals])) if self.signals else float("nan"),
            "signal_return": self.signal_return(),
            "signals_scored": len(self.signals),
            "trustworthy": self.is_trustworthy(),
        }

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load_or_new(path: str | Path, cfg: ModelConfig) -> OnlineModel:
        path = Path(path)
        if path.exists():
            model = joblib.load(path)
            model.cfg = cfg
            return model
        return OnlineModel(cfg)
