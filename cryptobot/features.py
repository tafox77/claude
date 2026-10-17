"""Feature engineering and labels computed from OHLCV bars.

Every feature at bar t uses only data up to and including bar t's close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

FEATURE_COLUMNS = [
    "ret_1",
    "ret_3",
    "ret_12",
    "ret_48",
    "ema_spread",
    "rsi",
    "atr_pct",
    "volatility",
    "vol_ratio",
    "volume_z",
    "bb_pos",
    "range_pct",
    "close_pos",
]

WARMUP_BARS = 60


def to_frame(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    df["timestamp"] = df["timestamp"].astype("int64")
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame aligned with df holding FEATURE_COLUMNS plus raw 'atr'."""
    close, volume = df["close"], df["volume"]
    log_close = np.log(close)
    out = pd.DataFrame(index=df.index)
    for n in (1, 3, 12, 48):
        out[f"ret_{n}"] = log_close.diff(n)
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=48, adjust=False).mean()
    out["ema_spread"] = (ema_fast - ema_slow) / close
    out["rsi"] = (rsi(close) - 50) / 50
    out["atr"] = atr(df)
    out["atr_pct"] = out["atr"] / close
    vol_short = out["ret_1"].rolling(20).std()
    out["volatility"] = vol_short
    out["vol_ratio"] = vol_short / out["ret_1"].rolling(60).std() - 1
    log_vol = np.log1p(volume)
    out["volume_z"] = (log_vol - log_vol.rolling(48).mean()) / log_vol.rolling(48).std()
    sma, std = close.rolling(20).mean(), close.rolling(20).std()
    out["bb_pos"] = (close - sma) / (2 * std)
    bar_range = df["high"] - df["low"]
    out["range_pct"] = bar_range / close
    out["close_pos"] = ((close - df["low"]) / bar_range.replace(0, np.nan)).fillna(0.5) - 0.5
    out = out.replace([np.inf, -np.inf], np.nan)
    out.iloc[:WARMUP_BARS] = np.nan
    return out


def trade_outcomes(
    df: pd.DataFrame,
    atr_series: pd.Series,
    horizon: int,
    take_profit_atr: float,
    stop_loss_atr: float,
    round_trip_cost: float = 0.0,
) -> pd.Series:
    """Net return of a trade entered at bar t's close, using the bot's own exit rules.

    The trade exits at the take-profit, the stop, or the close of bar t + horizon, whichever
    comes first. If stop and target fall inside the same bar the stop is assumed to hit first
    (conservative). Bars whose window hasn't closed yet get NaN, so outcomes never use
    information that isn't available yet. The model's label is `outcome > 0`.
    """
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    a = atr_series.to_numpy()
    n = len(df)
    out = np.full(n, np.nan)
    for t in range(n - horizon):
        if not np.isfinite(a[t]) or a[t] <= 0:
            continue
        entry = close[t]
        target = entry + take_profit_atr * a[t]
        stop = entry - stop_loss_atr * a[t]
        exit_price = close[t + horizon]
        for j in range(t + 1, t + horizon + 1):
            if low[j] <= stop:
                exit_price = stop
                break
            if high[j] >= target:
                exit_price = target
                break
        out[t] = exit_price / entry - 1 - round_trip_cost
    return pd.Series(out, index=df.index)


def labels_from_outcomes(outcomes: pd.Series | np.ndarray) -> np.ndarray:
    r = np.asarray(outcomes, dtype=float)
    return np.where(np.isfinite(r), (r > 0).astype(float), np.nan)
