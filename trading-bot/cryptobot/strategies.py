"""Entry/exit signal generators. Each strategy only looks at closed candles."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from .data import Candle
from .indicators import ema, rsi


class Action(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str = ""


HOLD = Signal(Action.HOLD)


class Strategy(ABC):
    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of candles before signals are meaningful."""

    @abstractmethod
    def signal(self, candles: list[Candle]) -> Signal: ...


class TrendFollow(Strategy):
    """For BTC: hold while the fast EMA is above the slow EMA, skip overbought entries."""

    def __init__(self, fast: int = 12, slow: int = 26, rsi_period: int = 14, rsi_max: float = 70):
        if fast >= slow:
            raise ValueError("trend: fast must be < slow")
        self.fast, self.slow, self.rsi_period, self.rsi_max = fast, slow, rsi_period, rsi_max

    @property
    def warmup(self) -> int:
        return max(self.slow * 3, self.rsi_period + 1)

    def signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD
        closes = [c.close for c in candles]
        fast = ema(closes, self.fast)[-1]
        slow = ema(closes, self.slow)[-1]
        r = rsi(closes, self.rsi_period)
        if fast < slow:
            return Signal(Action.SELL, "trend: fast EMA crossed below slow EMA")
        if closes[-1] > slow and r is not None and r < self.rsi_max:
            return Signal(Action.BUY, f"trend: uptrend (RSI {r:.0f})")
        return HOLD


class MemeMomentum(Strategy):
    """For meme coins: buy volume-confirmed breakouts, exit fast when momentum fades."""

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 2.5,
        exit_ema: int = 9,
        rsi_period: int = 14,
        rsi_entry_max: float = 85,
        rsi_exit: float = 90,
    ):
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.exit_ema = exit_ema
        self.rsi_period = rsi_period
        self.rsi_entry_max = rsi_entry_max
        self.rsi_exit = rsi_exit

    @property
    def warmup(self) -> int:
        return max(self.lookback + 1, self.exit_ema * 3, self.rsi_period + 1)

    def signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD
        closes = [c.close for c in candles]
        last = candles[-1]
        prior = candles[-self.lookback - 1 : -1]
        r = rsi(closes, self.rsi_period)
        if r is not None and r >= self.rsi_exit:
            return Signal(Action.SELL, f"meme: RSI blow-off ({r:.0f})")
        if last.close < ema(closes, self.exit_ema)[-1]:
            return Signal(Action.SELL, "meme: momentum faded (close below EMA)")
        avg_vol = sum(c.volume for c in prior) / len(prior)
        breakout = last.close > max(c.high for c in prior)
        if (
            breakout
            and avg_vol > 0
            and last.volume >= self.volume_mult * avg_vol
            and r is not None
            and r < self.rsi_entry_max
        ):
            return Signal(
                Action.BUY,
                f"meme: {self.lookback}-bar breakout on {last.volume / avg_vol:.1f}x volume",
            )
        return HOLD


STRATEGIES: dict[str, type[Strategy]] = {
    "trend": TrendFollow,
    "meme_momentum": MemeMomentum,
}


def build_strategy(name: str, params: dict) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; choose from {sorted(STRATEGIES)}") from None
    return cls(**params)
