from cryptobot.backtest import run_backtest, synthetic_ohlcv
from cryptobot.config import Config


def test_noise_market_does_not_lose_much():
    """With nothing to learn, the bot should mostly sit out instead of churning fees."""
    for seed in (1, 2):
        result = run_backtest(synthetic_ohlcv(12_000, seed=seed, signal=0.0), Config())
        assert result.metrics["total_return"] > -0.03, result.report()


def test_finds_edge_when_one_exists():
    result = run_backtest(synthetic_ohlcv(20_000, seed=4, signal=0.4), Config())
    assert result.metrics["trades"] > 50
    assert result.metrics["total_return"] > 0, result.report()


def test_accounting_is_consistent():
    cfg = Config()
    result = run_backtest(synthetic_ohlcv(12_000, seed=4, signal=0.4), cfg)
    pnl = sum(t.pnl for t in result.trades)
    assert abs(cfg.starting_cash + pnl - result.equity.iloc[-1]) < 1e-6
