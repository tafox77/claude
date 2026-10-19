from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from conftest import NOW
from helpers import BTC, ETH, Clock, book, ts

from spotguard.config import MarketPolicy
from spotguard.identities import IdentityError, validate_coinbase_product
from spotguard.market_data import BookStatus, CoinbaseL2Handler, quote_usable, subscribe_messages

POL = MarketPolicy()


def l2(seq, kind, updates, product="BTC-USD", at=NOW):
    return json.dumps({
        "channel": "l2_data", "client_id": "", "timestamp": ts(at), "sequence_num": seq,
        "events": [{"type": kind, "product_id": product,
                    "updates": [{"side": s, "event_time": ts(at), "price_level": p, "new_quantity": q}
                                for s, p, q in updates]}],
    })  # fmt: skip


SNAP = [("bid", "100000", "1"), ("bid", "99990", "2"), ("offer", "100010", "1"), ("offer", "100020", "3")]


def test_snapshot_then_updates_builds_book():
    h = CoinbaseL2Handler([BTC])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    h.handle(l2(1, "update", [("bid", "100000", "0"), ("offer", "100010", "0.4")]), 2, NOW)
    q = h.quotes()[BTC.key]
    assert q.status is BookStatus.LIVE
    assert q.best_bid == Decimal("99990") and q.best_ask == Decimal("100010") and q.asks[0][1] == Decimal("0.4")


def test_sequence_gap_invalidates_and_requires_resync():
    h = CoinbaseL2Handler([BTC])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    events = h.handle(l2(5, "update", [("bid", "100001", "1")]), 2, NOW)
    assert events[0].kind == "gap" and h.needs_resync
    assert h.quotes()[BTC.key].status is BookStatus.GAP
    # Updates after a gap are ignored until a fresh snapshot arrives.
    h.handle(l2(6, "update", [("bid", "100002", "1")]), 3, NOW)
    assert h.quotes()[BTC.key].bids == ()
    h.on_reconnect()
    h.handle(l2(0, "snapshot", SNAP), 4, NOW)
    assert h.quotes()[BTC.key].status is BookStatus.LIVE


def test_duplicate_and_out_of_order_messages_are_dropped():
    h = CoinbaseL2Handler([BTC])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    h.handle(l2(1, "update", [("bid", "100000", "5")]), 2, NOW)
    assert h.handle(l2(1, "update", [("bid", "100000", "9")]), 3, NOW)[0].kind == "duplicate"
    assert h.quotes()[BTC.key].bids[0][1] == Decimal("5")


def test_crossed_book_is_not_quotable():
    h = CoinbaseL2Handler([BTC])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    events = h.handle(l2(1, "update", [("bid", "100015", "1")]), 2, NOW)
    assert events[0].kind == "resync" and h.quotes()[BTC.key].status is BookStatus.CROSSED


def test_malformed_levels_force_resync():
    h = CoinbaseL2Handler([BTC])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    for bad in ([("bid", "NaN", "1")], [("bid", "100", "-1")], [("ask", "100", "1")], [("bid", "0", "1")]):
        h2 = CoinbaseL2Handler([BTC])
        h2.handle(l2(0, "snapshot", SNAP), 1, NOW)
        assert h2.handle(l2(1, "update", bad), 2, NOW)[0].kind == "resync"
    assert h.handle("{not json", 3, NOW)[0].kind == "alert"


def test_alias_product_is_never_relabelled():
    h = CoinbaseL2Handler([BTC])
    events = h.handle(l2(0, "snapshot", SNAP, product="BTC-USDC"), 1, NOW)
    assert events[0].kind == "alert" and "alias" in events[0].detail
    assert h.quotes()[BTC.key].status is BookStatus.AWAITING_SNAPSHOT


def test_error_message_invalidates_books():
    h = CoinbaseL2Handler([BTC, ETH])
    h.handle(l2(0, "snapshot", SNAP), 1, NOW)
    assert h.handle(json.dumps({"type": "error", "message": "rate limit"}), 2, NOW)[0].kind == "error"
    assert all(q.status is BookStatus.INVALID for q in h.quotes().values())


def test_quote_usability_rules():
    c = Clock()
    q = book(c)
    assert quote_usable(q, c.mono, c.wall, POL) == (True, "ok")
    c.advance(2.5)
    assert "age" in quote_usable(q, c.mono, c.wall, POL)[1]  # stale
    c2 = Clock()
    wide = book(c2, bid="100000", ask="100500")
    assert "spread" in quote_usable(wide, c2.mono, c2.wall, POL)[1]
    from dataclasses import replace

    skewed = replace(book(c2), exchange_ts=c2.wall - timedelta(seconds=30))
    assert "skew" in quote_usable(skewed, c2.mono, c2.wall, POL)[1]
    future = replace(book(c2), recv_mono_ns=c2.mono + 10**9)
    assert not quote_usable(future, c2.mono, c2.wall, POL)[0]
    assert quote_usable(None, c2.mono, c2.wall, POL)[1] == "no quote"
    assert "timestamp" in quote_usable(replace(book(c2), exchange_ts=None), c2.mono, c2.wall, POL)[1]


def test_nanosecond_timestamps_parse():
    h = CoinbaseL2Handler([BTC])
    msg = json.loads(l2(0, "snapshot", SNAP))
    msg["timestamp"] = "2026-10-19T12:00:00.123456789Z"
    h.handle(msg, 1, NOW)
    assert h.quotes()[BTC.key].exchange_ts.microsecond == 123456


def test_subscription_messages_match_documented_shape():
    subs = subscribe_messages([BTC, ETH])
    assert subs[0] == {"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"], "channel": "level2"}
    assert subs[1]["channel"] == "heartbeats"


PRODUCT = {
    "product_id": "BTC-USD", "base_currency_id": "BTC", "quote_currency_id": "USD", "product_type": "SPOT",
    "status": "online", "trading_disabled": False, "is_disabled": False, "cancel_only": False, "limit_only": False,
    "view_only": False, "auction_mode": False, "alias": "", "alias_to": ["BTC-USDC"],
    "base_increment": "0.00000001", "quote_increment": "0.01", "price_increment": "0.01",
    "base_min_size": "0.00000001", "quote_min_size": "1",
}  # fmt: skip


def test_product_validation_accepts_documented_listing():
    r = validate_coinbase_product(BTC, PRODUCT, NOW)
    assert r.base_increment == "0.00000001" and r.quote_min_size == "1"


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "change",
    [{"product_id": "BTC-USDC"}, {"alias": "BTC-USD"}, {"status": "delisted"}, {"trading_disabled": True},
     {"cancel_only": True}, {"product_type": "FUTURE"}, {"base_increment": ""}, {"view_only": None}],
)  # fmt: skip
def test_product_validation_blocks_bad_listings(change):
    with pytest.raises(IdentityError):
        validate_coinbase_product(BTC, {**PRODUCT, **change}, NOW)
