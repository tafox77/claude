"""Typed, immutable policy loaded from a reviewed TOML file.

The policy hash is recorded with every intent, security report and model decision. Nothing in the
application mutates a loaded policy (all dataclasses are frozen); changing limits means editing the
file, which produces a new policy version.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from spotguard.identities import CexMarketId


class PolicyError(ValueError):
    pass


def _dec(value: Any, name: str, *, lo: Decimal | None = Decimal(0), hi: Decimal | None = None) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError(f"{name}: not a number: {value!r}") from exc
    if not d.is_finite():
        raise PolicyError(f"{name}: must be finite")
    if lo is not None and d < lo:
        raise PolicyError(f"{name}: must be >= {lo}")
    if hi is not None and d > hi:
        raise PolicyError(f"{name}: must be <= {hi}")
    return d


@dataclass(frozen=True)
class RiskLimits:
    virtual_equity: Decimal = Decimal("10000")
    max_asset_exposure_pct: Decimal = Decimal("0.05")
    max_total_exposure_pct: Decimal = Decimal("0.15")
    max_positions: int = 3
    risk_per_trade_pct: Decimal = Decimal("0.0025")
    cost_allowance_pct: Decimal = Decimal("0.003")
    daily_loss_stop_pct: Decimal = Decimal("0.02")
    drawdown_stop_pct: Decimal = Decimal("0.05")
    max_group_exposure_pct: Decimal = Decimal("0.10")
    correlation_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def group_of(self, asset_key: str) -> str | None:
        for name, members in self.correlation_groups:
            if asset_key in members:
                return name
        return None


@dataclass(frozen=True)
class MarketPolicy:
    max_quote_age_s: Decimal = Decimal("2")
    max_spread_pct: Decimal = Decimal("0.002")
    max_clock_skew_s: Decimal = Decimal("5")
    heartbeat_timeout_s: Decimal = Decimal("10")


@dataclass(frozen=True)
class ExecutionPolicy:
    entry_time_in_force: str = "IOC"
    max_entry_slippage_pct: Decimal = Decimal("0.001")
    max_exit_slippage_pct: Decimal = Decimal("0.003")
    emergency_max_impact_pct: Decimal = Decimal("0.02")
    order_ack_timeout_s: Decimal = Decimal("5")
    taker_fee_pct: Decimal = Decimal("0.006")
    maker_fee_pct: Decimal = Decimal("0.004")


@dataclass(frozen=True)
class StrategyParams:
    name: str = "momentum_v1"
    fast_window_s: int = 30
    slow_window_s: int = 300
    min_edge_pct: Decimal = Decimal("0.0015")
    stop_pct: Decimal = Decimal("0.006")
    target_pct: Decimal = Decimal("0.012")
    max_hold_s: int = 3600


@dataclass(frozen=True)
class ScreeningPolicy:
    min_liquidity_usd: Decimal = Decimal("250000")
    min_age_days: Decimal = Decimal("7")
    max_top10_holder_pct: Decimal = Decimal("0.20")
    max_buy_tax: Decimal = Decimal("0.02")
    max_sell_tax: Decimal = Decimal("0.02")
    max_evidence_age_s: Decimal = Decimal("60")
    min_lp_locked_pct: Decimal = Decimal("0.95")
    min_lp_lock_remaining_days: Decimal = Decimal("30")
    allow_mutable_metadata: bool = False


@dataclass(frozen=True)
class Policy:
    mode: str = "paper"
    timezone: str = "UTC"
    risk: RiskLimits = field(default_factory=RiskLimits)
    market: MarketPolicy = field(default_factory=MarketPolicy)
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    screening: ScreeningPolicy = field(default_factory=ScreeningPolicy)
    cex_allowlist: tuple[CexMarketId, ...] = ()
    version: str = "builtin-defaults"

    def is_allowed_market(self, market: CexMarketId) -> bool:
        return market in self.cex_allowlist


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{name}] must be a table")
    return value


def _decimals(cls: type, data: dict, prefix: str, bounds: dict[str, tuple] | None = None) -> dict:
    out: dict[str, Any] = {}
    bounds = bounds or {}
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise PolicyError(f"[{prefix}] unknown keys: {sorted(unknown)}")
    for key, value in data.items():
        default = getattr(cls(), key)
        if isinstance(default, Decimal):
            lo, hi = bounds.get(key, (Decimal(0), None))
            out[key] = _dec(value, f"{prefix}.{key}", lo=lo, hi=hi)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise PolicyError(f"{prefix}.{key}: must be true/false")
            out[key] = value
        elif isinstance(default, int):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError(f"{prefix}.{key}: must be a non-negative integer")
            out[key] = value
        else:
            out[key] = value
    return out


PCT = (Decimal(0), Decimal(1))


def load_policy(path: str | Path) -> Policy:
    raw_bytes = Path(path).read_bytes()
    raw = tomllib.loads(raw_bytes.decode())
    mode = raw.get("mode", "paper")
    if mode not in ("paper", "sandbox", "live"):
        raise PolicyError("mode must be paper, sandbox or live")
    risk_raw = dict(_section(raw, "risk"))
    groups_raw = risk_raw.pop("correlation_groups", {})
    pct_keys = {k: PCT for k in RiskLimits.__dataclass_fields__ if k.endswith("_pct")}
    risk = RiskLimits(
        **_decimals(RiskLimits, risk_raw, "risk", pct_keys),
        correlation_groups=tuple(sorted((g, tuple(sorted(m))) for g, m in groups_raw.items())),
    )
    if risk.max_positions < 1:
        raise PolicyError("risk.max_positions must be >= 1")
    if risk.max_asset_exposure_pct > risk.max_total_exposure_pct:
        raise PolicyError("risk.max_asset_exposure_pct cannot exceed max_total_exposure_pct")
    market = MarketPolicy(**_decimals(MarketPolicy, _section(raw, "market"), "market"))
    execution = ExecutionPolicy(
        **_decimals(
            ExecutionPolicy,
            _section(raw, "execution"),
            "execution",
            {k: PCT for k in ExecutionPolicy.__dataclass_fields__ if k.endswith("_pct")},
        )
    )
    if execution.entry_time_in_force not in ("IOC", "GTC"):
        raise PolicyError("execution.entry_time_in_force must be IOC or GTC")
    strategy = StrategyParams(**_decimals(StrategyParams, _section(raw, "strategy"), "strategy"))
    screening = ScreeningPolicy(
        **_decimals(
            ScreeningPolicy,
            _section(raw, "screening"),
            "screening",
            {k: PCT for k in ("max_top10_holder_pct", "max_buy_tax", "max_sell_tax", "min_lp_locked_pct")},
        )
    )
    allow = []
    for entry in _section(raw, "allowlist").get("cex", []):
        allow.append(CexMarketId(entry["venue"], entry["market_id"], entry["base_id"], entry["quote_id"]))
    canonical = json.dumps(raw, sort_keys=True, default=str).encode()
    return Policy(
        mode=mode,
        timezone=raw.get("timezone", "UTC"),
        risk=risk,
        market=market,
        execution=execution,
        strategy=strategy,
        screening=screening,
        cex_allowlist=tuple(allow),
        version="policy-" + hashlib.sha256(canonical).hexdigest()[:16],
    )
