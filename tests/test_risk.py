import pytest

from cryptobot.config import RiskConfig
from cryptobot.risk import DailyLossGuard, DrawdownKillSwitch, check_exit, open_position, size_position


def test_size_risks_fixed_fraction():
    cfg = RiskConfig(risk_per_trade=0.01, max_position_pct=1.0)
    qty = size_position(equity=10_000, cash=10_000, price=100, stop=98, cfg=cfg)
    assert qty == pytest.approx(50)  # 50 units * $2 risk = $100 = 1% of equity


def test_size_capped_by_max_position():
    cfg = RiskConfig(risk_per_trade=0.05, max_position_pct=0.2)
    qty = size_position(10_000, 10_000, price=100, stop=99.9, cfg=cfg)
    assert qty * 100 == pytest.approx(2_000)


def test_invalid_stop_gives_zero():
    assert size_position(10_000, 10_000, 100, 101, RiskConfig()) == 0


def test_exits():
    cfg = RiskConfig(stop_loss_atr=2, take_profit_atr=3)
    pos = open_position("X", 1, 100, atr=1, ts=0, cfg=cfg)
    assert check_exit(pos, 100, 101, 99, 100.5, 1, cfg, 10) is None
    assert check_exit(pos, 100, 103.5, 99, 103, 1, cfg, 10) == (103, "target")
    pos = open_position("X", 1, 100, atr=1, ts=0, cfg=cfg)
    assert check_exit(pos, 97, 97.5, 96, 97, 1, cfg, 10) == (97, "stop_gap")
    pos = open_position("X", 1, 100, atr=1, ts=0, cfg=cfg)
    assert check_exit(pos, 100, 104, 97, 101, 1, cfg, 10) == (98, "stop")
    pos = open_position("X", 1, 100, atr=1, ts=0, cfg=cfg)
    assert check_exit(pos, 100, 101, 99, 100.2, 1, cfg, 1) == (100.2, "time")


def test_trailing_stop_ratchets_up():
    cfg = RiskConfig(stop_loss_atr=2, take_profit_atr=10, trailing_atr=1)
    pos = open_position("X", 1, 100, atr=1, ts=0, cfg=cfg)
    check_exit(pos, 100, 105, 100, 105, 1, cfg, 10)
    assert pos.stop == 104


def test_daily_loss_guard_resets_each_day():
    guard = DailyLossGuard(RiskConfig(max_daily_loss_pct=0.03))
    day = 86_400_000
    assert guard.allows_entry(0, 10_000)
    assert not guard.allows_entry(1000, 9_600)
    assert guard.allows_entry(day, 9_600)


def test_kill_switch_latches():
    ks = DrawdownKillSwitch(RiskConfig(max_drawdown_pct=0.15))
    assert ks.allows_entry(10_000)
    assert ks.allows_entry(12_000)
    assert not ks.allows_entry(10_000)
    assert not ks.allows_entry(12_500)
