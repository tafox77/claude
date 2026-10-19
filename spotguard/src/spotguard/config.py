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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


TOP_LEVEL = {"mode", "timezone", "risk", "market", "execution", "strategy", "screening", "allowlist"}


def load_policy(path: str | Path) -> Policy:
    raw_bytes = Path(path).read_bytes()
    try:
        raw = tomllib.loads(raw_bytes.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{path}: not valid TOML: {exc}") from exc
    unknown_top = set(raw) - TOP_LEVEL
    if unknown_top:  # a typo like [risks] must not silently fall back to defaults
        raise PolicyError(f"unknown top-level keys/sections: {sorted(unknown_top)}")
    if set(_section(raw, "allowlist")) - {"cex"}:
        raise PolicyError(f"unknown [allowlist] keys: {sorted(set(raw['allowlist']) - {'cex'})}")
    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):  # 'sandbox' is not implemented (Coinbase has no ccxt sandbox)
        raise PolicyError("mode must be 'paper' or 'live'")
    timezone = raw.get("timezone", "UTC")
    try:
        ZoneInfo(str(timezone))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PolicyError(f"unknown timezone {timezone!r}") from exc
    risk_raw = dict(_section(raw, "risk"))
    groups_raw = risk_raw.pop("correlation_groups", {})
    if not isinstance(groups_raw, dict) or not all(
        isinstance(m, list) and m and all(isinstance(x, str) for x in m) for m in groups_raw.values()
    ):
        raise PolicyError("risk.correlation_groups must be a table of non-empty arrays of asset keys")
    pct_keys = {k: PCT for k in RiskLimits.__dataclass_fields__ if k.endswith("_pct")}
    risk = RiskLimits(
        **_decimals(RiskLimits, risk_raw, "risk", pct_keys),
        correlation_groups=tuple(sorted((g, tuple(sorted(m))) for g, m in groups_raw.items())),
    )
    if risk.max_positions < 1:
        raise PolicyError("risk.max_positions must be >= 1")
    for key in ("risk_per_trade_pct", "daily_loss_stop_pct", "drawdown_stop_pct", "max_asset_exposure_pct",
                "max_total_exposure_pct"):  # fmt: skip
        if getattr(risk, key) <= 0:
            raise PolicyError(f"risk.{key} must be > 0")
    seen_groups: set[str] = set()
    for _name, members in risk.correlation_groups:
        dup = seen_groups & set(members)
        if dup:
            raise PolicyError(f"asset(s) {sorted(dup)} appear in more than one correlation group")
        seen_groups |= set(members)
    if risk.max_asset_exposure_pct > risk.max_total_exposure_pct:
        raise PolicyError("risk.max_asset_exposure_pct cannot exceed max_total_exposure_pct")
    market = MarketPolicy(**_decimals(MarketPolicy, _section(raw, "market"), "market", {"max_spread_pct": PCT}))
    for key in ("max_quote_age_s", "max_clock_skew_s", "heartbeat_timeout_s", "max_spread_pct"):
        if getattr(market, key) <= 0:
            raise PolicyError(f"market.{key} must be > 0")
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
    strategy = StrategyParams(
        **_decimals(
            StrategyParams, _section(raw, "strategy"), "strategy", {k: PCT for k in ("min_edge_pct", "stop_pct", "target_pct")}
        )  # fmt: skip
    )
    if not 0 < strategy.fast_window_s < strategy.slow_window_s:
        raise PolicyError("strategy.fast_window_s must be > 0 and shorter than slow_window_s")
    if strategy.stop_pct <= 0 or strategy.target_pct <= 0 or strategy.max_hold_s <= 0:
        raise PolicyError("strategy stop_pct, target_pct and max_hold_s must be > 0")
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
        if set(entry) != {"venue", "market_id", "base_id", "quote_id"}:
            raise PolicyError(f"allowlist entry must have exactly venue, market_id, base_id, quote_id: {entry}")
        if entry["venue"] != "coinbase":
            raise PolicyError(f"venue {entry['venue']!r} is not supported (coinbase only)")
        market_id = CexMarketId(entry["venue"], entry["market_id"], entry["base_id"], entry["quote_id"])
        if market_id in allow:
            raise PolicyError(f"duplicate allowlist entry {market_id.key}")
        allow.append(market_id)
    unknown_members = seen_groups - {m.key for m in allow}
    if unknown_members:
        raise PolicyError(f"correlation group members not on the allowlist: {sorted(unknown_members)}")
    canonical = json.dumps(raw, sort_keys=True, default=str).encode()
    return Policy(
        mode=mode,
        timezone=str(timezone),
        risk=risk,
        market=market,
        execution=execution,
        strategy=strategy,
        screening=screening,
        cex_allowlist=tuple(allow),
        version="policy-" + hashlib.sha256(canonical).hexdigest()[:16],
    )
