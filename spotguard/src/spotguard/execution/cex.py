"""Live Coinbase Advanced Trade adapter via ccxt — DISABLED BY DEFAULT and UNTESTED against the venue.

Status: code path only. It has been exercised against a fake ccxt client in unit tests, never
against Coinbase. Coinbase has no sandbox in ccxt (features.default.sandbox = False), so there is
no testnet path; Coinbase's order *preview* endpoint can validate an order without placing it and
is used by the acceptance gate.

Enabling requires ALL of:
  * policy mode = "live"
  * environment SPOTGUARD_LIVE_ENABLE=I_ACCEPT_REAL_MONEY_RISK
  * an approval JSON whose venue, markets, capital, limits (policy hash) and model hash match the
    running configuration, signed off by a named person, listing passed acceptance gates
  * API key permissions: trade only, withdrawals disabled, IP-restricted where supported
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from spotguard.config import Policy
from spotguard.execution.base import AdapterError, OrderRequest, VenueFill, VenueOrderView, VenueStatus

LIVE_ENV_FLAG = "SPOTGUARD_LIVE_ENABLE"
LIVE_ENV_VALUE = "I_ACCEPT_REAL_MONEY_RISK"
REQUIRED_GATES = (
    "paper_30_days",
    "paper_200_closed_trades",
    "acceptance_test_suite",
    "order_preview_roundtrip",
    "balance_reconciliation",
    "independent_review",
)


class LiveDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveApproval:
    venue: str
    markets: tuple[str, ...]
    max_capital: Decimal
    policy_version: str
    model_id: str | None
    approved_by: str
    approved_at: str
    gates_passed: tuple[str, ...]

    @classmethod
    def load(cls, path: str | Path) -> LiveApproval:
        raw = json.loads(Path(path).read_text())
        return cls(
            raw["venue"],
            tuple(raw["markets"]),
            Decimal(str(raw["max_capital"])),
            raw["policy_version"],
            raw.get("model_id"),
            raw["approved_by"],
            raw["approved_at"],
            tuple(raw["gates_passed"]),
        )

    def verify(self, policy: Policy, model_id: str | None, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        problems = []
        if policy.mode != "live":
            problems.append("policy mode is not 'live'")
        if env.get(LIVE_ENV_FLAG) != LIVE_ENV_VALUE:
            problems.append(f"{LIVE_ENV_FLAG} not set to the required value")
        if self.venue != "coinbase":
            problems.append(f"approved venue {self.venue!r} is not supported")
        allowed = {m.market_id for m in policy.cex_allowlist}
        if not set(self.markets) or not set(self.markets) <= allowed:
            problems.append("approved markets must be a non-empty subset of the policy allowlist")
        if self.policy_version != policy.version:
            problems.append(f"approval is for policy {self.policy_version}, running {policy.version}")
        if self.model_id != model_id:
            problems.append(f"approval is for model {self.model_id}, running {model_id}")
        if self.max_capital <= 0 or self.max_capital > policy.risk.virtual_equity:
            problems.append("max_capital must be positive and within the policy's equity")
        if not self.approved_by.strip():
            problems.append("approval has no named approver")
        missing = [g for g in REQUIRED_GATES if g not in self.gates_passed]
        if missing:
            problems.append(f"acceptance gates not passed: {missing}")
        if problems:
            raise LiveDisabled("live trading refused: " + "; ".join(problems))


class CoinbaseLiveAdapter:
    label = "LIVE"

    def __init__(self, policy: Policy, approval: LiveApproval, model_id: str | None, exchange=None):
        approval.verify(policy, model_id)
        if exchange is None:
            import ccxt  # optional dependency: pip install spotguard[live]

            key, secret = os.environ.get("COINBASE_API_KEY_NAME"), os.environ.get("COINBASE_API_PRIVATE_KEY")
            if not key or not secret:
                raise LiveDisabled("COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set")
            exchange = ccxt.coinbase({"apiKey": key, "secret": secret.replace("\\n", "\n"), "enableRateLimit": True})
        self.ex = exchange
        self.approval = approval
        self.symbols = {f"cex:coinbase:{m}": m.replace("-", "/") for m in approval.markets}

    async def _call(self, fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # network/venue errors: outcome unknown, caller reconciles
            raise AdapterError(f"{type(exc).__name__}: {exc}") from exc

    def _view(self, order: dict) -> VenueOrderView:
        status = {"open": VenueStatus.OPEN, "closed": VenueStatus.FILLED, "canceled": VenueStatus.CANCELED,
                  "expired": VenueStatus.CANCELED, "rejected": VenueStatus.REJECTED}.get(order.get("status"),
                                                                                       VenueStatus.OPEN)  # fmt: skip
        return VenueOrderView(order.get("clientOrderId") or "", order.get("id"), status, [], order.get("status") or "")

    async def _fills(self, symbol: str, order_id: str) -> list[VenueFill]:
        trades = await self._call(self.ex.fetch_my_trades, symbol, None, None, {"order_id": order_id})
        out = []
        for t in trades:
            if t.get("order") != order_id:
                continue
            fee = (t.get("fee") or {}).get("cost") or 0
            out.append(VenueFill(str(t["id"]), Decimal(str(t["amount"])), Decimal(str(t["price"])), Decimal(str(fee)),
                                 t.get("takerOrMaker") or "taker",
                                 datetime.fromtimestamp(t["timestamp"] / 1000, UTC)))  # fmt: skip
        return out

    async def submit(self, req: OrderRequest) -> VenueOrderView:
        symbol = self.symbols.get(req.market_key)
        if symbol is None:
            raise LiveDisabled(f"{req.market_key} is not in the live approval")
        params = {"client_order_id": req.client_order_id, "timeInForce": req.time_in_force}
        order = await self._call(self.ex.create_order, symbol, "limit", req.side, float(req.qty), float(req.limit_price),
                                 params)  # fmt: skip
        view = self._view(order)
        view.client_order_id = req.client_order_id
        if order.get("id"):
            view.fills = await self._fills(symbol, order["id"])
        return view

    async def lookup(self, client_order_id: str) -> VenueOrderView | None:
        for symbol in self.symbols.values():
            orders = await self._call(self.ex.fetch_orders, symbol, None, 100, {})
            for o in orders:
                if o.get("clientOrderId") == client_order_id:
                    view = self._view(o)
                    view.client_order_id = client_order_id
                    view.fills = await self._fills(symbol, o["id"])
                    return view
        return None

    async def cancel(self, client_order_id: str) -> VenueOrderView | None:
        view = await self.lookup(client_order_id)
        if view is None or view.exchange_order_id is None:
            return view
        symbol = next(iter(self.symbols.values()))
        await self._call(self.ex.cancel_order, view.exchange_order_id, symbol)
        return None  # the order manager confirms with a lookup; never trust a cancel ack alone

    async def balances(self) -> dict[str, Decimal]:
        bal = await self._call(self.ex.fetch_balance)
        return {k: Decimal(str(v)) for k, v in (bal.get("total") or {}).items() if v}
