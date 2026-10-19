from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal

import pytest
from conftest import NOW
from helpers import BTC, Clock, book, rules

from spotguard.execution.paper import PaperVenue
from spotguard.logs import redact
from spotguard.replay import fixture_product, generate, run_replay
from spotguard.runtime import ConflatingQueue, QuoteBoard, Runtime
from spotguard.storage import Store
from spotguard.strategies.momentum import Signal


def make_runtime(policy, tmp_path, initial=Decimal("10000")):
    clock = Clock()
    board = QuoteBoard()
    venue = PaperVenue(board, {BTC.key: rules()}, policy.execution, policy.market, clock.mono_ns, clock.now,
                       {"USD": initial})  # fmt: skip
    rt = Runtime(policy, Store(tmp_path / "db.sqlite"), venue, board, {BTC.key: rules()}, clock.mono_ns, clock.now)
    return rt, clock, board, venue


async def test_concurrent_signals_cannot_double_enter(policy, tmp_path):
    rt, clock, board, _ = make_runtime(policy, tmp_path)
    board.update(book(clock))
    q = board(BTC.key)
    sig = Signal(BTC.key, q.best_ask, q.best_ask * Decimal("0.994"), q.best_ask * Decimal("1.012"), 0.01, "test")
    rt.strategy.on_quote = lambda quote, t: sig  # force a signal on every quote
    await asyncio.gather(*(rt.on_market(BTC.key) for _ in range(5)))
    await rt.on_market(BTC.key)
    entries = rt.store.query("SELECT count(*) FROM orders WHERE purpose = 'entry'")[0][0]
    assert entries == 1
    assert any("averaging down" in " ".join(d["reasons"]) for d in rt.decisions if not d["approved"])


async def test_startup_balance_mismatch_blocks_entries(policy, tmp_path):
    rt, clock, board, venue = make_runtime(policy, tmp_path)
    venue.cash["USD"] = Decimal("9000")  # venue disagrees with the ledger
    problems = await rt.startup(acquire_leader=False)
    assert problems and rt.risk.state.block_entries
    assert "reconciliation" in rt.risk.state.block_reason


async def test_startup_reconciliation_passes_when_consistent(policy, tmp_path):
    rt, *_ = make_runtime(policy, tmp_path)
    assert await rt.startup(acquire_leader=False) == []


def test_conflating_queue_keeps_one_pending_per_market():
    q = ConflatingQueue(maxsize=2)
    for _ in range(100):
        q.put("a")
    q.put("b")
    q.put("c")
    assert list(q.pending) == ["a", "b"] and q.dropped == 1


async def test_dashboard_controls_flow_into_risk_engine(policy, tmp_path):
    rt, *_ = make_runtime(policy, tmp_path)
    rt.store.set_control("block_entries", "1", "dashboard", NOW)
    rt.mark()
    assert rt.risk.state.block_entries
    rt.store.set_control("flatten", "requested", "dashboard", NOW)
    rt.mark()
    assert rt.risk.state.flatten_requested and rt.store.get_control("flatten") == "accepted"


# ----- live-data paper path with a fake WebSocket (mock: not a Coinbase integration test) -----------


class FakeWS:
    def __init__(self, messages, stop):
        self.messages = list(messages)
        self.sent = []
        self.stop = stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if not self.messages:
            self.stop.set()
            await asyncio.sleep(3600)
        await asyncio.sleep(0)
        return self.messages.pop(0)


async def test_paper_runner_end_to_end_with_fake_socket(policy, tmp_path):
    from spotguard.paper_live import run_paper

    stop = asyncio.Event()
    data = tmp_path / "r.jsonl"
    generate(data, list(policy.cex_allowlist), minutes=2, seed=3, anomalies=False)
    msgs = [json.dumps(json.loads(line)["msg"]) for line in data.read_text().splitlines()]
    sockets = []

    def connect(url, **kw):
        ws = FakeWS(msgs, stop)
        sockets.append(ws)
        return ws

    async def get(url):
        pid = url.rsplit("/", 1)[1]
        return fixture_product(next(m for m in policy.cex_allowlist if m.market_id == pid))

    rt = await asyncio.wait_for(run_paper(policy, str(tmp_path / "db.sqlite"), stop, connect=connect, get=get), 60)
    assert sockets[0].sent[0]["channel"] == "level2" and sockets[0].sent[1]["channel"] == "heartbeats"
    assert rt.feed_stats.messages > 100 and rt.feed_stats.gaps == 0


async def test_paper_runner_refuses_alias_listing(policy, tmp_path):
    from spotguard.identities import IdentityError
    from spotguard.paper_live import run_paper

    async def get(url):
        return {**fixture_product(policy.cex_allowlist[0]), "alias": "BTC-USDC"}

    with pytest.raises(IdentityError):
        await run_paper(policy, str(tmp_path / "db.sqlite"), asyncio.Event(), get=get)


# ----- replay determinism ---------------------------------------------------------------------------


async def test_replay_is_deterministic(policy, tmp_path):
    data = tmp_path / "r.jsonl"
    generate(data, list(policy.cex_allowlist), minutes=40, seed=11)
    a = await run_replay(policy, data, tmp_path / "a.sqlite")
    b = await run_replay(policy, data, tmp_path / "b.sqlite")
    assert a.digest == b.digest and a.final_equity == b.final_equity
    assert a.feed_events.get("gap", 0) >= 1 and a.feed_events.get("duplicate", 0) >= 1
    assert a.feed_events.get("resync", 0) >= 1


# ----- dashboard ---------------------------------------------------------------------------------


def test_dashboard_requires_csrf_token_and_local_binding(policy, tmp_path, monkeypatch):
    from spotguard.dashboard import make_server

    store = Store(tmp_path / "db.sqlite")
    store.set_initial_balance("USD", Decimal("10000"), NOW)
    server, token = make_server(str(tmp_path / "db.sqlite"), "paper", policy.version, "127.0.0.1", 0, store)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
        assert state["mode"] == "PAPER"
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "PAPER" in page and token in page
        body = json.dumps({"action": "flatten"}).encode()
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/control", body, method="POST"))
        assert exc.value.code == 403
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/control", body, method="POST",
                                     headers={"X-Spotguard-Token": token})  # fmt: skip
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        assert store.get_control("flatten") == "requested"
    finally:
        server.shutdown()
    monkeypatch.delenv("SPOTGUARD_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        make_server(str(tmp_path / "db.sqlite"), "paper", policy.version, "0.0.0.0", 0, store)


# ----- log redaction -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer eyJhbGciOiJFUzI1NiJ9.abc.def",
        "api_key=abcd1234efgh5678",
        '"secret": "s3cr3t-value"',
        "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE...\n-----END EC PRIVATE KEY-----",
        "0x" + "ab" * 32,
        "5" * 87,
        "[" + ",".join(["12"] * 64) + "]",
    ],
)
def test_logs_redact_credentials_and_signing_material(secret):
    out = redact(f"request failed: {secret} end")
    assert "REDACTED" in out
    for fragment in ("eyJhbGci", "abcd1234efgh5678", "s3cr3t-value", "MHcCAQEE", "ab" * 32, "5" * 87):
        assert fragment not in out
