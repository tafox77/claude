"""Load and validate the TOML configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .data import timeframe_ms
from .strategies import build_strategy


@dataclass
class RiskConfig:
    max_open_positions: int = 4
    max_daily_loss_pct: float = 0.05  # stop opening trades after losing this much in a UTC day
    min_order_usd: float = 10.0
    max_order_usd: float = 250.0  # hard cap on any single buy


@dataclass
class AssetConfig:
    symbol: str
    strategy: str
    allocation_pct: float  # share of total equity per position
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float = 0.0  # 0 disables
    cooldown_bars: int = 3  # bars to wait after an exit before re-entering
    params: dict = field(default_factory=dict)


@dataclass
class Config:
    assets: list[AssetConfig]
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategies: dict[str, dict] = field(default_factory=dict)
    mode: str = "paper"
    exchange: str = "kraken"
    quote: str = "USD"
    timeframe: str = "15m"
    poll_seconds: int = 60
    history_bars: int = 200
    starting_cash: float = 1000.0
    fee_rate: float = 0.0026
    slippage_pct: float = 0.001
    db_path: str = "cryptobot.db"
    notify_webhook_url: str = ""

    def strategy_params(self, asset: AssetConfig) -> dict:
        return {**self.strategies.get(asset.strategy, {}), **asset.params}


def _pct(name: str, value: float, allow_zero: bool = False) -> None:
    lo_ok = value >= 0 if allow_zero else value > 0
    if not (lo_ok and value < 1):
        raise ValueError(f"{name} must be a fraction in {'[0' if allow_zero else '(0'}, 1)")


def validate(cfg: Config) -> Config:
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    timeframe_ms(cfg.timeframe)
    if not cfg.assets:
        raise ValueError("configure at least one [[assets]] entry")
    symbols = [a.symbol for a in cfg.assets]
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbols in [[assets]]")
    for a in cfg.assets:
        if not a.symbol.endswith("/" + cfg.quote):
            raise ValueError(f"{a.symbol}: symbol must be quoted in {cfg.quote}")
        _pct(f"{a.symbol} allocation_pct", a.allocation_pct)
        _pct(f"{a.symbol} stop_loss_pct", a.stop_loss_pct)
        if a.take_profit_pct <= 0:
            raise ValueError(f"{a.symbol} take_profit_pct must be > 0")
        _pct(f"{a.symbol} trailing_stop_pct", a.trailing_stop_pct, allow_zero=True)
        build_strategy(a.strategy, cfg.strategy_params(a))  # fail fast on bad params
    if sum(a.allocation_pct for a in cfg.assets) > 1.0 + 1e-9:
        raise ValueError("sum of allocation_pct across assets must be <= 1")
    r = cfg.risk
    _pct("risk.max_daily_loss_pct", r.max_daily_loss_pct)
    if r.max_open_positions < 1:
        raise ValueError("risk.max_open_positions must be >= 1")
    if not 0 < r.min_order_usd <= r.max_order_usd:
        raise ValueError("need 0 < risk.min_order_usd <= risk.max_order_usd")
    if cfg.poll_seconds < 5:
        raise ValueError("poll_seconds must be >= 5")
    return cfg


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    assets = [AssetConfig(**a) for a in raw.pop("assets", [])]
    risk = RiskConfig(**raw.pop("risk", {}))
    return validate(Config(assets=assets, risk=risk, **raw))
