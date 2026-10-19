"""Offline training of a candidate model with purged walk-forward evaluation and a final holdout."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from spotguard.learning.evaluate import evaluate_rows, make_model, purged_walk_forward
from spotguard.learning.features import FEATURES, feature_matrix
from spotguard.learning.labels import labels_from_returns, net_forward_returns
from spotguard.learning.registry import Registry


def build_dataset(arrays: list[dict[str, np.ndarray]], horizon: int, fee_pct: float):
    Xs, ys, rs, vols = [], [], [], []
    for a in arrays:  # markets are concatenated in time order per market; folds stay within the time axis
        X = feature_matrix(a["close"], a["high"], a["low"], a["spread"], a["imbalance"])
        r = net_forward_returns(a["close"], a["spread"], horizon, fee_pct)
        Xs.append(X)
        rs.append(r)
        ys.append(labels_from_returns(r))
        vols.append(X[:, FEATURES.index("vol_15")])
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(rs), np.concatenate(vols)


def feature_reference(X: np.ndarray) -> dict:
    ok = X[np.isfinite(X).all(axis=1)]
    return {
        name: {"quantiles": np.nanpercentile(ok[:, i], [10, 20, 30, 40, 50, 60, 70, 80, 90]).tolist()}
        for i, name in enumerate(FEATURES)
    }


def train_candidate(arrays: list[dict[str, np.ndarray]], registry: Registry, now: datetime, horizon: int = 15,
                    fee_pct: float = 0.006, threshold: float = 0.55, folds: int = 4, holdout_frac: float = 0.2,
                    embargo: int = 5) -> tuple[str, dict]:  # fmt: skip
    """Train, evaluate on purged folds, fit on all pre-holdout data, then score the holdout exactly once."""
    if len(arrays) != 1:
        raise ValueError("train one market series at a time so chronological splits stay meaningful")
    X, y, r, vol = build_dataset(arrays, horizon, fee_pct)
    splits, holdout = purged_walk_forward(len(X), folds, horizon, embargo, holdout_frac)
    fold_results = []
    for s in splits:
        tr = s.train[np.isfinite(X[s.train]).all(axis=1) & np.isfinite(y[s.train])]
        if len(np.unique(y[tr])) < 2:
            continue
        m = make_model().fit(X[tr], y[tr])
        fold_results.append(evaluate_rows(m, X, y, r, s.test, threshold, horizon, fee_pct, vol))
    pre = np.arange(0, holdout[0] - horizon - embargo) if len(holdout) else np.arange(len(X))
    pre = pre[np.isfinite(X[pre]).all(axis=1) & np.isfinite(y[pre])]
    final = make_model().fit(X[pre], y[pre])
    holdout_result = evaluate_rows(final, X, y, r, holdout, threshold, horizon, fee_pct, vol)
    metrics = {"folds": fold_results, "holdout": holdout_result, "rows": int(len(X)), "horizon": horizon,
               "threshold": threshold, "fee_pct": fee_pct}  # fmt: skip
    params = {
        "features": FEATURES,
        "horizon": horizon,
        "threshold": threshold,
        "model": "StandardScaler+LogisticRegression",
    }
    model_id = registry.register(final, "logreg_v1", params, metrics, feature_reference(X[pre]), now)
    return model_id, metrics
