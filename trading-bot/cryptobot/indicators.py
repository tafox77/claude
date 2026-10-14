"""Technical indicators (pure Python, no numpy needed)."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average, seeded with the first value."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Latest Wilder RSI, or None if there is not enough data."""
    if len(values) <= period:
        return None
    deltas = [b - a for a, b in zip(values, values[1:], strict=False)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)
