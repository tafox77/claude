from __future__ import annotations

import os
from datetime import timedelta

import numpy as np
import pytest
from conftest import NOW

from spotguard.learning.evaluate import purged_walk_forward, rug_detection_metrics
from spotguard.learning.features import FEATURES, feature_matrix
from spotguard.learning.labels import label_available_at, net_forward_returns
from spotguard.learning.registry import PromotionRejected, Registry
from spotguard.learning.shadow import DriftMonitor, ShadowRunner
from spotguard.learning.train import train_candidate
from spotguard.storage import Store


def series(n=3000, seed=0, signal=0.0):
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = signal * np.sign(r[i - 1]) * 0.0004 + rng.normal(0, 0.001)
    close = 100 * np.exp(np.cumsum(r))
    return {"close": close, "high": close * 1.0005, "low": close * 0.9995, "spread": np.full(n, 0.0002),
            "imbalance": rng.uniform(-1, 1, n)}  # fmt: skip


def test_features_are_causal():
    a = series()
    full = feature_matrix(a["close"], a["high"], a["low"], a["spread"], a["imbalance"])
    cut = 500
    part = feature_matrix(*(v[: cut + 1] for v in (a["close"], a["high"], a["low"], a["spread"], a["imbalance"])))
    np.testing.assert_allclose(full[cut], part[cut])


def test_labels_exist_only_after_horizon_and_include_costs():
    a = series()
    r = net_forward_returns(a["close"], a["spread"], 15, 0.006)
    assert np.isnan(r[-15:]).all() and np.isfinite(r[:-15]).all()
    flat = net_forward_returns(np.full(50, 100.0), np.full(50, 0.0002), 5, 0.006)
    assert (flat[:-5] < 0).all()  # a flat market loses fees + spread
    assert label_available_at(NOW, 15, 60) == NOW + timedelta(minutes=16)


def test_purged_splits_never_overlap_labels_and_holdout_is_untouched():
    splits, holdout = purged_walk_forward(3000, 4, horizon=15, embargo=5, holdout_frac=0.2)
    assert len(splits) == 4
    for s in splits:
        assert s.train.max() + 15 + 5 <= s.test.min()  # training labels end before the test window
        assert s.test.max() < holdout.min()
    assert holdout.min() >= int(3000 * 0.8)


def test_training_registers_immutable_candidate_with_holdout(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reg = Registry(store, tmp_path / "models")
    model_id, metrics = train_candidate([series(signal=0.5)], reg, NOW)
    row = reg.row(model_id)
    assert row["status"] == "candidate" and "holdout" in metrics and len(metrics["folds"]) >= 2
    for key in (
        "baseline_no_trade",
        "baseline_fixed_momentum",
        "baseline_buy_hold_same_exposure",
        "model_stressed_costs",
    ):
        assert key in metrics["holdout"]
    assert oct(os.stat(row["artifact_path"]).st_mode)[-3:] == "444"
    assert reg.load(model_id) is not None
    same, _ = train_candidate([series(signal=0.5)], reg, NOW)
    assert same == model_id  # deterministic training → identical artifact hash


def test_tampered_artifact_is_refused(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reg = Registry(store, tmp_path / "models")
    model_id, _ = train_candidate([series()], reg, NOW)
    path = reg.row(model_id)["artifact_path"]
    os.chmod(path, 0o644)
    with open(path, "ab") as fh:
        fh.write(b"x")
    with pytest.raises(PromotionRejected):
        reg.load(model_id)


def test_candidate_promotion_rejected_without_evidence(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reg = Registry(store, tmp_path / "models")
    model_id, _ = train_candidate([series()], reg, NOW)
    with pytest.raises(PromotionRejected, match="must be shadow"):
        reg.approve(model_id, "alice", "looks good", NOW)
    reg.to_shadow(model_id, "alice", NOW)
    with pytest.raises(PromotionRejected) as exc:
        reg.approve(model_id, "alice", "looks good", NOW)
    msg = str(exc.value)
    assert "30" in msg and "200" in msg
    assert reg.active() is None
    events = store.query("SELECT kind FROM model_events WHERE model_id = ?", (model_id,))
    assert "approval_rejected" in [e[0] for e in events]


def test_rollback_only_to_previously_approved(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reg = Registry(store, tmp_path / "models")
    model_id, _ = train_candidate([series()], reg, NOW)
    with pytest.raises(PromotionRejected):
        reg.rollback(model_id, "alice", "bad model", NOW)


class StubModel:
    def predict_proba(self, X):
        return np.tile([0.4, 0.6], (len(X), 1))


def _quotes(n, drift=0.0, start=NOW):
    from helpers import BTC, Clock, book

    c = Clock(wall=start)
    price = 100000.0
    for i in range(n):
        price *= 1 + drift + (0.0002 if i % 2 else -0.0002)
        c.advance(60)
        yield book(c, BTC, bid=f"{price:.2f}", ask=f"{price + 1:.2f}"), c.wall


def test_shadow_writes_predictions_and_delayed_labels_without_trading(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    runner = ShadowRunner(store, "m1", StubModel(), horizon=5, interval_s=60, drift=None)
    for q, at in _quotes(60):
        runner.on_quote(q, at)
    preds = store.query("SELECT count(*) FROM predictions")[0][0]
    labels = store.query("SELECT count(*) FROM labels")[0][0]
    assert preds > 0 and 0 < labels < preds  # most recent predictions still await their horizon
    late = store.query("SELECT p.at, l.labeled_at FROM predictions p JOIN labels l USING (pred_id)")
    assert all(row[1] > row[0] for row in late)
    assert store.query("SELECT count(*) FROM orders")[0][0] == 0
    assert not hasattr(runner, "orders")


def test_drift_alarm_blocks_new_entries(policy, tmp_path):
    from spotguard.risk import RiskEngine

    store = Store(tmp_path / "db.sqlite")
    risk = RiskEngine(policy, store, NOW)
    reference = {name: {"quantiles": list(np.linspace(-0.001, 0.001, 9))} for name in FEATURES}
    drift = DriftMonitor(reference, window=20, threshold=0.25)
    runner = ShadowRunner(store, "m1", StubModel(), 5, 60, drift,
                          on_anomaly=lambda d: risk.set_block_entries(True, "drift-monitor", str(d), NOW))  # fmt: skip
    for q, at in _quotes(80, drift=0.01):  # a violently trending market far outside the reference
        runner.on_quote(q, at)
    assert len(runner.anomalies) == 1 and risk.state.block_entries  # one latched alarm, not one per bar
    assert "drift" in risk.state.block_reason or "psi" in risk.state.block_reason


def test_rug_metrics_require_independent_incidents():
    m = rug_detection_metrics(
        flagged_at={"a": 100.0, "b": None, "c": 50.0},
        incidents={"a": 200.0, "b": 300.0, "c": None, "d": None},
        unknown={"d"},
    )
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["false_negatives"] == ["b"]
    assert m["unknown_coverage"] == 0.25 and m["median_lead_s"] == 100.0
