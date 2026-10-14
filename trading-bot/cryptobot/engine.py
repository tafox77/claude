"""The trading loop: pull candles, evaluate strategies, enforce risk, place orders."""

from __future__ import annotations

import logging
import threading

from .config import AssetConfig, Config
from .data import Feed, timeframe_ms
from .exchange import Exchange, Fill
from .journal import Journal
from .notify import Notifier
from .risk import Position, RiskManager
from .strategies import Action, build_strategy

log = logging.getLogger(__name__)

DAY_MS = 86_400_000


class Engine:
    def __init__(
        self,
        cfg: Config,
        feed: Feed,
        exchange: Exchange,
        journal: Journal,
        notifier: Notifier | None = None,
    ):
        self.cfg = cfg
        self.feed = feed
        self.exchange = exchange
        self.journal = journal
        self.notifier = notifier or Notifier()
        self.risk = RiskManager(cfg.risk)
        self.tf_ms = timeframe_ms(cfg.timeframe)
        self.strategies = {
            a.symbol: build_strategy(a.strategy, cfg.strategy_params(a)) for a in cfg.assets
        }
        self.positions: dict[str, Position] = journal.load_positions()
        self.last_exit_ts: dict[str, int] = journal.get("last_exit_ts", {})
        self.halted = False
        if state := journal.get("exchange_state"):
            exchange.restore(state)
        unmanaged = set(self.positions) - set(self.strategies)
        if unmanaged:
            log.warning("open positions for symbols no longer configured: %s", sorted(unmanaged))

    # ---- public API -------------------------------------------------------------------

    def run_once(self) -> None:
        now = self.feed.now()
        prices = self._prices()
        equity = self.equity(prices)
        self._check_daily_loss(now, equity)
        for asset in self.cfg.assets:
            try:
                self._process(asset, now, equity, prices)
            except Exception:
                log.exception("error processing %s", asset.symbol)
        self.journal.record_equity(now, self.equity(prices), self.exchange.quote_balance())

    def run_forever(self, stop: threading.Event) -> None:
        log.info(
            "bot started in %s mode: %s on %s",
            self.cfg.mode.upper(),
            ", ".join(a.symbol for a in self.cfg.assets),
            self.cfg.timeframe,
        )
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("iteration failed; retrying next poll")
            stop.wait(self.cfg.poll_seconds)
        log.info("bot stopped")

    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.qty * prices.get(s, p.entry_price) for s, p in self.positions.items())
        return self.exchange.quote_balance() + held

    # ---- internals --------------------------------------------------------------------

    def _prices(self) -> dict[str, float]:
        prices = {}
        for symbol in {a.symbol for a in self.cfg.assets} | set(self.positions):
            try:
                prices[symbol] = self.feed.last_price(symbol)
            except Exception as e:
                log.warning("no price for %s: %s", symbol, e)
        return prices

    def _check_daily_loss(self, now: int, equity: float) -> None:
        day = now // DAY_MS
        if self.journal.get("day") != day:
            self.journal.set("day", day)
            self.journal.set("day_start_equity", equity)
            if self.halted:
                log.info("new UTC day: daily-loss halt lifted")
            self.halted = False
        start = self.journal.get("day_start_equity", equity)
        if not self.halted and self.risk.daily_loss_breached(start, equity):
            self.halted = True
            msg = (
                f"KILL SWITCH: equity {equity:.2f} is down more than "
                f"{self.cfg.risk.max_daily_loss_pct:.0%} today (from {start:.2f}). "
                "No new entries until tomorrow (UTC); open positions keep their stops."
            )
            log.warning(msg)
            self.notifier.send(msg)

    def _process(
        self, asset: AssetConfig, now: int, equity: float, prices: dict[str, float]
    ) -> None:
        symbol = asset.symbol
        price = prices.get(symbol)
        if price is None:
            return
        signal = self.strategies[symbol].signal(self.feed.candles(symbol, self.cfg.history_bars))

        pos = self.positions.get(symbol)
        if pos:
            pos.high_water = max(pos.high_water, price)
            reason = self.risk.exit_reason(asset, pos, price)
            if reason is None and signal.action is Action.SELL:
                reason = signal.reason
            if reason:
                self._exit(pos, reason)
            else:
                self.journal.save_position(pos)
            return

        if signal.action is not Action.BUY:
            return
        if now - self.last_exit_ts.get(symbol, -(10**15)) < asset.cooldown_bars * self.tf_ms:
            return
        size = self.risk.entry_size(
            asset, equity, self.exchange.quote_balance(), len(self.positions), self.halted
        )
        if size > 0:
            self._enter(asset, size, signal.reason)

    def _enter(self, asset: AssetConfig, size: float, reason: str) -> None:
        fill = self.exchange.market_buy(asset.symbol, size)
        if fill.qty <= 0:
            raise RuntimeError(f"buy of {asset.symbol} filled zero quantity")
        pos = Position(
            symbol=asset.symbol,
            qty=fill.qty,
            entry_price=fill.price,
            entry_ts=fill.ts,
            cost=fill.qty * fill.price + fill.fee,
            high_water=fill.price,
        )
        self.positions[asset.symbol] = pos
        self.journal.save_position(pos)
        self._record(fill, None, reason)

    def _exit(self, pos: Position, reason: str) -> None:
        fill = self.exchange.market_sell(pos.symbol, pos.qty)
        pnl = fill.qty * fill.price - fill.fee - pos.cost * (fill.qty / pos.qty)
        del self.positions[pos.symbol]
        self.journal.delete_position(pos.symbol)
        self.last_exit_ts[pos.symbol] = fill.ts
        self.journal.set("last_exit_ts", self.last_exit_ts)
        self._record(fill, pnl, reason)

    def _record(self, fill: Fill, pnl: float | None, reason: str) -> None:
        self.journal.record_trade(fill, pnl, reason, self.cfg.mode)
        self.journal.set("exchange_state", self.exchange.state())
        pnl_txt = "" if pnl is None else f" | PnL {pnl:+.2f} {self.cfg.quote}"
        msg = (
            f"[{self.cfg.mode}] {fill.side.upper()} {fill.qty:.8g} {fill.symbol} "
            f"@ {fill.price:.8g}{pnl_txt} | {reason}"
        )
        log.info(msg)
        self.notifier.send(msg)
