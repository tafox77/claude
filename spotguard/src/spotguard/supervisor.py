"""Exit supervisor: runs independently of entries, screening and learning.

For every held position it enforces stop, target, maximum holding time, risk deterioration and
manual flatten. Residual quantities stay managed until resolved. Exits use marketable limit
orders bounded by a reviewed slippage limit; a stale or empty book produces an alert and a retry,
never a fabricated fill. If nothing can be sold within the emergency impact limit the position is
reported as unsellable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from spotguard.config import Policy
from spotguard.execution.orders import Intent, OrderManager
from spotguard.identities import ProductRules
from spotguard.market_data import Quote, quote_usable
from spotguard.risk import RiskEngine
from spotguard.storage import Store

log = logging.getLogger(__name__)


def round_price(price: Decimal, step: Decimal, direction: str) -> Decimal:
    mode = ROUND_DOWN if direction == "down" else ROUND_UP
    return (price / step).to_integral_value(rounding=mode) * step


def round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass
class ExitSupervisor:
    policy: Policy
    store: Store
    orders: OrderManager
    risk: RiskEngine
    quotes: Callable[[str], Quote | None]
    rules: dict[str, ProductRules]
    mono_ns: Callable[[], int]
    wall: Callable[[], datetime]
    deteriorated: Callable[[str], str | None] = lambda key: None  # asset -> reason if risk deteriorated
    alerts: list[str] = field(default_factory=list)
    _last_alert: dict[str, str] = field(default_factory=dict)

    def _alert(self, key: str, level: str, msg: str) -> None:
        if self._last_alert.get(key) != msg:  # avoid flooding the same alert every tick
            self._last_alert[key] = msg
            self.alerts.append(msg)
            self.store.alert(level, msg, self.wall())
            log.warning(msg)

    async def tick(self) -> None:
        now = self.wall()
        if self.risk.state.flatten_requested:
            await self.orders.cancel_all(purpose="entry")
        await self.orders.reconcile_all()
        exiting = {r["asset_key"] for r in self.orders.open_orders() if r["purpose"] != "entry"}
        for key, pos in self.store.positions().items():
            if key in exiting:
                continue
            reason = self._exit_reason(key, pos, now)
            if reason is None:
                continue
            await self._exit(key, pos.qty, reason[0], emergency=reason[1])
        if self.risk.state.flatten_requested and not self.store.positions() and not self.orders.open_orders():
            self.risk.clear_flatten("supervisor", now)
            self._alert("flatten", "info", "flatten complete: no positions or open orders remain")

    def _exit_reason(self, key: str, pos, now: datetime) -> tuple[str, bool] | None:
        if self.risk.state.flatten_requested:
            return "manual flatten", True
        bad = self.deteriorated(key)
        if bad:
            return f"risk deterioration: {bad}", True
        q = self.quotes(key)
        bid = q.best_bid if q else None
        if bid is not None and pos.stop is not None and bid <= pos.stop:
            return f"stop {pos.stop} (bid {bid})", False
        if bid is not None and pos.target is not None and bid >= pos.target:
            return f"target {pos.target} (bid {bid})", False
        held_s = (now - datetime.fromisoformat(pos.opened_at)).total_seconds()
        if held_s >= self.policy.strategy.max_hold_s:
            return f"max hold {self.policy.strategy.max_hold_s}s", False
        return None

    async def _exit(self, key: str, qty: Decimal, reason: str, emergency: bool) -> None:
        q = self.quotes(key)
        ok, why = quote_usable(q, self.mono_ns(), self.wall(), self.policy.market)
        if not ok and not (why.startswith("spread") and q is not None and q.bids):
            self._alert(key, "warning", f"{key}: exit wanted ({reason}) but book unusable ({why}); retrying")
            return
        rules = self.rules.get(key)
        if rules is None:
            self._alert(key, "critical", f"{key}: exit wanted ({reason}) but market rules missing")
            return
        ex = self.policy.execution
        slip = ex.emergency_max_impact_pct if emergency else ex.max_exit_slippage_pct
        # Price off the bid (so a wide spread can't block a stop), but never below the reviewed emergency
        # impact limit measured from the mid (so a collapsed bid side isn't dumped into).
        floor = q.mid * (1 - ex.emergency_max_impact_pct) if q.mid is not None else q.best_bid * (1 - slip)
        limit = round_price(max(q.best_bid * (1 - slip), floor), Decimal(rules.price_increment), "up")
        sellable = sum((size for price, size in q.bids if price >= limit), Decimal(0))
        exit_qty = round_qty(min(qty, sellable), Decimal(rules.base_increment))
        if exit_qty <= 0 or exit_qty < Decimal(rules.base_min_size):
            self._alert(key, "critical", f"{key}: UNSELLABLE at or above {limit} ({reason}); "
                                         f"{qty} still held, will retry")  # fmt: skip
            return
        allowed, why = RiskEngine.check_exit(exit_qty, qty)
        if not allowed:
            self._alert(key, "critical", f"{key}: exit refused: {why}")
            return
        intent = Intent(key, "sell", "emergency_exit" if emergency else "exit", exit_qty, limit, "IOC", reason,
                        self.policy.version)  # fmt: skip
        await self.orders.place(intent)
