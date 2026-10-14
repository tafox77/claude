"""Market data: candles, live feeds, historical replay and synthetic data."""

from __future__ import annotations

import bisect
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe] * 1000
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; use one of {sorted(TIMEFRAME_SECONDS)}"
        ) from None


@dataclass(frozen=True)
class Candle:
    ts: int  # candle open time, epoch milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float


class Feed(ABC):
    """Source of closed candles and current prices."""

    @abstractmethod
    def candles(self, symbol: str, limit: int) -> list[Candle]:
        """Most recent *closed* candles, oldest first."""

    @abstractmethod
    def last_price(self, symbol: str) -> float: ...

    @abstractmethod
    def now(self) -> int:
        """Current time in epoch milliseconds."""


class CcxtFeed(Feed):
    """Live market data from any ccxt-supported exchange (no API key needed)."""

    def __init__(self, client, timeframe: str):
        self.client = client
        self.timeframe = timeframe
        self.tf_ms = timeframe_ms(timeframe)

    def candles(self, symbol: str, limit: int) -> list[Candle]:
        rows = self.client.fetch_ohlcv(symbol, self.timeframe, limit=limit + 1)
        candles = [Candle(int(r[0]), *map(float, r[1:6])) for r in rows]
        # The newest candle is usually still forming; only act on closed ones.
        if candles and candles[-1].ts + self.tf_ms > self.now():
            candles.pop()
        return candles[-limit:]

    def last_price(self, symbol: str) -> float:
        return float(self.client.fetch_ticker(symbol)["last"])

    def now(self) -> int:
        return int(time.time() * 1000)


class ReplayFeed(Feed):
    """Replays historical candles one bar at a time, never exposing future data."""

    def __init__(self, data: dict[str, list[Candle]]):
        self.data = {s: sorted(c, key=lambda x: x.ts) for s, c in data.items()}
        self._ts = {s: [c.ts for c in cs] for s, cs in self.data.items()}
        self.timeline = sorted({c.ts for cs in self.data.values() for c in cs})
        self._i = -1

    def advance(self) -> bool:
        self._i += 1
        return self._i < len(self.timeline)

    def now(self) -> int:
        return self.timeline[self._i]

    def _end(self, symbol: str) -> int:
        return bisect.bisect_right(self._ts[symbol], self.now())

    def candles(self, symbol: str, limit: int) -> list[Candle]:
        end = self._end(symbol)
        return self.data[symbol][max(0, end - limit) : end]

    def last_price(self, symbol: str) -> float:
        end = self._end(symbol)
        if end == 0:
            raise LookupError(f"no data for {symbol} yet")
        return self.data[symbol][end - 1].close


def fetch_history(client, symbol: str, timeframe: str, days: float) -> list[Candle]:
    """Download up to `days` of OHLCV history, paging through the exchange API."""
    tf_ms = timeframe_ms(timeframe)
    since = int(time.time() * 1000 - days * 86_400_000)
    out: dict[int, Candle] = {}
    while True:
        rows = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        new = [r for r in rows if int(r[0]) not in out]
        for r in new:
            out[int(r[0])] = Candle(int(r[0]), *map(float, r[1:6]))
        if not new:
            break
        since = int(new[-1][0]) + tf_ms
        if since > time.time() * 1000:
            break
    candles = sorted(out.values(), key=lambda c: c.ts)
    # Drop the still-forming candle.
    if candles and candles[-1].ts + tf_ms > time.time() * 1000:
        candles.pop()
    return candles


def synthetic_history(
    symbol: str,
    timeframe: str = "15m",
    bars: int = 3000,
    seed: int = 0,
    start_price: float | None = None,
    meme: bool | None = None,
) -> list[Candle]:
    """Random-walk candles for offline testing. Meme symbols get higher vol and pumps."""
    rng = random.Random(f"{symbol}:{seed}")
    if meme is None:
        meme = not symbol.upper().startswith(("BTC", "XBT"))
    price = start_price or (60_000.0 if not meme else 0.1)
    vol = 0.012 if meme else 0.004
    tf_ms = timeframe_ms(timeframe)
    ts = 1_700_000_000_000 - (1_700_000_000_000 % tf_ms)
    drift = 0.0
    out = []
    for _ in range(bars):
        if rng.random() < 0.01:  # regime change
            drift = rng.gauss(0, vol / 4)
        ret = drift + rng.gauss(0, vol)
        volume = rng.lognormvariate(10, 0.3)
        if meme and rng.random() < 0.002:  # pump
            ret += rng.uniform(0.03, 0.15)
            volume *= rng.uniform(3, 8)
        o = price
        c = max(o * math.exp(ret), 1e-12)
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        out.append(Candle(ts, o, hi, lo, c, volume))
        price = c
        ts += tf_ms
    return out
