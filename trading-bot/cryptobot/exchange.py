"""Order execution: a simulated paper exchange and a live ccxt-backed exchange."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .data import Feed


class InsufficientFunds(Exception):
    pass


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str  # "buy" or "sell"
    qty: float  # base units actually received (buy) or sold (sell)
    price: float  # average fill price
    fee: float  # in quote currency
    ts: int


class Exchange(ABC):
    @abstractmethod
    def quote_balance(self) -> float: ...

    @abstractmethod
    def market_buy(self, symbol: str, quote_amount: float) -> Fill: ...

    @abstractmethod
    def market_sell(self, symbol: str, qty: float) -> Fill: ...

    def state(self) -> dict:
        """Serializable state to persist across restarts."""
        return {}

    def restore(self, state: dict) -> None:  # noqa: B027 - optional hook
        """Restore what state() returned."""


class PaperExchange(Exchange):
    """Simulated fills at the feed's last price, with fees and slippage."""

    def __init__(self, feed: Feed, cash: float, fee_rate: float, slippage_pct: float):
        self.feed = feed
        self.cash = cash
        self.fee_rate = fee_rate
        self.slippage_pct = slippage_pct
        self.holdings: dict[str, float] = {}

    def quote_balance(self) -> float:
        return self.cash

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        if quote_amount > self.cash + 1e-9:
            raise InsufficientFunds(f"need {quote_amount:.2f}, have {self.cash:.2f}")
        price = self.feed.last_price(symbol) * (1 + self.slippage_pct)
        fee = quote_amount * self.fee_rate
        qty = (quote_amount - fee) / price
        self.cash -= quote_amount
        self.holdings[symbol] = self.holdings.get(symbol, 0.0) + qty
        return Fill(symbol, "buy", qty, price, fee, self.feed.now())

    def market_sell(self, symbol: str, qty: float) -> Fill:
        qty = min(qty, self.holdings.get(symbol, 0.0))
        if qty <= 0:
            raise InsufficientFunds(f"no {symbol} to sell")
        price = self.feed.last_price(symbol) * (1 - self.slippage_pct)
        gross = qty * price
        fee = gross * self.fee_rate
        self.cash += gross - fee
        self.holdings[symbol] -= qty
        return Fill(symbol, "sell", qty, price, fee, self.feed.now())

    def state(self) -> dict:
        return {"cash": self.cash, "holdings": self.holdings}

    def restore(self, state: dict) -> None:
        self.cash = float(state.get("cash", self.cash))
        self.holdings = {k: float(v) for k, v in state.get("holdings", {}).items()}


def make_ccxt_client(exchange_id: str, with_keys: bool = False):
    import ccxt  # imported lazily so tests/backtests on synthetic data need no network libs

    opts: dict = {"enableRateLimit": True}
    if with_keys:
        key, secret = os.environ.get("CRYPTOBOT_API_KEY"), os.environ.get("CRYPTOBOT_API_SECRET")
        if not key or not secret:
            raise RuntimeError("set CRYPTOBOT_API_KEY and CRYPTOBOT_API_SECRET for live trading")
        opts.update(apiKey=key, secret=secret)
        if pw := os.environ.get("CRYPTOBOT_API_PASSWORD"):
            opts["password"] = pw
    client = getattr(ccxt, exchange_id)(opts)
    client.load_markets()
    return client


class CcxtExchange(Exchange):
    """Real orders on a ccxt exchange. Only ever trades the quantities the bot itself bought."""

    def __init__(self, client, quote: str):
        self.client = client
        self.quote = quote

    def quote_balance(self) -> float:
        return float(self.client.fetch_balance()["free"].get(self.quote, 0.0) or 0.0)

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        price = float(self.client.fetch_ticker(symbol)["last"])
        # Leave headroom for fees and price movement between ticker and fill.
        amount = float(self.client.amount_to_precision(symbol, quote_amount * 0.99 / price))
        order = self.client.create_order(symbol, "market", "buy", amount)
        return self._to_fill(self._settle(order, symbol), symbol, "buy", price)

    def market_sell(self, symbol: str, qty: float) -> Fill:
        base = symbol.split("/")[0]
        free = float(self.client.fetch_balance()["free"].get(base, 0.0) or 0.0)
        amount = float(self.client.amount_to_precision(symbol, min(qty, free)))
        if amount <= 0:
            raise InsufficientFunds(f"no {base} available to sell")
        price = float(self.client.fetch_ticker(symbol)["last"])
        order = self.client.create_order(symbol, "market", "sell", amount)
        return self._to_fill(self._settle(order, symbol), symbol, "sell", price)

    def _settle(self, order: dict, symbol: str, attempts: int = 10) -> dict:
        for _ in range(attempts):
            if order.get("status") == "closed" and order.get("filled"):
                return order
            time.sleep(1)
            order = self.client.fetch_order(order["id"], symbol)
        if not order.get("filled"):
            raise RuntimeError(f"order {order.get('id')} for {symbol} did not fill")
        return order

    def _to_fill(self, order: dict, symbol: str, side: str, price_hint: float) -> Fill:
        filled = float(order["filled"])
        price = float(order.get("average") or order.get("price") or price_hint)
        fee_quote = 0.0
        base = symbol.split("/")[0]
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cost = float(fee.get("cost") or 0.0)
            if fee.get("currency") == self.quote:
                fee_quote += cost
            elif fee.get("currency") == base:
                fee_quote += cost * price
                if side == "buy":
                    filled -= cost  # fee was taken out of the coins we received
        ts = int(order.get("timestamp") or time.time() * 1000)
        return Fill(symbol, side, filled, price, fee_quote, ts)
