"""The trading loop shared by paper and live mode.

Each cycle, for every symbol: learn from bars whose outcome is now known, enforce stops and
targets against the live price, and on each newly closed bar consider exits and entries.
New entries need a passing safety check, a trusted model and room under every risk limit.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

import numpy as np

from cryptobot.config import Config
from cryptobot.features import (
    FEATURE_COLUMNS,
    WARMUP_BARS,
    compute_features,
    labels_from_outcomes,
    to_frame,
    trade_outcomes,
)
from cryptobot.model import OnlineModel
from cryptobot.risk import DailyLossGuard, DrawdownKillSwitch, Position, check_exit, open_position, size_position
from cryptobot.safety import SafetyChecker, resolve_contract

log = logging.getLogger("cryptobot")

TRADE_FIELDS = ["symbol", "entry_ts", "exit_ts", "entry_price", "exit_price", "qty", "pnl", "return", "reason", "bars"]


class Engine:
    def __init__(
        self, cfg: Config, market, broker, model: OnlineModel, checker: SafetyChecker, clock=time.time, universe=None
    ):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.model = model
        self.checker = checker
        self.clock = clock
        self.state_dir = Path(cfg.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.positions: dict[str, Position] = {}
        self.entry_costs: dict[str, float] = {}
        self.last_learned_ts: dict[str, int] = {}
        self.last_bar_ts: dict[str, int] = {}
        self.prices: dict[str, float] = {}
        self.daily_guard = DailyLossGuard(cfg.risk)
        self.kill_switch = DrawdownKillSwitch(cfg.risk)
        self.universe = universe
        self._load_state()

    # ----- persistence -------------------------------------------------------------
    @property
    def model_path(self) -> Path:
        return self.state_dir / "model.joblib"

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"state_{self.cfg.mode}.json"

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        self.positions = {s: Position(**p) for s, p in data.get("positions", {}).items()}
        self.entry_costs = data.get("entry_costs", {})
        self.last_learned_ts = data.get("last_learned_ts", {})
        self.last_bar_ts = data.get("last_bar_ts", {})
        self.prices = data.get("prices", {})
        # Restore token contracts of coins the scanner found, so held positions stay tradable.
        for symbol, contract in data.get("contracts", {}).items():
            self.cfg.token_contracts.setdefault(symbol, contract)
        ks = data.get("kill_switch", {})
        self.kill_switch = DrawdownKillSwitch(self.cfg.risk, ks.get("peak", 0.0), ks.get("tripped", False))
        if "paper_cash" in data and hasattr(self.broker, "cash"):
            self.broker.cash = data["paper_cash"]

    def save_state(self) -> None:
        data = {
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "entry_costs": self.entry_costs,
            "last_learned_ts": self.last_learned_ts,
            "last_bar_ts": self.last_bar_ts,
            "prices": self.prices,
            "kill_switch": {"peak": self.kill_switch.peak, "tripped": self.kill_switch.tripped},
            "contracts": {s: self.cfg.token_contracts[s] for s in self.positions if s in self.cfg.token_contracts},
        }
        if hasattr(self.broker, "cash"):
            data["paper_cash"] = self.broker.cash
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_path)
        self.model.save(self.model_path)

    def _journal(self, row: dict) -> None:
        path = self.state_dir / f"trades_{self.cfg.mode}.csv"
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(row)

    # ----- trading -----------------------------------------------------------------
    def equity(self) -> float:
        held = sum(p.qty * self.prices.get(s, p.entry_price) for s, p in self.positions.items())
        return self.broker.free_cash() + held

    def run_forever(self) -> None:
        log.info("Starting %s trading on %s: %s", self.cfg.mode, self.cfg.exchange, ", ".join(self.cfg.symbols))
        try:
            while True:
                started = self.clock()
                self.step()
                time.sleep(max(1.0, self.cfg.poll_seconds - (self.clock() - started)))
        except KeyboardInterrupt:
            log.info("Stopping; open positions are kept in %s", self.state_path)
        finally:
            self.save_state()

    def symbols(self) -> list[str]:
        """Configured symbols, scanner picks, and anything still held (never abandon a position)."""
        found = []
        if self.universe is not None:
            try:
                found = self.universe.refresh()
            except Exception:
                log.exception("Scanner refresh failed; keeping the previous list")
                found = self.universe.symbols
        return list(dict.fromkeys([*self.cfg.symbols, *found, *self.positions]))

    def step(self) -> None:
        for symbol in self.symbols():
            try:
                self._step_symbol(symbol)
            except Exception:  # one bad symbol must not stop the others
                log.exception("Error processing %s", symbol)
        self.save_state()
        stats = self.model.stats()
        log.info(
            "equity %.2f | open %d | model samples %d edge %+.4f signal return %+.3f%% trusted %s%s",
            self.equity(),
            len(self.positions),
            stats["samples"],
            stats["edge"],
            100 * stats["signal_return"],
            stats["trustworthy"],
            " | KILL SWITCH TRIPPED" if self.kill_switch.tripped else "",
        )

    def _step_symbol(self, symbol: str) -> None:
        cfg = self.cfg
        h = cfg.model.horizon_bars
        df = to_frame(self.market.fetch_closed_ohlcv(symbol, cfg.timeframe, cfg.history_bars))
        if len(df) < WARMUP_BARS + h + 2:
            log.warning("%s: only %d bars of history, skipping", symbol, len(df))
            return
        feats = compute_features(df)
        X = feats[FEATURE_COLUMNS].to_numpy()
        ts = df["timestamp"].to_numpy()
        outcomes = trade_outcomes(
            df, feats["atr"], h, cfg.risk.take_profit_atr, cfg.risk.stop_loss_atr, cfg.round_trip_cost
        ).to_numpy()
        self._learn(symbol, X, ts, outcomes)

        ticker = self.market.fetch_ticker(symbol)
        price = float(ticker.get("last") or df["close"].iloc[-1])
        self.prices[symbol] = price
        bar = df.iloc[-1]
        bar_ts = int(bar["timestamp"])
        new_bar = bar_ts > self.last_bar_ts.get(symbol, 0)
        prob = float(self.model.predict_proba(X[-1:])[0]) if np.isfinite(X[-1]).all() else float("nan")

        pos = self.positions.get(symbol)
        if pos is not None:
            reason = None
            if price <= pos.stop:
                reason = "stop"
            elif price >= pos.take_profit:
                reason = "target"
            elif new_bar and bar_ts > pos.entry_ts:
                hit = check_exit(
                    pos, bar["open"], bar["high"], bar["low"], bar["close"], float(feats["atr"].iloc[-1]), cfg.risk, h
                )
                if hit:
                    reason = hit[1]
                elif np.isfinite(prob) and prob < cfg.model.exit_threshold:
                    reason = "model_exit"
            if reason:
                self._exit(symbol, price, reason, bar_ts)
        elif new_bar:
            self._consider_entry(symbol, ticker, price, prob, float(feats["atr"].iloc[-1]), bar_ts, len(df))
        self.last_bar_ts[symbol] = bar_ts

    def _learn(self, symbol: str, X: np.ndarray, ts: np.ndarray, outcomes: np.ndarray) -> None:
        resolved = np.isfinite(outcomes)
        new = resolved & (ts > self.last_learned_ts.get(symbol, -1))
        if new.any():
            self.model.learn(X[new], labels_from_outcomes(outcomes[new]), outcomes[new])
            self.last_learned_ts[symbol] = int(ts[resolved].max())

    def _consider_entry(self, symbol, ticker, price, prob, atr, bar_ts, history) -> None:
        cfg = self.cfg
        if not np.isfinite(prob) or prob < cfg.model.entry_threshold:
            return
        if not self.model.is_trustworthy():
            log.info("%s: signal %.2f ignored, model has not proven an edge yet", symbol, prob)
            return
        if len(self.positions) >= cfg.risk.max_open_positions:
            return
        equity = self.equity()
        if not self.kill_switch.allows_entry(equity):
            log.warning("Kill switch tripped (drawdown > %.0f%%); no new trades", 100 * cfg.risk.max_drawdown_pct)
            return
        if not self.daily_guard.allows_entry(int(self.clock() * 1000), equity):
            log.warning("Daily loss limit reached; no new trades today")
            return
        report = self.checker.check_market(symbol, ticker, history)
        contract = resolve_contract(cfg, symbol)
        if report.tradable and contract:
            report = self.checker.check_contract(contract["chain"], contract["address"])
        if not report.tradable:
            log.warning("%s blocked by safety check:\n%s", symbol, report.summary())
            return
        stop = price - cfg.risk.stop_loss_atr * atr
        qty = size_position(equity, self.broker.free_cash(), price, stop, cfg.risk)
        if qty <= 0:
            return
        filled, fill = self.broker.buy(symbol, qty, price)
        if filled <= 0:
            log.warning("%s: order not filled (below exchange minimum?)", symbol)
            return
        self.positions[symbol] = open_position(symbol, filled, fill, atr, bar_ts, cfg.risk)
        self.entry_costs[symbol] = filled * fill * (1 + cfg.fee_rate)
        p = self.positions[symbol]
        log.info(
            "BUY %s qty %.6f @ %.6f (p=%.2f) stop %.6f target %.6f", symbol, filled, fill, prob, p.stop, p.take_profit
        )

    def _exit(self, symbol: str, price: float, reason: str, bar_ts: int) -> None:
        pos = self.positions[symbol]
        qty, fill = self.broker.sell(symbol, pos.qty, price)
        if qty <= 0:
            log.error("%s: exit order failed; still holding", symbol)
            return
        pnl = qty * fill * (1 - self.cfg.fee_rate) - self.entry_costs.pop(symbol, pos.qty * pos.entry_price)
        del self.positions[symbol]
        self._journal(
            {
                "symbol": symbol,
                "entry_ts": pos.entry_ts,
                "exit_ts": bar_ts,
                "entry_price": pos.entry_price,
                "exit_price": fill,
                "qty": qty,
                "pnl": round(pnl, 8),
                "return": round(fill / pos.entry_price - 1, 6),
                "reason": reason,
                "bars": pos.bars_held,
            }
        )
        log.info("SELL %s qty %.6f @ %.6f (%s) pnl %+.2f", symbol, qty, fill, reason, pnl)
