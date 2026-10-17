"""Walk-forward backtest that learns online exactly as the live bot would.

The model starts untrained, learns each label only after its outcome window has closed,
and trades only once its out-of-sample predictions beat the baseline. Signals form on a
bar's close and fill at the next bar's open, with fees and slippage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from cryptobot.config import Config
from cryptobot.exchange import PaperBroker
from cryptobot.features import FEATURE_COLUMNS, WARMUP_BARS, compute_features, labels_from_outcomes, trade_outcomes
from cryptobot.model import OnlineModel
from cryptobot.risk import (
    DailyLossGuard,
    DrawdownKillSwitch,
    Position,
    check_exit,
    open_position,
    size_position,
)


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    reason: str
    bars: int

    @property
    def ret(self) -> float:
        """Net return after fees and slippage."""
        return self.pnl / (self.qty * self.entry_price) if self.qty else 0.0


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade]
    equity: pd.Series
    buy_and_hold: float
    model_stats: dict
    metrics: dict = field(default_factory=dict)
    kill_switch_tripped: bool = False

    def report(self) -> str:
        m = self.metrics
        lines = [
            f"Backtest {self.symbol}",
            f"  Return            {m['total_return']:+.2%}   (buy & hold {self.buy_and_hold:+.2%})",
            f"  Max drawdown      {m['max_drawdown']:.2%}",
            f"  Sharpe (annual)   {m['sharpe']:.2f}",
            f"  Trades            {m['trades']}   win rate {m['win_rate']:.1%}"
            f"   profit factor {m['profit_factor']:.2f}",
            f"  Avg trade (net)   {m['avg_trade']:+.3%}   avg bars held {m['avg_bars']:.1f}",
            f"  Exit reasons      {m['exit_reasons']}",
            f"  Kill switch       {'TRIPPED' if self.kill_switch_tripped else 'not triggered'}",
            "  Model (out-of-sample, scored before learning)",
            f"    samples {self.model_stats['samples']}   base win rate {self.model_stats['win_rate']:.1%}",
            f"    edge vs baseline (log-loss) {self.model_stats['edge']:+.4f}",
            (
                f"    precision when signalling   {self.model_stats['precision_at_entry']:.1%}"
                f"  ({self.model_stats['signals_scored']} signals)"
            ),
            f"    avg net return of signals   {self.model_stats['signal_return']:+.3%}",
            f"    trustworthy at end: {self.model_stats['trustworthy']}",
        ]
        return "\n".join(lines)


def _bars_per_year(ts: np.ndarray) -> float:
    step_ms = float(np.median(np.diff(ts))) if len(ts) > 1 else 3_600_000
    return 365 * 86_400_000 / step_ms


def compute_metrics(trades: list[Trade], equity: pd.Series, bars_per_year: float) -> dict:
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else 0.0,
        "max_drawdown": drawdown,
        "sharpe": sharpe,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": sum(wins) / sum(losses) if losses and sum(losses) > 0 else float("inf") if wins else 0.0,
        "avg_trade": float(np.mean([t.ret for t in trades])) if trades else 0.0,
        "avg_bars": float(np.mean([t.bars for t in trades])) if trades else 0.0,
        "exit_reasons": reasons,
    }


def run_backtest(
    df: pd.DataFrame,
    cfg: Config,
    symbol: str = "SYMBOL",
    model: OnlineModel | None = None,
    learn_every: int = 12,
) -> BacktestResult:
    h = cfg.model.horizon_bars
    model = model or OnlineModel(cfg.model)
    feats = compute_features(df)
    X = feats[FEATURE_COLUMNS].to_numpy()
    atr = feats["atr"].to_numpy()
    outcomes = trade_outcomes(
        df, feats["atr"], h, cfg.risk.take_profit_atr, cfg.risk.stop_loss_atr, cfg.round_trip_cost
    ).to_numpy()
    labels = labels_from_outcomes(outcomes)
    ts = df["timestamp"].to_numpy()
    o, hi, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df)
    finite = np.isfinite(X).all(axis=1)

    broker = PaperBroker(cfg.starting_cash, cfg.fee_rate, cfg.slippage)
    guard = DailyLossGuard(cfg.risk)
    kill_switch = DrawdownKillSwitch(cfg.risk)
    trades: list[Trade] = []
    equity = np.full(n, cfg.starting_cash, dtype=float)
    pos: Position | None = None
    entry_cost = 0.0
    pending_entry = pending_exit = False
    learned_upto = 0

    def close_position(t: int, price: float, reason: str) -> None:
        nonlocal pos
        qty, fill = broker.sell(symbol, pos.qty, price)
        proceeds = qty * fill * (1 - cfg.fee_rate)
        trades.append(
            Trade(
                symbol,
                pos.entry_ts,
                int(ts[t]),
                pos.entry_price,
                fill,
                qty,
                proceeds - entry_cost,
                reason,
                pos.bars_held,
            )
        )
        pos = None

    for start in range(WARMUP_BARS, n, learn_every):
        end = min(start + learn_every, n)
        known = max(0, start - h)  # labels of rows < start - h are fully resolved by bar start-1
        if known > learned_upto:
            model.learn(X[learned_upto:known], labels[learned_upto:known], outcomes[learned_upto:known])
            learned_upto = known
        trusted = model.is_trustworthy()
        probs = np.full(end - start, np.nan)
        ok = finite[start:end]
        if ok.any():
            probs[ok] = model.predict_proba(X[start:end][ok])

        for t in range(start, end):
            if pending_exit and pos is not None:
                close_position(t, o[t], "model_exit")
            if pending_entry and pos is None and np.isfinite(atr[t - 1]):
                stop = o[t] - cfg.risk.stop_loss_atr * atr[t - 1]
                qty = size_position(equity[t - 1], broker.free_cash(), o[t], stop, cfg.risk)
                if qty > 0:
                    qty, fill = broker.buy(symbol, qty, o[t])
                    entry_cost = qty * fill * (1 + cfg.fee_rate)
                    pos = open_position(symbol, qty, fill, atr[t - 1], int(ts[t]), cfg.risk)
            pending_entry = pending_exit = False

            if pos is not None:
                result = check_exit(pos, o[t], hi[t], lo[t], c[t], atr[t], cfg.risk, h)
                if result:
                    close_position(t, result[0], result[1])

            equity[t] = broker.free_cash() + (pos.qty * c[t] if pos else 0.0)
            p = probs[t - start]
            if not np.isfinite(p):
                continue
            if pos is None:
                daily_ok = guard.allows_entry(int(ts[t]), equity[t])
                if kill_switch.allows_entry(equity[t]) and daily_ok and trusted and p >= cfg.model.entry_threshold:
                    pending_entry = True
            elif p < cfg.model.exit_threshold:
                pending_exit = True

    if pos is not None:
        close_position(n - 1, c[-1], "end_of_data")
        equity[-1] = broker.free_cash()

    curve = pd.Series(equity[WARMUP_BARS:], index=pd.to_datetime(ts[WARMUP_BARS:], unit="ms"))
    result = BacktestResult(
        symbol=symbol,
        trades=trades,
        equity=curve,
        buy_and_hold=float(c[-1] / c[WARMUP_BARS] - 1),
        model_stats=model.stats(),
    )
    result.kill_switch_tripped = kill_switch.tripped
    result.metrics = compute_metrics(trades, curve, _bars_per_year(ts))
    return result


def synthetic_ohlcv(n: int = 20_000, seed: int = 0, signal: float = 0.0, timeframe_ms: int = 300_000) -> pd.DataFrame:
    """Random-walk prices with regime-switching volatility.

    With signal > 0, a hidden momentum effect makes short-term trends persist, which gives
    a learner something real to find. With signal = 0 the series is unpredictable, which is
    useful for checking that the bot does *not* hallucinate an edge.
    """
    rng = np.random.default_rng(seed)
    vol = 0.002 * np.exp(np.cumsum(rng.normal(0, 0.02, n)).clip(-1.5, 1.5))
    rets = np.zeros(n)
    for i in range(1, n):
        drift = signal * np.tanh(rets[max(0, i - 6) : i].sum() / (vol[i] * 3))
        rets[i] = vol[i] * (drift + rng.normal())
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wiggle = np.abs(rng.normal(0, 0.6, (2, n))) * vol * close
    high = np.maximum(open_, close) + wiggle[0]
    low = np.minimum(open_, close) - wiggle[1]
    volume = rng.lognormal(10, 0.5, n) * (1 + np.abs(rets) / vol)
    ts = 1_700_000_000_000 + np.arange(n, dtype=np.int64) * timeframe_ms
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})
