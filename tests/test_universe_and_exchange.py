import pytest

from cryptobot.backtest import synthetic_ohlcv
from cryptobot.config import Config, ModelConfig
from cryptobot.engine import Engine
from cryptobot.exchange import LiveBroker, MarketData, PaperBroker
from cryptobot.model import OnlineModel
from cryptobot.safety import DANGER, SafetyChecker, SafetyReport
from cryptobot.universe import Candidate, Universe, exchange_candidates, solana_candidates

TF = 300_000


class FakeExchange:
    """Minimal ccxt stand-in. With coinbase=True it mimics Coinbase's quirks."""

    def __init__(self, coinbase=False, now_ms=1_700_000_000_000 + 3000 * TF):
        self.id = "coinbase" if coinbase else "binance"
        self.now = now_ms
        self.df = synthetic_ohlcv(3000, seed=1)
        self.ohlcv_calls = 0
        self.orders = []
        self.has = {"createMarketBuyOrderWithCost": True}
        self.options = {"createMarketBuyOrderRequiresPrice": True} if coinbase else {}
        self.page = 300 if coinbase else 1000
        self.markets = {}
        listing = {
            "BTC/USD": (5000, 60000),
            "DOGE/USD": (1e8, 0.1),
            "USDT/USD": (1e9, 1.0),
            "BTCUP/USD": (1e6, 5.0),
            "ETH/EUR": (1e4, 3000),
        }
        for sym, (vol, price) in listing.items():
            base, quote = sym.split("/")
            self.markets[sym] = {
                "spot": True,
                "active": True,
                "base": base,
                "quote": quote,
                "limits": {"amount": {"min": 0}},
                "info": {"volume_24h": str(vol), "price": str(price)},
            }

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def milliseconds(self):
        return self.now

    def parse_timeframe(self, tf):
        return 300

    rateLimit = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.ohlcv_calls += 1
        rows = self.df[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()
        rows = [r for r in rows if r[0] >= (since or 0) and r[0] < self.now]
        return rows[: min(limit or self.page, self.page)]

    def fetch_ticker(self, symbol):
        if self.id == "coinbase":  # retail buy/sell prices, no volume
            return {"last": 100.0, "bid": 99.0, "ask": 101.0, "quoteVolume": None}
        return {"last": 100.0, "bid": 99.99, "ask": 100.0, "quoteVolume": 5e7}

    def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[99.99, 1]], "asks": [[100.0, 1]]}

    def fetch_tickers(self):
        if self.id == "coinbase":
            return {s: {"last": 1.0} for s in self.markets}
        return {
            s: {"quoteVolume": float(m["info"]["volume_24h"]) * float(m["info"]["price"])}
            for s, m in self.markets.items()
        }

    def amount_to_precision(self, symbol, qty):
        return f"{qty:.6f}"

    def create_order(self, symbol, type_, side, amount):
        self.orders.append(("amount", side, amount))
        return {"id": "1", "average": 100.0, "filled": amount}

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.orders.append(("cost", "buy", cost))
        return {"id": "2", "average": 100.0, "filled": cost / 100.0}

    def fetch_order(self, order_id, symbol):
        raise AssertionError("not needed")

    def fetch_balance(self):
        return {"free": {"USD": 1000.0, "BTC": 1.0}}


# ----- exchange market data ---------------------------------------------------------------


@pytest.mark.parametrize("coinbase", [False, True])
def test_closed_candles_paginate_and_cache(coinbase):
    ex = FakeExchange(coinbase=coinbase)
    md = MarketData(ex)
    rows = md.fetch_closed_ohlcv("BTC/USD", "5m", 1000)
    assert len(rows) == 1000  # Coinbase needs several 300-candle pages for this
    assert all(r[0] + TF <= ex.now for r in rows)
    calls = ex.ohlcv_calls
    md.fetch_closed_ohlcv("BTC/USD", "5m", 1000)
    assert ex.ohlcv_calls == calls  # nothing new closed yet: no request


def test_coinbase_ticker_uses_order_book_and_fills_volume():
    md = MarketData(FakeExchange(coinbase=True))
    md.fetch_closed_ohlcv("BTC/USD", "5m", 1000)
    ticker = md.fetch_ticker("BTC/USD")
    assert (ticker["bid"], ticker["ask"]) == (99.99, 100.0)
    assert ticker["quoteVolume"] > 0
    report = SafetyChecker(Config().safety).check_market("BTC/USD", ticker, 1000)
    assert "Spread" not in " ".join(report.critical)


def test_coinbase_volume_falls_back_to_market_listing():
    md = MarketData(FakeExchange(coinbase=True))
    assert md.fetch_ticker("BTC/USD")["quoteVolume"] == pytest.approx(5000 * 60000)


def test_coinbase_market_buy_is_sized_by_cost():
    broker = object.__new__(LiveBroker)
    broker.exchange, broker.quote = FakeExchange(coinbase=True), "USD"
    qty, price = broker.buy("BTC/USD", 0.5, 100.0)
    assert broker.exchange.orders == [("cost", "buy", 50.0)]
    assert (qty, price) == (0.5, 100.0)
    broker.sell("BTC/USD", 0.5, 100.0)
    assert broker.exchange.orders[-1] == ("amount", "sell", 0.5)


def test_other_exchanges_buy_by_amount():
    broker = object.__new__(LiveBroker)
    broker.exchange, broker.quote = FakeExchange(), "USD"
    broker.buy("BTC/USD", 0.5, 100.0)
    assert broker.exchange.orders == [("amount", "buy", 0.5)]


# ----- scanner ----------------------------------------------------------------------------


@pytest.mark.parametrize("coinbase", [False, True])
def test_exchange_candidates_rank_by_volume_and_skip_stables_and_leveraged(coinbase):
    cands = exchange_candidates(FakeExchange(coinbase=coinbase), "USD", 10)
    assert [c.symbol for c in cands] == ["BTC/USD", "DOGE/USD"]  # $300M vs $10M


def gecko_pool(mint, name, liquidity=2e6, volume=5e6, created="2024-01-01T00:00:00Z"):
    return {
        "attributes": {
            "name": name,
            "reserve_in_usd": str(liquidity),
            "volume_usd": {"h24": str(volume)},
            "pool_created_at": created,
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}},
    }


def test_solana_candidates_filter_and_name_collisions():
    pools = [
        gecko_pool("Mint1111111111111111111111111111111111111111", "PEPE / SOL"),
        gecko_pool("Mint2222222222222222222222222222222222222222", "PEPE / USDC"),  # same ticker, other token
        gecko_pool("Mint3333333333333333333333333333333333333333", "TINY / SOL", liquidity=5_000),
        gecko_pool("Mint4444444444444444444444444444444444444444", "NEW / SOL", created="2026-10-16T00:00:00Z"),
        gecko_pool("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC / SOL"),
    ]
    cfg = Config(venue="solana", symbols=["SOL/USDC"])
    cands = solana_candidates(cfg, 10, fetch=lambda u, p: {"data": pools}, now=1_760_700_000)
    names = [c.symbol for c in cands]
    assert names[0] == "PEPE/USDC"
    # The second PEPE is renamed only if the first was registered; either way mints stay distinct.
    assert len({c.contract["address"] for c in cands}) == len(cands) == 2


class StubChecker:
    def __init__(self, bad):
        self.bad = bad

    def check_contract(self, chain, address):
        report = SafetyReport(subject=address)
        if address in self.bad:
            report.critical.append("scam")
        return report.finalize()

    def check_market(self, symbol, ticker, history):
        return SafetyReport(subject=symbol).finalize()


def test_universe_keeps_only_safe_coins_and_registers_contracts():
    cfg = Config(venue="solana", symbols=["SOL/USDC"])
    cfg.scan.max_symbols = 5
    found = [
        Candidate("GOOD/USDC", {"chain": "solana", "address": "GoodMint"}),
        Candidate("RUG/USDC", {"chain": "solana", "address": "RugMint"}),
    ]
    clock = [0.0]
    universe = Universe(cfg, StubChecker({"RugMint"}), lambda n: found, clock=lambda: clock[0])
    assert universe.refresh() == ["GOOD/USDC"]
    assert cfg.token_contracts["GOOD/USDC"]["address"] == "GoodMint"
    found.append(Candidate("LATER/USDC", {"chain": "solana", "address": "LaterMint"}))
    assert universe.refresh() == ["GOOD/USDC"]  # not due yet
    clock[0] += 3601
    assert universe.refresh() == ["GOOD/USDC", "LATER/USDC"]


class FakeMarket:
    def __init__(self):
        self.df = synthetic_ohlcv(1000, seed=2)

    def fetch_closed_ohlcv(self, symbol, timeframe, limit):
        return self.df[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()[:500]

    def fetch_ticker(self, symbol):
        price = float(self.df["close"].iloc[499])
        return {"last": price, "bid": price, "ask": price, "quoteVolume": 1e9}


class AlwaysBuy(OnlineModel):
    def predict_proba(self, X):
        import numpy as np

        return np.full(len(X), 0.9)

    def is_trustworthy(self):
        return True


def test_engine_trades_scanner_picks_and_remembers_their_contracts(tmp_path):
    cfg = Config(venue="solana", symbols=["SOL/USDC"], state_dir=str(tmp_path))
    cfg.safety.min_history_bars = 100
    cfg.risk.max_open_positions = 5
    found = [Candidate("MEME/USDC", {"chain": "solana", "address": "MemeMint"})]
    universe = Universe(cfg, StubChecker(set()), lambda n: found)
    broker = PaperBroker(cfg.starting_cash, cfg.fee_rate, cfg.slippage)
    engine = Engine(cfg, FakeMarket(), broker, AlwaysBuy(ModelConfig()), StubChecker(set()), universe=universe)
    engine.step()
    assert "MEME/USDC" in engine.positions

    fresh = Config(venue="solana", symbols=["SOL/USDC"], state_dir=str(tmp_path))
    reloaded = Engine(fresh, FakeMarket(), PaperBroker(0, 0, 0), AlwaysBuy(ModelConfig()), StubChecker(set()))
    assert fresh.token_contracts["MEME/USDC"]["address"] == "MemeMint"
    assert "MEME/USDC" in reloaded.symbols()  # still watched so it can be sold


def test_engine_skips_scam_from_scanner(tmp_path):
    cfg = Config(venue="solana", symbols=["SOL/USDC"], state_dir=str(tmp_path))
    cfg.safety.min_history_bars = 100
    found = [Candidate("RUG/USDC", {"chain": "solana", "address": "RugMint"})]
    checker = StubChecker({"RugMint"})
    engine = Engine(
        cfg, FakeMarket(), PaperBroker(10_000, 0, 0), AlwaysBuy(ModelConfig()), checker,
        universe=Universe(cfg, checker, lambda n: found),
    )  # fmt: skip
    engine.step()
    assert "RUG/USDC" not in engine.positions
    assert DANGER == StubChecker({"RugMint"}).check_contract("solana", "RugMint").verdict
