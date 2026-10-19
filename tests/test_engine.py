import numpy as np

from cryptobot.backtest import synthetic_ohlcv
from cryptobot.config import Config, ModelConfig
from cryptobot.engine import Engine
from cryptobot.exchange import PaperBroker
from cryptobot.model import OnlineModel
from cryptobot.safety import SafetyChecker


class FakeMarket:
    def __init__(self, df):
        self.df = df
        self.cursor = 500

    def fetch_closed_ohlcv(self, symbol, timeframe, limit):
        rows = self.df.iloc[max(0, self.cursor - limit) : self.cursor]
        return rows[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()

    def fetch_ticker(self, symbol):
        price = float(self.df["close"].iloc[self.cursor - 1])
        return {"last": price, "bid": price * 0.9999, "ask": price, "quoteVolume": 1e9}


class AlwaysBuy(OnlineModel):
    def predict_proba(self, X):
        return np.full(len(X), 0.9)

    def is_trustworthy(self):
        return True


def make_engine(tmp_path, model, market):
    cfg = Config(symbols=["SYN/USDT"], state_dir=str(tmp_path), history_bars=400)
    cfg.safety.min_history_bars = 100
    broker = PaperBroker(cfg.starting_cash, cfg.fee_rate, cfg.slippage)
    return Engine(cfg, market, broker, model, SafetyChecker(cfg.safety), clock=lambda: 1_700_000_000.0)


def test_engine_learns_but_does_not_trade_before_trusted(tmp_path):
    market = FakeMarket(synthetic_ohlcv(2000, seed=2))
    engine = make_engine(tmp_path, OnlineModel(ModelConfig()), market)
    for _ in range(20):
        engine.step()
        market.cursor += 1
    assert engine.model.n_seen > 300
    assert not engine.positions
    assert (tmp_path / "model.joblib").exists()


def test_engine_enters_exits_and_persists(tmp_path):
    market = FakeMarket(synthetic_ohlcv(2000, seed=2))
    engine = make_engine(tmp_path, AlwaysBuy(ModelConfig()), market)
    engine.step()
    assert "SYN/USDT" in engine.positions
    pos = engine.positions["SYN/USDT"]
    assert pos.stop < pos.entry_price < pos.take_profit

    reloaded = make_engine(tmp_path, AlwaysBuy(ModelConfig()), market)
    assert reloaded.positions["SYN/USDT"].qty == pos.qty
    assert reloaded.broker.cash == engine.broker.cash

    for _ in range(40):
        market.cursor += 1
        reloaded.step()
    assert (tmp_path / "trades_paper.csv").exists()


def test_engine_blocks_unsafe_market(tmp_path):
    class ThinMarket(FakeMarket):
        def fetch_ticker(self, symbol):
            t = super().fetch_ticker(symbol)
            t["quoteVolume"] = 10.0
            return t

    engine = make_engine(tmp_path, AlwaysBuy(ModelConfig()), ThinMarket(synthetic_ohlcv(2000, seed=2)))
    engine.step()
    assert not engine.positions


def test_run_forever_survives_network_outage(tmp_path, monkeypatch):
    """A dropped connection must not stop the bot, even when the balance lookup fails (live mode)."""

    class OfflineBroker(PaperBroker):
        def free_cash(self):
            raise ConnectionError("Wi-Fi down")

    class OfflineMarket(FakeMarket):
        def fetch_closed_ohlcv(self, symbol, timeframe, limit):
            raise ConnectionError("Wi-Fi down")

    cfg = Config(symbols=["SYN/USDT"], state_dir=str(tmp_path), poll_seconds=1)
    market = OfflineMarket(synthetic_ohlcv(600, seed=2))
    engine = Engine(cfg, market, OfflineBroker(1000, 0, 0), OnlineModel(ModelConfig()), SafetyChecker(cfg.safety))
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt  # stand-in for pressing Ctrl+C

    monkeypatch.setattr("cryptobot.engine.time.sleep", fake_sleep)
    engine.run_forever()
    assert len(sleeps) == 3  # kept cycling through the outage until stopped
