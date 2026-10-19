"""Walk-forward evaluation with purging, an untouched final holdout, costs and baselines."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


@dataclass(frozen=True)
class Split:
    train: np.ndarray
    test: np.ndarray


def purged_walk_forward(n: int, folds: int, horizon: int, embargo: int, holdout_frac: float) -> tuple[list[Split], np.ndarray]:
    """Chronological folds over the non-holdout region.

    Training rows end `horizon + embargo` bars before each test window starts, so no training label
    overlaps a test bar. The final holdout is returned separately and never used for selection.
    """
    holdout_start = int(n * (1 - holdout_frac))
    region = holdout_start
    test_len = region // (folds + 1)
    splits = []
    for k in range(1, folds + 1):
        test_start = k * test_len
        test_end = min(region, test_start + test_len)
        train_end = test_start - horizon - embargo
        if train_end <= 0 or test_end <= test_start:
            continue
        splits.append(Split(np.arange(0, train_end), np.arange(test_start, test_end - horizon)))
    holdout = np.arange(holdout_start + horizon + embargo, n - horizon)
    return splits, holdout


def make_model(c: float = 1.0):
    # Preprocessing lives inside the pipeline, so it is fitted on training folds only.
    return make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=1000))


def simulate(prob: np.ndarray, net_ret: np.ndarray, threshold: float, horizon: int, cost_stress: float = 0.0,
             fee_pct: float = 0.0) -> np.ndarray:  # fmt: skip
    """Non-overlapping trades: enter when prob >= threshold, hold `horizon` bars. Returns per-trade net returns."""
    trades = []
    t = 0
    while t < len(prob):
        if np.isfinite(prob[t]) and np.isfinite(net_ret[t]) and prob[t] >= threshold:
            trades.append(net_ret[t] - cost_stress * 2 * fee_pct)
            t += horizon
        else:
            t += 1
    return np.array(trades)


def trade_metrics(trades: np.ndarray, rng: np.random.Generator) -> dict:
    if len(trades) == 0:
        return {"trades": 0, "net_return": 0.0, "mean": 0.0, "ci95": [0.0, 0.0], "profit_factor": 0.0,
                "max_drawdown": 0.0, "win_rate": 0.0}  # fmt: skip
    equity = np.cumprod(1 + trades)
    dd = float(np.min(equity / np.maximum.accumulate(equity) - 1))
    boots = [float(np.mean(rng.choice(trades, len(trades)))) for _ in range(500)]
    wins, losses = trades[trades > 0].sum(), -trades[trades <= 0].sum()
    return {
        "trades": int(len(trades)),
        "net_return": float(equity[-1] - 1),
        "mean": float(np.mean(trades)),
        "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
        "profit_factor": float(wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0,
        "max_drawdown": dd,
        "win_rate": float(np.mean(trades > 0)),
    }


def evaluate_rows(model, X: np.ndarray, y: np.ndarray, r: np.ndarray, rows: np.ndarray, threshold: float, horizon: int,
                  fee_pct: float, vol: np.ndarray, seed: int = 0) -> dict:  # fmt: skip
    rng = np.random.default_rng(seed)
    ok = rows[np.isfinite(X[rows]).all(axis=1) & np.isfinite(y[rows])]
    prob = np.full(len(X), np.nan)
    prob[ok] = model.predict_proba(X[ok])[:, 1]
    mask = np.zeros(len(X), bool)
    mask[ok] = True
    p = np.where(mask, prob, np.nan)
    trades = simulate(p, np.where(mask, r, np.nan), threshold, horizon)
    stressed = simulate(p, np.where(mask, r, np.nan), threshold, horizon, cost_stress=1.0, fee_pct=fee_pct)
    rule = np.where(mask, (X[:, -1] > 0).astype(float), np.nan)  # fixed momentum baseline (ema_gap > 0)
    fixed = simulate(rule, np.where(mask, r, np.nan), 0.5, horizon)
    exposure_bars = len(trades) * horizon
    per_bar_hold = np.nanmean(r[ok]) / horizon if len(ok) else 0.0
    preds = prob[ok] >= threshold
    tp = int(np.sum(preds & (y[ok] == 1)))
    regimes = {}
    if len(ok) >= 30:
        cuts = np.nanpercentile(vol[ok], [33, 66])
        for name, sel in (("low_vol", vol[ok] <= cuts[0]), ("mid_vol", (vol[ok] > cuts[0]) & (vol[ok] <= cuts[1])),
                          ("high_vol", vol[ok] > cuts[1])):  # fmt: skip
            sub = np.full(len(X), np.nan)
            sub[ok[sel]] = prob[ok[sel]]
            regimes[name] = trade_metrics(simulate(sub, np.where(mask, r, np.nan), threshold, horizon), rng)["mean"]
    return {
        "rows": int(len(ok)),
        "log_loss": float(log_loss(y[ok], prob[ok], labels=[0, 1])) if len(ok) else float("nan"),
        "brier": float(brier_score_loss(y[ok], prob[ok])) if len(ok) else float("nan"),
        "base_rate": float(np.mean(y[ok])) if len(ok) else float("nan"),
        "precision": tp / int(np.sum(preds)) if np.sum(preds) else float("nan"),
        "recall": tp / int(np.sum(y[ok] == 1)) if np.sum(y[ok] == 1) else float("nan"),
        "model": trade_metrics(trades, rng),
        "model_stressed_costs": trade_metrics(stressed, rng),
        "baseline_no_trade": trade_metrics(np.array([]), rng),
        "baseline_fixed_momentum": trade_metrics(fixed, rng),
        "baseline_buy_hold_same_exposure": float(per_bar_hold * exposure_bars),
        "regimes_mean_trade": regimes,
    }


def rug_detection_metrics(flagged_at: dict[str, float | None], incidents: dict[str, float | None],
                          unknown: set[str]) -> dict:  # fmt: skip
    """Screening quality against INDEPENDENT incident labels (asset -> incident time, None = no incident).

    A price drop is not an incident label. Reports precision/recall, false negatives, UNKNOWN coverage
    and detection delay (incident time - first block time; positive means flagged before the incident).
    """
    assets = set(incidents)
    tp = {a for a in assets if incidents[a] is not None and flagged_at.get(a) is not None}
    fp = {a for a in assets if incidents[a] is None and flagged_at.get(a) is not None}
    fn = {a for a in assets if incidents[a] is not None and flagged_at.get(a) is None}
    delays = [incidents[a] - flagged_at[a] for a in tp]  # type: ignore[operator]
    return {
        "precision": len(tp) / (len(tp) + len(fp)) if tp or fp else float("nan"),
        "recall": len(tp) / (len(tp) + len(fn)) if tp or fn else float("nan"),
        "false_negatives": sorted(fn),
        "unknown_coverage": len(unknown & assets) / len(assets) if assets else float("nan"),
        "median_lead_s": float(np.median(delays)) if delays else float("nan"),
    }
