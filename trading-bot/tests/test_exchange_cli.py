import time

import pytest
from conftest import make_candles

from cryptobot.__main__ import main
from cryptobot.data import CcxtFeed, ReplayFeed
from cryptobot.exchange import CcxtExchange, InsufficientFunds, PaperExchange


def test_paper_exchange_applies_fees_and_slippage():
    feed = ReplayFeed({"BTC/USD": make_candles([100.0])})
    feed.advance()
    ex = PaperExchange(feed, cash=1000, fee_rate=0.01, slippage_pct=0.01)
    buy = ex.market_buy("BTC/USD", 101)
    assert buy.price == pytest.approx(101)
    assert buy.fee == pytest.approx(1.01)
    assert buy.qty == pytest.approx((101 - 1.01) / 101)
    assert ex.cash == pytest.approx(899)
    sell = ex.market_sell("BTC/USD", 999)  # clamps to holdings
    assert sell.qty == pytest.approx(buy.qty)
    assert sell.price == pytest.approx(99)
    assert ex.cash < 1000  # round trip costs money
    with pytest.raises(InsufficientFunds):
        ex.market_sell("BTC/USD", 1)
    with pytest.raises(InsufficientFunds):
        ex.market_buy("BTC/USD", 10_000)


class FakeClient:
    def __init__(self):
        self.orders = []
        self.markets = {"BTC/USD": {}}
        self.balance = {"USD": 500.0, "BTC": 0.002}

    def fetch_ohlcv(self, symbol, timeframe, limit=None, since=None):
        now = int(time.time() * 1000)
        start = now - (now % 900_000) - 900_000 * 2
        # two closed candles plus one still forming
        return [[start + i * 900_000, 1, 2, 0.5, 1.5, 10] for i in range(3)]

    def fetch_ticker(self, symbol):
        return {"last": 50_000.0}

    def fetch_balance(self):
        return {"free": dict(self.balance)}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.5f}"

    def create_order(self, symbol, type_, side, amount):
        self.orders.append((symbol, type_, side, amount))
        return {"id": "1", "status": "open", "filled": None}

    def fetch_order(self, order_id, symbol):
        _, _, side, amount = self.orders[-1]
        return {
            "id": order_id,
            "status": "closed",
            "filled": amount,
            "average": 50_100.0,
            "fee": {"currency": "USD", "cost": 0.25},
            "timestamp": 123,
        }


def test_ccxt_feed_drops_forming_candle():
    feed = CcxtFeed(FakeClient(), "15m")
    assert len(feed.candles("BTC/USD", 10)) == 2


def test_ccxt_exchange_orders(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    client = FakeClient()
    ex = CcxtExchange(client, "USD")
    assert ex.quote_balance() == 500
    fill = ex.market_buy("BTC/USD", 100)
    assert client.orders[-1][:3] == ("BTC/USD", "market", "buy")
    assert client.orders[-1][3] == pytest.approx(0.00198)  # 99 USD of headroom / 50k
    assert fill.price == 50_100 and fill.fee == 0.25
    # Never sells more than is actually free in the account.
    ex.market_sell("BTC/USD", 1.0)
    assert client.orders[-1][2:] == ("sell", 0.002)


def test_cli_refuses_live_without_confirmation(tmp_path, capsys):
    cfg = tmp_path / "live.toml"
    cfg.write_text(
        'mode = "live"\n'
        '[[assets]]\nsymbol = "BTC/USD"\nstrategy = "trend"\nallocation_pct = 0.5\n'
        "stop_loss_pct = 0.05\ntake_profit_pct = 0.1\n"
    )
    assert main(["-c", str(cfg), "run"]) == 2
    assert "Refusing to trade real money" in capsys.readouterr().err


def test_cli_synthetic_backtest_and_status(tmp_path, capsys):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    assert main(["-c", str(example), "backtest", "--synthetic", "--days", "5"]) == 0
    assert "Final equity" in capsys.readouterr().out
    cfg = tmp_path / "c.toml"
    cfg.write_text(example.read_text().replace('"cryptobot.db"', f'"{tmp_path / "x.db"}"'))
    assert main(["-c", str(cfg), "status"]) == 0
    assert "Open positions (0)" in capsys.readouterr().out
