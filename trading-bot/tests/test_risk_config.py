from dataclasses import replace
from pathlib import Path

import pytest

from cryptobot.config import AssetConfig, load_config, validate
from cryptobot.risk import Position, RiskManager

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_is_valid():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.mode == "paper"
    assert {a.symbol for a in cfg.assets} >= {"BTC/USD", "DOGE/USD"}


def test_validation_rejects_over_allocation(cfg):
    cfg.assets[0].allocation_pct = 0.9
    with pytest.raises(ValueError, match="allocation"):
        validate(cfg)


def test_validation_rejects_bad_mode_and_quote(cfg):
    with pytest.raises(ValueError):
        validate(replace(cfg, mode="yolo"))
    with pytest.raises(ValueError, match="quoted"):
        validate(replace(cfg, assets=[AssetConfig("BTC/EUR", "trend", 0.1, 0.05, 0.1)]))


def test_entry_size_respects_caps(cfg):
    asset = cfg.assets[0]  # 50% allocation
    rm = RiskManager(cfg.risk)
    assert rm.entry_size(asset, 1000, 1000, 0, False) == 500
    assert rm.entry_size(asset, 1000, 100, 0, False) == pytest.approx(99.5)  # cash-limited
    assert rm.entry_size(asset, 1000, 1000, 2, False) == 0  # max positions
    assert rm.entry_size(asset, 1000, 1000, 0, True) == 0  # halted
    assert rm.entry_size(asset, 10, 10, 0, False) == 0  # below min order
    rm.cfg.max_order_usd = 50
    assert rm.entry_size(asset, 1000, 1000, 0, False) == 50


def test_exit_rules(cfg):
    asset = cfg.assets[0]  # sl 5%, tp 10%, trail 3%
    rm = RiskManager(cfg.risk)
    pos = Position("BTC/USD", 1, 100.0, 0, 100.0, high_water=100.0)
    assert rm.exit_reason(asset, pos, 99) is None
    assert "stop-loss" in rm.exit_reason(asset, pos, 95)
    assert "take-profit" in rm.exit_reason(asset, pos, 110.5)
    # Trailing stop not armed until +3%.
    pos.high_water = 102
    assert rm.exit_reason(asset, pos, 98.9) is None
    pos.high_water = 106
    assert "trailing" in rm.exit_reason(asset, pos, 102.8)
    assert rm.exit_reason(asset, pos, 103) is None


def test_daily_loss(cfg):
    rm = RiskManager(cfg.risk)
    assert not rm.daily_loss_breached(1000, 960)
    assert rm.daily_loss_breached(1000, 940)
