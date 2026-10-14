"""Position sizing, stop-loss / take-profit / trailing stops and the daily-loss kill switch."""

from __future__ import annotations

from dataclasses import dataclass

from .config import AssetConfig, RiskConfig


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_ts: int
    cost: float  # quote currency spent, including fees
    high_water: float


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def entry_size(
        self, asset: AssetConfig, equity: float, cash: float, open_positions: int, halted: bool
    ) -> float:
        """Quote amount to spend on a new position, or 0 to skip."""
        if halted or open_positions >= self.cfg.max_open_positions:
            return 0.0
        size = min(equity * asset.allocation_pct, cash * 0.995, self.cfg.max_order_usd)
        return size if size >= self.cfg.min_order_usd else 0.0

    def exit_reason(self, asset: AssetConfig, pos: Position, price: float) -> str | None:
        if price <= pos.entry_price * (1 - asset.stop_loss_pct):
            return f"stop-loss ({asset.stop_loss_pct:.0%})"
        if price >= pos.entry_price * (1 + asset.take_profit_pct):
            return f"take-profit ({asset.take_profit_pct:.0%})"
        trail = asset.trailing_stop_pct
        # The trailing stop arms once price has risen by the trail distance, so it
        # never sits below breakeven-ish and never replaces the hard stop-loss.
        if (
            trail
            and pos.high_water >= pos.entry_price * (1 + trail)
            and price <= pos.high_water * (1 - trail)
        ):
            return f"trailing-stop ({trail:.0%} off high)"
        return None

    def daily_loss_breached(self, day_start_equity: float, equity: float) -> bool:
        return equity < day_start_equity * (1 - self.cfg.max_daily_loss_pct)
