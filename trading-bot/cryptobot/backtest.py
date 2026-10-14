"""Replay history through the exact same engine used for paper/live trading."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .config import Config
from .data import Candle, ReplayFeed
from .engine import Engine
from .exchange import PaperExchange
from .journal import Journal


@dataclass
class BacktestResult:
    start_equity: float
    final_equity: float
    max_drawdown_pct: float
    round_trips: int
    wins: int
    gross_profit: float
    gross_loss: float
    pnl_by_symbol: dict[str, float] = field(default_factory=dict)
    buy_and_hold_pct: dict[str, float] = field(default_factory=dict)
    open_positions: list[str] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        return (self.final_equity / self.start_equity - 1) * 100

    @property
    def win_rate_pct(self) -> float:
        return 100 * self.wins / self.round_trips if self.round_trips else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss else float("inf")

    def report(self) -> str:
        lines = [
            f"Start equity     {self.start_equity:,.2f}",
            f"Final equity     {self.final_equity:,.2f}  ({self.return_pct:+.2f}%)",
            f"Max drawdown     {self.max_drawdown_pct:.2f}%",
            f"Closed trades    {self.round_trips}  (win rate {self.win_rate_pct:.1f}%)",
            f"Profit factor    {self.profit_factor:.2f}",
            "",
            f"{'Symbol':<12}{'Bot PnL':>12}{'Buy & hold':>14}",
        ]
        for sym, bh in self.buy_and_hold_pct.items():
            lines.append(f"{sym:<12}{self.pnl_by_symbol.get(sym, 0.0):>12,.2f}{bh:>13.2f}%")
        if self.open_positions:
            lines.append(f"\nStill open (marked to market): {', '.join(self.open_positions)}")
        return "\n".join(lines)


def run_backtest(cfg: Config, data: dict[str, list[Candle]]) -> BacktestResult:
    cfg = replace(cfg, mode="paper", notify_webhook_url="")
    feed = ReplayFeed(data)
    exchange = PaperExchange(feed, cfg.starting_cash, cfg.fee_rate, cfg.slippage_pct)
    journal = Journal(":memory:")
    engine = Engine(cfg, feed, exchange, journal)
    while feed.advance():
        engine.run_once()

    curve = [e for _, e in journal.equity_curve()]
    peak, max_dd = curve[0] if curve else cfg.starting_cash, 0.0
    for e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)

    sells = [t for t in journal.trades() if t["side"] == "sell"]
    pnl_by_symbol: dict[str, float] = {}
    for t in sells:
        pnl_by_symbol[t["symbol"]] = pnl_by_symbol.get(t["symbol"], 0.0) + t["pnl"]
    return BacktestResult(
        start_equity=cfg.starting_cash,
        final_equity=curve[-1] if curve else cfg.starting_cash,
        max_drawdown_pct=max_dd * 100,
        round_trips=len(sells),
        wins=sum(1 for t in sells if t["pnl"] > 0),
        gross_profit=sum(t["pnl"] for t in sells if t["pnl"] > 0),
        gross_loss=-sum(t["pnl"] for t in sells if t["pnl"] < 0),
        pnl_by_symbol=pnl_by_symbol,
        buy_and_hold_pct={s: (c[-1].close / c[0].close - 1) * 100 for s, c in data.items() if c},
        open_positions=sorted(engine.positions),
    )
