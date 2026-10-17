"""Position sizing, exit rules and the daily loss circuit breaker."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from cryptobot.config import RiskConfig


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    stop: float
    take_profit: float
    entry_ts: int
    bars_held: int = 0
    highest: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def size_position(equity: float, cash: float, price: float, stop: float, cfg: RiskConfig) -> float:
    """Quantity that loses `risk_per_trade` of equity at the stop, capped by size limits."""
    risk_per_unit = price - stop
    if price <= 0 or risk_per_unit <= 0 or equity <= 0:
        return 0.0
    qty = equity * cfg.risk_per_trade / risk_per_unit
    max_notional = min(equity * cfg.max_position_pct, cash * 0.99)
    return max(0.0, min(qty, max_notional / price))


def open_position(symbol: str, qty: float, fill_price: float, atr: float, ts: int, cfg: RiskConfig) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        entry_price=fill_price,
        stop=fill_price - cfg.stop_loss_atr * atr,
        take_profit=fill_price + cfg.take_profit_atr * atr,
        entry_ts=ts,
        highest=fill_price,
    )


def check_exit(
    pos: Position,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    atr: float,
    cfg: RiskConfig,
    max_hold_bars: int,
) -> tuple[float, str] | None:
    """Evaluate one completed bar. Returns (exit_price, reason) or None to keep holding.

    A bar that gaps through a barrier fills at the open. If stop and target both fall in
    one bar, the stop is assumed to hit first.
    """
    pos.bars_held += 1
    if bar_open <= pos.stop:
        return bar_open, "stop_gap"
    if bar_low <= pos.stop:
        return pos.stop, "stop"
    if bar_open >= pos.take_profit:
        return bar_open, "target_gap"
    if bar_high >= pos.take_profit:
        return pos.take_profit, "target"
    pos.highest = max(pos.highest, bar_high)
    if cfg.trailing_atr > 0 and atr > 0:
        pos.stop = max(pos.stop, pos.highest - cfg.trailing_atr * atr)
    if pos.bars_held >= max_hold_bars:
        return bar_close, "time"
    return None


class DailyLossGuard:
    """Blocks new entries once equity falls `max_daily_loss_pct` below the day's start."""

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.day: str | None = None
        self.start_equity = 0.0

    @staticmethod
    def _day(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def allows_entry(self, ts_ms: int, equity: float) -> bool:
        day = self._day(ts_ms)
        if day != self.day:
            self.day, self.start_equity = day, equity
        return equity >= self.start_equity * (1 - self.cfg.max_daily_loss_pct)


class DrawdownKillSwitch:
    """Permanently blocks new entries once equity falls `max_drawdown_pct` below its peak.

    Reset it deliberately (delete the bot's state file) after reviewing what went wrong.
    """

    def __init__(self, cfg: RiskConfig, peak: float = 0.0, tripped: bool = False):
        self.cfg = cfg
        self.peak = peak
        self.tripped = tripped

    def allows_entry(self, equity: float) -> bool:
        self.peak = max(self.peak, equity)
        if equity < self.peak * (1 - self.cfg.max_drawdown_pct):
            self.tripped = True
        return not self.tripped
