"""Configuration loaded from YAML, with safe defaults (paper trading)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01  # fraction of equity lost if the stop is hit
    max_position_pct: float = 0.20  # cap on a single position's share of equity
    stop_loss_atr: float = 2.0  # stop distance in ATRs below entry
    take_profit_atr: float = 3.0  # target distance in ATRs above entry
    trailing_atr: float = 0.0  # 0 disables the trailing stop
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03  # stop opening trades after this daily drawdown
    max_drawdown_pct: float = 0.15  # halt all new trades after this drop from peak equity


@dataclass
class SafetyConfig:
    # On-chain token checks (used when a symbol has a contract address configured)
    min_liquidity_usd: float = 100_000
    min_volume_24h_usd: float = 250_000
    min_pair_age_days: float = 7
    max_buy_tax: float = 0.05
    max_sell_tax: float = 0.05
    max_top10_holder_pct: float = 0.50
    min_lp_locked_pct: float = 0.80
    # Exchange market checks (used for every symbol)
    min_quote_volume_24h: float = 1_000_000
    max_spread_pct: float = 0.005
    min_history_bars: int = 500
    cache_minutes: float = 30


@dataclass
class ModelConfig:
    horizon_bars: int = 12  # label window and max holding time
    entry_threshold: float = 0.55  # min P(target before stop) to enter
    exit_threshold: float = 0.35  # exit early if P falls below this
    min_train_samples: int = 500  # never trade before this many labeled samples
    eval_window: int = 2000  # recent samples used to judge the model's track record
    min_edge: float = 0.001  # required log-loss improvement over the recent-win-rate baseline
    min_signals: int = 30  # recent would-be trades needed before judging their returns
    learning_rate: float = 0.01  # SGD step size; higher adapts faster but is noisier


@dataclass
class ScanConfig:
    enabled: bool = False  # automatically find coins to trade beyond `symbols`
    max_symbols: int = 10  # extra markets watched at once (on top of `symbols`)
    candidates: int = 40  # markets screened per refresh
    refresh_minutes: float = 60


@dataclass
class SolanaConfig:
    rpc_url: str = "https://api.mainnet-beta.solana.com"  # a private RPC (Helius, QuickNode...) is faster
    jupiter_url: str = "https://lite-api.jup.ag/swap/v1"  # set JUPITER_API_KEY to use api.jup.ag
    slippage_bps: int = 100  # max slippage per swap (1%)
    max_price_impact: float = 0.01  # skip buys that would move the pool price more than this
    min_sol_balance: float = 0.02  # SOL always kept in the wallet for transaction fees
    max_priority_fee_lamports: int = 1_000_000  # cap on priority fee per swap (0.001 SOL)


@dataclass
class Config:
    mode: str = "paper"  # paper | live
    venue: str = "cex"  # cex (a ccxt exchange) | solana (on-chain via Jupiter)
    exchange: str = "binance"
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframe: str = "5m"
    history_bars: int = 1000  # bars fetched each cycle
    fee_rate: float = 0.001
    slippage: float = 0.0005
    starting_cash: float = 10_000.0
    poll_seconds: int = 30
    state_dir: str = "state"
    # symbol -> {"chain": "ethereum", "address": "0x..."} to enable on-chain scam checks
    token_contracts: dict[str, dict[str, str]] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    solana: SolanaConfig = field(default_factory=SolanaConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)

    @property
    def round_trip_cost(self) -> float:
        return 2 * (self.fee_rate + self.slippage)


def _build(cls: type, data: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = known[name].default_factory() if callable(known[name].default_factory) else None
        if is_dataclass(default) and isinstance(value, dict):
            value = _build(type(default), value)
        kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    data = yaml.safe_load(Path(path).read_text()) or {}
    cfg = _build(Config, data)
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    if not cfg.symbols:
        raise ValueError("List at least one symbol; it also sets the quote currency the scanner uses")
    if cfg.venue not in ("cex", "solana"):
        raise ValueError("venue must be 'cex' or 'solana'")
    if cfg.venue == "solana":
        bad = [s for s in cfg.symbols if "/" not in s or s.split("/")[1] not in ("USDC", "USDT")]
        if bad:
            raise ValueError(f"Solana symbols must be quoted in USDC or USDT (e.g. BONK/USDC): {bad}")
    return cfg
