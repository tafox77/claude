"""Property tests: no negative cash, no overselling, no overfilled or double-applied orders."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from conftest import NOW
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from spotguard.storage import LedgerError, Store

ops = st.lists(
    st.tuples(
        st.sampled_from(["buy", "sell"]),
        st.decimals(min_value="0.001", max_value="5", places=3),
        st.decimals(min_value="1", max_value="5000", places=2),
        st.integers(min_value=0, max_value=3),  # fill id reuse to exercise duplicates
    ),
    max_size=40,
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops)
def test_ledger_invariants(seq):
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "db.sqlite")
        store.set_initial_balance("USD", Decimal("10000"), NOW)
        for i, (side, qty, price, reuse) in enumerate(seq):
            cid = f"o{i}"
            store.insert_intent(
                {
                    "intent_id": f"i{i}",
                    "created_at": NOW.isoformat(),
                    "asset_key": "cex:x:A-USD",
                    "side": side,
                    "purpose": "entry",
                    "qty": str(qty),
                    "limit_price": str(price),
                    "reason": "",
                    "policy_version": "p",
                    "model_version": None,
                    "decision": "{}",
                }
            )
            store.insert_order(
                {
                    "client_order_id": cid,
                    "intent_id": f"i{i}",
                    "asset_key": "cex:x:A-USD",
                    "side": side,
                    "purpose": "entry",
                    "qty": str(qty),
                    "limit_price": str(price),
                    "time_in_force": "IOC",
                    "state": "SUBMITTING",
                    "created_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
            )
            fill_id = f"f{i - reuse}" if reuse and i >= reuse else f"f{i}"
            for _attempt in range(2):  # apply each fill twice: the second must be a no-op or rejected
                try:
                    store.apply_fill(fill_id, cid, qty, price, qty * price * Decimal("0.006"), "taker", NOW, "USD")
                except LedgerError:
                    pass
            cash = store.balance("USD")
            pos = store.query("SELECT qty FROM positions")
            held = Decimal(pos[0][0]) if pos else Decimal(0)
            assert cash >= 0, "negative cash"
            assert held >= 0, "oversold"
            for o in store.query("SELECT qty, filled_qty FROM orders"):
                assert Decimal(o[1]) <= Decimal(o[0]), "overfilled order"
            fills = store.query("SELECT count(*), count(DISTINCT fill_id) FROM fills")[0]
            assert fills[0] == fills[1], "double-applied fill"
            # Cash + cost basis is conserved except for fees and realized P&L, which are tracked.
        store.close()
