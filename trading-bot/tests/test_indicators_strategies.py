import pytest
from conftest import make_candles, zigzag_up

from cryptobot.indicators import ema, rsi
from cryptobot.strategies import Action, MemeMomentum, TrendFollow, build_strategy


def test_ema_constant_series_is_constant():
    assert ema([5.0] * 10, 3) == [5.0] * 10


def test_ema_reacts_toward_new_values():
    out = ema([1.0, 1.0, 1.0, 10.0], 3)
    assert out[2] == 1.0 and 1.0 < out[3] < 10.0
    assert out[3] == pytest.approx(5.5)  # k = 0.5


def test_rsi_bounds():
    assert rsi(list(range(1, 30)), 14) == 100.0
    assert rsi(list(range(30, 1, -1)), 14) == pytest.approx(0.0)
    assert rsi([1, 2, 3], 14) is None
    assert 0 < rsi([1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2], 14) < 100


def test_trend_buys_in_moderate_uptrend_and_sells_in_downtrend():
    s = TrendFollow(fast=3, slow=6, rsi_max=101)
    assert s.signal(make_candles(zigzag_up())).action is Action.BUY
    down = [200 - i for i in range(40)]
    assert s.signal(make_candles(down)).action is Action.SELL


def test_trend_skips_overbought_entries():
    s = TrendFollow(fast=3, slow=6, rsi_max=70)
    straight_up = [100 + i for i in range(40)]  # RSI 100
    assert s.signal(make_candles(straight_up)).action is Action.HOLD


def test_trend_holds_during_warmup():
    s = TrendFollow(fast=3, slow=6)
    assert s.signal(make_candles([1.0] * 5)).action is Action.HOLD


def _flat_then(last_close, last_volume, n=30):
    closes = [100 + (i % 2) * 0.5 for i in range(n)] + [last_close]
    vols = [100.0] * n + [last_volume]
    return make_candles(closes, vols)


def test_meme_buys_breakout_with_volume():
    s = MemeMomentum(
        lookback=10, volume_mult=2.5, exit_ema=3, rsi_period=5, rsi_entry_max=101, rsi_exit=101
    )
    sig = s.signal(_flat_then(103, 400))
    assert sig.action is Action.BUY and "breakout" in sig.reason


def test_meme_ignores_breakout_without_volume():
    s = MemeMomentum(
        lookback=10, volume_mult=2.5, exit_ema=3, rsi_period=5, rsi_entry_max=101, rsi_exit=101
    )
    assert s.signal(_flat_then(103, 120)).action is Action.HOLD


def test_meme_sells_when_momentum_fades():
    s = MemeMomentum(lookback=10, exit_ema=3, rsi_period=5)
    closes = [100.0] * 20 + [120, 130, 140, 125]
    assert s.signal(make_candles(closes)).action is Action.SELL


def test_build_strategy_rejects_unknown():
    with pytest.raises(ValueError):
        build_strategy("yolo", {})
