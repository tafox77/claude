"""Market data and order execution: a simulated paper broker and a live ccxt broker."""

from __future__ import annotations

import os
import time

import ccxt


def make_exchange(exchange_id: str, api_key: str | None = None, secret: str | None = None):
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"Unknown exchange '{exchange_id}'")
    params = {"enableRateLimit": True}
    if api_key and secret:
        params.update(apiKey=api_key, secret=secret)
    return getattr(ccxt, exchange_id)(params)


class MarketData:
    """Public market data. Only closed candles are returned."""

    def __init__(self, exchange):
        self.exchange = exchange

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        rows = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        now_ms = self.exchange.milliseconds()
        return [r for r in rows if r[0] + tf_ms <= now_ms]

    def fetch_history(self, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None):
        """Paginate OHLCV from `since_ms` (used by backtests)."""
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        until_ms = until_ms or self.exchange.milliseconds()
        rows: list[list[float]] = []
        cursor = since_ms
        while cursor < until_ms:
            batch = self.exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(r for r in batch if r[0] < until_ms)
            cursor = batch[-1][0] + tf_ms
            time.sleep(self.exchange.rateLimit / 1000)
        return rows

    def fetch_ticker(self, symbol: str) -> dict:
        return self.exchange.fetch_ticker(symbol)


class PaperBroker:
    """Simulated spot account with fees and slippage."""

    def __init__(self, cash: float, fee_rate: float, slippage: float):
        self.cash = cash
        self.fee_rate = fee_rate
        self.slippage = slippage

    def buy(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        fill = price * (1 + self.slippage)
        cost = qty * fill * (1 + self.fee_rate)
        if cost > self.cash:
            qty = self.cash / (fill * (1 + self.fee_rate))
            cost = self.cash
        self.cash -= cost
        return qty, fill

    def sell(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        fill = price * (1 - self.slippage)
        self.cash += qty * fill * (1 - self.fee_rate)
        return qty, fill

    def free_cash(self) -> float:
        return self.cash


class LiveBroker:
    """Real orders through ccxt. Keys come from EXCHANGE_API_KEY / EXCHANGE_API_SECRET.

    Stops and targets are enforced by the bot, not resting on the exchange, so the bot
    must keep running while it holds positions.
    """

    def __init__(self, exchange_id: str, quote_currency: str):
        key, secret = os.environ.get("EXCHANGE_API_KEY"), os.environ.get("EXCHANGE_API_SECRET")
        if not key or not secret:
            raise RuntimeError("Set EXCHANGE_API_KEY and EXCHANGE_API_SECRET for live trading")
        self.exchange = make_exchange(exchange_id, key, secret)
        self.exchange.load_markets()
        self.quote = quote_currency

    def _market_order(self, side: str, symbol: str, qty: float) -> tuple[float, float]:
        amount = float(self.exchange.amount_to_precision(symbol, qty))
        market = self.exchange.market(symbol)
        min_amount = (market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            return 0.0, 0.0
        order = self.exchange.create_order(symbol, "market", side, amount)
        order = self.exchange.fetch_order(order["id"], symbol) if not order.get("average") else order
        filled = float(order.get("filled") or amount)
        avg = float(order.get("average") or order.get("price") or 0)
        return filled, avg

    def buy(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        return self._market_order("buy", symbol, qty)

    def sell(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        base = symbol.split("/")[0]
        held = float(self.exchange.fetch_balance().get("free", {}).get(base, 0) or 0)
        return self._market_order("sell", symbol, min(qty, held))

    def free_cash(self) -> float:
        return float(self.exchange.fetch_balance().get("free", {}).get(self.quote, 0) or 0)
