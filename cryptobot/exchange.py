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


# Exchanges whose public ticker prices aren't the real order book (Coinbase's include a
# retail markup), so bid/ask come from the order book instead.
ORDER_BOOK_PRICED = {"coinbase", "coinbaseadvanced"}


def market_quote_volume(market: dict) -> float:
    """24h quote volume from the exchange's market listing (Coinbase publishes it there)."""
    info = market.get("info") or {}
    try:
        return float(info.get("volume_24h") or 0) * float(info.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


class MarketData:
    """Public market data. Only closed candles are returned.

    Candles are cached and only new ones are fetched each cycle, which keeps request counts
    low and works on exchanges that return a few hundred candles per request (Coinbase: 300).
    """

    def __init__(self, exchange):
        self.exchange = exchange
        self._candles: dict[tuple[str, str], list[list[float]]] = {}

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        now_ms = self.exchange.milliseconds()
        key = (symbol, timeframe)
        cached = self._candles.get(key, [])
        if cached and now_ms < cached[-1][0] + 2 * tf_ms:
            return cached  # no new candle has closed yet
        since = cached[-1][0] + tf_ms if cached else now_ms - (limit + 1) * tf_ms
        merged = {r[0]: r for r in cached}
        merged.update((r[0], r) for r in self.fetch_history(symbol, timeframe, since, now_ms))
        rows = [merged[t] for t in sorted(merged) if t + tf_ms <= now_ms][-limit:]
        self._candles[key] = rows
        return rows

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
        ticker = dict(self.exchange.fetch_ticker(symbol))
        if self.exchange.id in ORDER_BOOK_PRICED or not (ticker.get("bid") and ticker.get("ask")):
            book = self.exchange.fetch_order_book(symbol, limit=5)
            if book.get("bids") and book.get("asks"):
                ticker["bid"], ticker["ask"] = book["bids"][0][0], book["asks"][0][0]
        if not ticker.get("quoteVolume"):
            ticker["quoteVolume"] = self._quote_volume_fallback(symbol)
        return ticker

    def _quote_volume_fallback(self, symbol: str) -> float:
        day_ago = self.exchange.milliseconds() - 86_400_000
        for (sym, _), rows in self._candles.items():
            if sym == symbol and rows and rows[0][0] <= day_ago:
                return sum(r[4] * r[5] for r in rows if r[0] >= day_ago)
        if not self.exchange.markets:
            self.exchange.load_markets()
        return market_quote_volume(self.exchange.market(symbol))


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
        # Coinbase secrets are multi-line PEM keys; allow them to be pasted with literal \n.
        secret = secret.replace("\\n", "\n")
        self.exchange = make_exchange(exchange_id, key, secret)
        self.exchange.load_markets()
        self.quote = quote_currency

    def _market_order(self, side: str, symbol: str, qty: float, price: float) -> tuple[float, float]:
        amount = float(self.exchange.amount_to_precision(symbol, qty))
        market = self.exchange.market(symbol)
        min_amount = (market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            return 0.0, 0.0
        buy_by_cost = (
            side == "buy"
            and self.exchange.has.get("createMarketBuyOrderWithCost")
            and self.exchange.options.get("createMarketBuyOrderRequiresPrice")
        )
        if buy_by_cost:  # e.g. Coinbase: market buys are sized in quote currency
            order = self.exchange.create_market_buy_order_with_cost(symbol, amount * price)
        else:
            order = self.exchange.create_order(symbol, "market", side, amount)
        if not order.get("average"):
            time.sleep(1)
            order = self.exchange.fetch_order(order["id"], symbol)
        filled = float(order.get("filled") or amount)
        avg = float(order.get("average") or order.get("price") or price)
        return filled, avg

    def buy(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        return self._market_order("buy", symbol, qty, price)

    def sell(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        base = symbol.split("/")[0]
        held = float(self.exchange.fetch_balance().get("free", {}).get(base, 0) or 0)
        return self._market_order("sell", symbol, min(qty, held), price)

    def free_cash(self) -> float:
        return float(self.exchange.fetch_balance().get("free", {}).get(self.quote, 0) or 0)
