from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spotguard.config import Policy, load_policy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> Policy:
    return load_policy(ROOT / "policy.toml")
