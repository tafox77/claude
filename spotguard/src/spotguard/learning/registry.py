"""Versioned, immutable model registry.

* model_id = sha256 of the serialized artifact; the file is written once, read-only, and its hash is
  re-verified on every load (tampering is detected).
* Lifecycle: candidate → shadow → approved → retired. Approval needs a named human plus review
  evidence: >= 30 days of shadow/paper operation, >= 200 closed paper trades, an untouched-holdout
  evaluation that beats no-trade after stressed costs, fold stability, and no active drift alarm.
  These are operational minimums, not statistical proof of an edge, and trades are never forced to
  meet them.
* Rollback re-activates a previously approved model. Nothing here can change risk limits.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path

import joblib

from spotguard.storage import Store, iso

MIN_SHADOW_DAYS = 30
MIN_CLOSED_TRADES = 200


class PromotionRejected(RuntimeError):
    pass


class Registry:
    def __init__(self, store: Store, directory: str | Path):
        self.store = store
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def register(self, model, kind: str, params: dict, metrics: dict, feature_reference: dict, now: datetime) -> str:
        buf = io.BytesIO()
        joblib.dump(model, buf)
        blob = buf.getvalue()
        model_id = hashlib.sha256(blob).hexdigest()
        path = self.dir / f"{model_id}.joblib"
        if not path.exists():
            path.write_bytes(blob)
            os.chmod(path, 0o444)
        with self.store.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO models VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)",
                (model_id, iso(now), kind, json.dumps(params), json.dumps(metrics, default=str), str(path),
                 json.dumps(feature_reference)),
            )  # fmt: skip
            c.execute("INSERT INTO model_events (model_id, at, kind, actor, detail) VALUES (?, ?, 'registered', 'trainer', '')",
                      (model_id, iso(now)))  # fmt: skip
        return model_id

    def row(self, model_id: str):
        rows = self.store.query("SELECT * FROM models WHERE model_id = ?", (model_id,))
        if not rows:
            raise KeyError(model_id)
        return rows[0]

    def load(self, model_id: str):
        path = Path(self.row(model_id)["artifact_path"])
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != model_id:
            raise PromotionRejected(f"artifact for {model_id} was modified on disk")
        return joblib.load(io.BytesIO(blob))

    def _set(self, model_id: str, status: str, actor: str, kind: str, detail: dict, now: datetime) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE models SET status = ? WHERE model_id = ?", (status, model_id))
            c.execute("INSERT INTO model_events (model_id, at, kind, actor, detail) VALUES (?, ?, ?, ?, ?)",
                      (model_id, iso(now), kind, actor, json.dumps(detail, default=str)))  # fmt: skip

    def to_shadow(self, model_id: str, actor: str, now: datetime) -> None:
        if self.row(model_id)["status"] != "candidate":
            raise PromotionRejected("only candidates can enter shadow")
        self._set(model_id, "shadow", actor, "shadow", {}, now)

    def review_evidence(self, model_id: str) -> dict:
        preds = self.store.query("SELECT min(at), max(at), count(*) FROM predictions WHERE model_id = ?", (model_id,))[0]
        days = 0.0
        if preds[0] and preds[1]:
            days = (datetime.fromisoformat(preds[1]) - datetime.fromisoformat(preds[0])).total_seconds() / 86400
        closed = self.store.query("SELECT count(*) FROM orders WHERE purpose != 'entry' AND state = 'FILLED'")[0][0]
        metrics = json.loads(self.row(model_id)["metrics"])
        drift = self.store.query("SELECT count(*) FROM model_events WHERE model_id = ? AND kind = 'drift_alarm'",
                                 (model_id,))[0][0]  # fmt: skip
        return {"shadow_days": days, "predictions": preds[2], "closed_paper_trades": closed, "metrics": metrics,
                "drift_alarms": drift}  # fmt: skip

    def approve(self, model_id: str, approver: str, note: str, now: datetime) -> dict:
        row = self.row(model_id)
        ev = self.review_evidence(model_id)
        problems = []
        if not approver.strip() or not note.strip():
            problems.append("named approver and review note required")
        if row["status"] != "shadow":
            problems.append(f"status is {row['status']}, must be shadow")
        if ev["shadow_days"] < MIN_SHADOW_DAYS:
            problems.append(f"shadow period {ev['shadow_days']:.1f} days < {MIN_SHADOW_DAYS}")
        if ev["closed_paper_trades"] < MIN_CLOSED_TRADES:
            problems.append(f"{ev['closed_paper_trades']} closed paper trades < {MIN_CLOSED_TRADES}")
        holdout = ev["metrics"].get("holdout") or {}
        stressed = (holdout.get("model_stressed_costs") or {}).get("net_return")
        if stressed is None:
            problems.append("no untouched-holdout evaluation recorded")
        elif stressed <= 0:
            problems.append(f"holdout net return under stressed costs is {stressed:.4f} (no better than no-trade)")
        folds = ev["metrics"].get("folds") or []
        means = [f["model"]["mean"] for f in folds if f["model"]["trades"]]
        if len(means) < 2 or sum(m > 0 for m in means) < len(means) * 0.6:
            problems.append("walk-forward folds are not consistently positive")
        if ev["drift_alarms"]:
            problems.append(f"{ev['drift_alarms']} drift alarm(s) recorded")
        if problems:
            with self.store.tx() as c:
                c.execute("INSERT INTO model_events (model_id, at, kind, actor, detail) VALUES (?, ?, 'approval_rejected', ?, ?)",
                          (model_id, iso(now), approver or "?", json.dumps(problems)))  # fmt: skip
            raise PromotionRejected("; ".join(problems))
        for prev in self.store.query("SELECT model_id FROM models WHERE status = 'approved'"):
            self._set(prev[0], "retired", approver, "superseded", {"by": model_id}, now)
        self._set(model_id, "approved", approver, "approved", {"note": note, "evidence": ev}, now)
        return ev

    def active(self) -> str | None:
        rows = self.store.query("SELECT model_id FROM models WHERE status = 'approved'")
        return rows[0][0] if rows else None

    def rollback(self, to_model_id: str, actor: str, reason: str, now: datetime) -> None:
        was_approved = self.store.query("SELECT 1 FROM model_events WHERE model_id = ? AND kind = 'approved'", (to_model_id,))
        if not was_approved:
            raise PromotionRejected("can only roll back to a model that was previously approved")
        current = self.active()
        if current:
            self._set(current, "retired", actor, "rolled_back", {"reason": reason, "to": to_model_id}, now)
        self._set(to_model_id, "approved", actor, "rollback_target", {"reason": reason}, now)

    def record_drift(self, model_id: str, detail: dict, now: datetime) -> None:
        with self.store.tx() as c:
            c.execute("INSERT INTO model_events (model_id, at, kind, actor, detail) VALUES (?, ?, 'drift_alarm', 'monitor', ?)",
                      (model_id, iso(now), json.dumps(detail)))  # fmt: skip
