import pytest

from cryptobot.config import AssetConfig, Config, RiskConfig
from cryptobot.data import Candle


def zigzag_up(n=41):
    """Uptrend with pullbacks: +1, +1, -0.8, ..."""
    out, v = [], 100.0
    for i in range(n):
        v += -0.8 if i % 3 == 0 else 1.0
        out.append(v)
    return out


def make_candles(closes, volumes=None, start_ts=0, step=900_000):
    volumes = volumes or [100.0] * len(closes)
    out = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        out.append(Candle(start_ts + i * step, prev, max(prev, c), min(prev, c), c, v))
        prev = c
    return out


@pytest.fixture
def cfg():
    return Config(
        assets=[
            AssetConfig(
                "BTC/USD",
                "trend",
                0.5,
                0.05,
                0.10,
                0.03,
                cooldown_bars=2,
                params={"fast": 3, "slow": 6, "rsi_max": 101},
            ),
            AssetConfig(
                "DOGE/USD",
                "meme_momentum",
                0.2,
                0.10,
                0.30,
                0.10,
                params={"lookback": 5, "exit_ema": 3, "rsi_period": 5},
            ),
        ],
        risk=RiskConfig(
            max_open_positions=2, max_daily_loss_pct=0.05, min_order_usd=10, max_order_usd=10_000
        ),
        starting_cash=1000.0,
        fee_rate=0.001,
        slippage_pct=0.0,
    )
