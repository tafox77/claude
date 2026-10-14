from dataclasses import replace

import pytest
from conftest import make_candles, zigzag_up

from cryptobot.backtest import run_backtest
from cryptobot.data import ReplayFeed, synthetic_history
from cryptobot.engine import Engine
from cryptobot.exchange import PaperExchange
from cryptobot.journal import Journal

ZIGZAG_UP = zigzag_up()


def btc_only(cfg):
    return replace(cfg, assets=[cfg.assets[0]])


def build(cfg, data, db=":memory:"):
    feed = ReplayFeed(data)
    ex = PaperExchange(feed, cfg.starting_cash, cfg.fee_rate, cfg.slippage_pct)
    journal = Journal(db)
    return feed, ex, journal, Engine(cfg, feed, ex, journal)


def run_all(feed, engine):
    while feed.advance():
        engine.run_once()


def test_replay_feed_never_leaks_future_candles():
    feed = ReplayFeed({"BTC/USD": make_candles([1, 2, 3, 4, 5])})
    seen = []
    while feed.advance():
        cs = feed.candles("BTC/USD", 100)
        assert cs[-1].ts == feed.now()
        seen.append(feed.last_price("BTC/USD"))
    assert seen == [1, 2, 3, 4, 5]


def test_enters_on_signal_and_exits_on_stop_loss(cfg):
    cfg = btc_only(cfg)
    closes = ZIGZAG_UP + [80.0]  # crash > 5% stop
    feed, ex, journal, engine = build(cfg, {"BTC/USD": make_candles(closes)})
    run_all(feed, engine)
    trades = list(reversed(journal.trades()))
    assert trades[0]["side"] == "buy"
    last = trades[-1]
    assert last["side"] == "sell" and "stop-loss" in last["reason"]
    assert last["pnl"] < 0
    assert not engine.positions
    # Cash reconciles with the realized PnL of all round trips.
    realized = sum(t["pnl"] for t in trades if t["side"] == "sell")
    assert ex.cash == pytest.approx(cfg.starting_cash + realized)


def test_cooldown_blocks_immediate_reentry(cfg):
    cfg = btc_only(cfg)
    cfg.assets[0].cooldown_bars = 1000
    cfg.assets[0].stop_loss_pct = 0.001  # stop out on the first dip
    feed, ex, journal, engine = build(cfg, {"BTC/USD": make_candles(ZIGZAG_UP)})
    run_all(feed, engine)
    buys = [t for t in journal.trades() if t["side"] == "buy"]
    assert len(buys) == 1


def test_positions_and_cash_survive_restart(cfg, tmp_path):
    cfg = btc_only(cfg)
    db = str(tmp_path / "bot.db")
    data = {"BTC/USD": make_candles(ZIGZAG_UP)}
    feed, ex, journal, engine = build(cfg, data, db)
    run_all(feed, engine)
    assert engine.positions
    cash = ex.cash

    feed2, ex2, journal2, engine2 = build(cfg, data, db)
    assert engine2.positions.keys() == engine.positions.keys()
    assert ex2.cash == pytest.approx(cash)
    assert ex2.holdings == pytest.approx(ex.holdings)


def test_daily_loss_kill_switch_blocks_new_entries(cfg):
    cfg = btc_only(cfg)
    feed, ex, journal, engine = build(cfg, {"BTC/USD": make_candles(ZIGZAG_UP)})
    feed.advance()
    journal.set("day", feed.now() // 86_400_000)
    journal.set("day_start_equity", 5000.0)  # pretend we already lost a lot today
    engine.run_once()
    run_all(feed, engine)
    assert engine.halted
    assert journal.trades() == []


def test_backtest_on_synthetic_data_is_consistent(cfg):
    data = {a.symbol: synthetic_history(a.symbol, "15m", 1500, seed=1) for a in cfg.assets}
    result = run_backtest(cfg, data)
    assert result.round_trips > 0
    assert 0 <= result.max_drawdown_pct < 100
    assert result.final_equity > 0
    realized = sum(result.pnl_by_symbol.values())
    if not result.open_positions:
        assert result.final_equity == pytest.approx(cfg.starting_cash + realized)
    assert "Final equity" in result.report()
