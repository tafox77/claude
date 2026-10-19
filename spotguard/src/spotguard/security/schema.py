"""Screening result types and strict value parsing.

PASS_POLICY means only that every configured check passed with fresh evidence. It is never a
statement that a token is safe or legitimate. UNKNOWN (missing, malformed, stale, conflicting or
unimplemented evidence) blocks new exposure exactly like BLOCK.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    PASS_POLICY = "PASS_POLICY"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Evidence:
    source: str
    url: str
    fetched_at: datetime
    raw: Any

    @property
    def evidence_id(self) -> str:
        blob = json.dumps({"source": self.source, "url": self.url, "raw": self.raw}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class Finding:
    check: str
    status: Verdict
    reason: str
    source: str
    evidence_id: str | None
    observed_at: datetime


@dataclass
class SecurityReport:
    asset_key: str
    policy_version: str
    created_at: datetime
    findings: list[Finding] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if not self.findings:
            return Verdict.UNKNOWN
        if any(f.status is Verdict.BLOCK for f in self.findings):
            return Verdict.BLOCK
        if any(f.status is Verdict.UNKNOWN for f in self.findings):
            return Verdict.UNKNOWN
        return Verdict.PASS_POLICY

    def oldest_evidence(self) -> datetime | None:
        times = [e.fetched_at for e in self.evidence]
        return min(times) if times else None

    def is_fresh(self, now: datetime, max_age_s: Decimal) -> bool:
        oldest = self.oldest_evidence()
        if oldest is None:
            return False
        age = (now - oldest).total_seconds()
        return -1.0 <= age <= float(max_age_s)  # evidence from the future is malformed, not fresh

    def allows_entry(self, now: datetime, max_age_s: Decimal) -> tuple[bool, str]:
        if self.verdict is not Verdict.PASS_POLICY:
            return False, f"screening verdict {self.verdict.value}"
        if not self.is_fresh(now, max_age_s):
            return False, f"screening evidence older than {max_age_s}s"
        return True, "configured checks passed with fresh evidence (not a safety guarantee)"

    def reasons(self, *statuses: Verdict) -> list[str]:
        wanted = statuses or (Verdict.BLOCK, Verdict.UNKNOWN)
        return [f"[{f.status.value}] {f.check}: {f.reason}" for f in self.findings if f.status in wanted]

    def to_json(self) -> str:
        return json.dumps(
            [
                {
                    "check": f.check,
                    "status": f.status.value,
                    "reason": f.reason,
                    "source": f.source,
                    "evidence_id": f.evidence_id,
                    "observed_at": f.observed_at.isoformat(),
                }
                for f in self.findings
            ]
        )


class FindingSink:
    """Helper that stamps findings with source/evidence/time."""

    def __init__(self, report: SecurityReport, now: datetime):
        self.report = report
        self.now = now

    def add(self, check: str, status: Verdict, reason: str, ev: Evidence | None = None, source: str = "") -> None:
        self.report.findings.append(
            Finding(check, status, reason, ev.source if ev else source or "policy", ev.evidence_id if ev else None, self.now)
        )

    def ok(self, check: str, reason: str, ev: Evidence | None = None, source: str = "") -> None:
        self.add(check, Verdict.PASS_POLICY, reason, ev, source)

    def block(self, check: str, reason: str, ev: Evidence | None = None, source: str = "") -> None:
        self.add(check, Verdict.BLOCK, reason, ev, source)

    def unknown(self, check: str, reason: str, ev: Evidence | None = None, source: str = "") -> None:
        self.add(check, Verdict.UNKNOWN, reason, ev, source)


# ----- strict parsing: anything not clearly valid is None (UNKNOWN) -------------------------


def flag01(value: Any) -> bool | None:
    """GoPlus-style flag. Only "1"/"0" (or int 1/0) are accepted.

    "true", "false", "yes", "", None, bools and anything else are malformed and return None,
    which callers must treat as UNKNOWN, never as false.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return {1: True, 0: False}.get(value)
    if isinstance(value, str):
        return {"1": True, "0": False}.get(value.strip())
    return None


def ratio(value: Any) -> Decimal | None:
    """A fraction in [0, 1] where 1 means 100%. Rejects NaN, infinities, negatives and >1."""
    d = number(value)
    if d is None or d > 1:
        return None
    return d


def number(value: Any) -> Decimal | None:
    """Non-negative finite decimal, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def timestamp(value: Any, now: datetime, max_future_s: int = 300) -> datetime | None:
    """Epoch seconds/milliseconds or ISO-8601 → aware UTC datetime. Future-dated values are rejected."""
    if value is None or isinstance(value, bool):
        return None
    dt: datetime | None = None
    text = str(value).strip()
    if text.isdigit():
        n = int(text)
        if n > 10**12:
            n //= 1000
        try:
            dt = datetime.fromtimestamp(n, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return None
        dt = dt.astimezone(UTC)
    if (dt - now).total_seconds() > max_future_s:
        return None
    return dt


def is_burn_address(address: str) -> bool:
    a = address.lower()
    return a in {
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "1nc1nerator11111111111111111111111111111111".lower(),
    }
