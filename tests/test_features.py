import numpy as np
import pandas as pd
import pytest

from cryptobot.backtest import synthetic_ohlcv
from cryptobot.features import FEATURE_COLUMNS, compute_features, labels_from_outcomes, trade_outcomes


def test_features_do_not_look_ahead():
    df = synthetic_ohlcv(600, seed=3)
    full = compute_features(df)
    cut = 400
    partial = compute_features(df.iloc[: cut + 1])
    np.testing.assert_allclose(
        full[FEATURE_COLUMNS].iloc[cut].to_numpy(), partial[FEATURE_COLUMNS].iloc[cut].to_numpy()
    )


def test_outcomes_unresolved_at_end_and_unchanged_by_future_bars():
    df = synthetic_ohlcv(500, seed=1)
    atr = compute_features(df)["atr"]
    out = trade_outcomes(df, atr, horizon=12, take_profit_atr=3, stop_loss_atr=2)
    assert out.iloc[-12:].isna().all()
    shorter = trade_outcomes(df.iloc[:300], atr.iloc[:300], 12, 3, 2)
    np.testing.assert_allclose(out.iloc[:288].to_numpy(), shorter.iloc[:288].to_numpy())


def _bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(timestamp=range(len(rows)), volume=1.0)


def test_outcome_hits_target():
    df = _bars([[100, 100, 100, 100], [100, 104, 99.5, 103], [103, 103, 103, 103]])
    out = trade_outcomes(df, pd.Series([1.0] * 3), horizon=2, take_profit_atr=3, stop_loss_atr=2)
    assert out.iloc[0] == pytest.approx(0.03)


def test_outcome_assumes_stop_first_when_both_hit_in_one_bar():
    df = _bars([[100, 100, 100, 100], [100, 104, 97, 101], [101, 101, 101, 101]])
    out = trade_outcomes(df, pd.Series([1.0] * 3), horizon=2, take_profit_atr=3, stop_loss_atr=2)
    assert out.iloc[0] == pytest.approx(-0.02)
    assert labels_from_outcomes(out)[0] == 0.0


def test_costs_turn_tiny_wins_into_losses():
    df = _bars([[100, 100, 100, 100], [100, 100.1, 99.9, 100.1], [100.1, 100.1, 100.1, 100.1]])
    out = trade_outcomes(df, pd.Series([1.0] * 3), horizon=1, take_profit_atr=3, stop_loss_atr=2, round_trip_cost=0.003)
    assert labels_from_outcomes(out)[0] == 0.0
