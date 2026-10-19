"""spotguard command line. PAPER is the default; nothing here places real orders."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
from decimal import Decimal
from pathlib import Path

from spotguard.config import load_policy
from spotguard.logs import setup_logging
from spotguard.storage import Store, utcnow

MATRIX_CANDIDATES = [Path(__file__).resolve().parents[2] / "IMPLEMENTATION_MATRIX.md", Path("/app/IMPLEMENTATION_MATRIX.md")]


def _policy(args):
    return load_policy(args.policy)


def _shadow_factory(args):
    """Shadow model (status 'shadow' or 'approved') that scores bars and can only block entries on drift."""
    if not getattr(args, "shadow", ""):
        return None
    from spotguard.learning.registry import Registry
    from spotguard.learning.shadow import DriftMonitor, ShadowRunner

    def factory(rt):
        reg = Registry(rt.store, args.models)
        model_id = _full_id(reg, args.shadow)
        row = reg.row(model_id)
        if row["status"] not in ("shadow", "approved"):
            raise SystemExit(f"model {model_id[:16]} is {row['status']}; move it to shadow first")
        params = json.loads(row["params"])
        drift = DriftMonitor(json.loads(row["feature_reference"]))

        def on_anomaly(detail):
            reg.record_drift(model_id, detail, rt.wall())
            rt.risk.latch("model_drift", f"model {model_id[:16]} drift: {detail}", rt.wall())

        return ShadowRunner(rt.store, model_id, reg.load(model_id), params["horizon"], 60, drift, on_anomaly)

    return factory


def cmd_init(args) -> int:
    policy = _policy(args)
    store = Store(args.db)
    store.set_initial_balance("USD", policy.risk.virtual_equity, utcnow())
    print(f"database ready: {args.db}  policy {policy.version}  mode {policy.mode.upper()}")
    return 0


def cmd_replay(args) -> int:
    from spotguard.replay import generate, run_replay

    policy = _policy(args)
    data = Path(args.data)
    if args.generate or not data.exists():
        n = generate(data, list(policy.cex_allowlist), minutes=args.minutes, seed=args.seed)
        print(f"generated {n} synthetic Coinbase-format messages → {data}", file=sys.stderr)
    for suffix in ("", "-wal", "-shm"):
        if args.fresh and Path(args.db + suffix).exists():
            Path(args.db + suffix).unlink()
    result = asyncio.run(run_replay(policy, data, args.db, _shadow_factory(args)))
    print(result.report())
    return 0


def cmd_paper(args) -> int:
    from spotguard.paper_live import run_paper

    policy = _policy(args)
    if policy.mode != "paper":
        print("policy mode must be 'paper' for this command", file=sys.stderr)
        return 1

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        rt = await run_paper(policy, args.db, stop, shadow_factory=_shadow_factory(args))
        print(json.dumps(rt.latency.summary(), indent=2))

    print("PAPER mode on live public Coinbase data. Ctrl+C to stop.", file=sys.stderr)
    asyncio.run(main())
    return 0


def cmd_validate_markets(args) -> int:
    from spotguard.paper_live import fetch_rules

    rules = asyncio.run(fetch_rules(_policy(args)))
    for key, r in rules.items():
        print(f"{key}: OK base_increment={r.base_increment} price_increment={r.price_increment} "
              f"min_size={r.base_min_size} min_notional={r.quote_min_size}")  # fmt: skip
    return 0


def cmd_screen(args) -> int:
    import httpx

    from spotguard.identities import SUPPORTED_CHAINS
    from spotguard.security.identity import CanonicalRegistry
    from spotguard.security.providers import EvmRpc, GoPlusClient, SolanaRpc
    from spotguard.security.screener import Screener

    policy = _policy(args)

    async def main():
        async with httpx.AsyncClient(timeout=10) as client:
            evm = {}
            for key, spec in SUPPORTED_CHAINS.items():
                url = os.environ.get(f"EVM_RPC_URL_{spec.goplus_chain_id}") if spec.goplus_chain_id else None
                if url:
                    evm[key] = EvmRpc(client, url)
            sol_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            screener = Screener(policy, Store(args.db), CanonicalRegistry.load(args.registry), GoPlusClient(client),
                                SolanaRpc(client, sol_url), evm)  # fmt: skip
            return await screener.screen(args.chain, args.address, args.symbol, utcnow())

    report = asyncio.run(main())
    print(f"{report.asset_key}: {report.verdict.value}")
    print("  PASS_POLICY means only that configured checks passed; it is not a safety guarantee.")
    for f in report.findings:
        print(f"  [{f.status.value}] {f.check}: {f.reason} ({f.source})")
    return 0 if report.verdict.value == "PASS_POLICY" else 2


def _replay_bars(path: Path, market_id: str, policy):
    import json as _json

    from spotguard.learning.features import BarBuilder, bars_to_arrays
    from spotguard.market_data import CoinbaseL2Handler
    from spotguard.replay import SimClock

    policy_markets = [m for m in policy.cex_allowlist if m.market_id == market_id]
    if not policy_markets:
        raise SystemExit(f"{market_id} is not on the policy allowlist")
    handler = CoinbaseL2Handler(policy_markets)
    builder = BarBuilder(60)
    clock = SimClock()
    bars = []
    with open(path) as fh:
        for line in fh:
            row = _json.loads(line)
            clock.offset_ms = row["recv_offset_ms"]
            for ev in handler.handle(row["msg"], clock.mono_ns(), clock.now()):
                if ev.kind == "book" and ev.market_key:
                    bar = builder.on_quote(handler.books[ev.market_key].quote(), clock.now())
                    if bar:
                        bars.append(bar)
            if handler.needs_resync:
                handler.on_reconnect()
    return bars_to_arrays(bars), len(bars)


def cmd_train(args) -> int:
    from spotguard.learning.registry import Registry
    from spotguard.learning.train import train_candidate

    policy = _policy(args)
    arrays, n = _replay_bars(Path(args.data), args.market, policy)
    if n < 200:
        print(f"only {n} bars; need at least 200 to train", file=sys.stderr)
        return 1
    reg = Registry(Store(args.db), args.models)
    model_id, metrics = train_candidate([arrays], reg, utcnow(), horizon=args.horizon,
                                        fee_pct=float(policy.execution.taker_fee_pct))  # fmt: skip
    h = metrics["holdout"]
    print(f"candidate {model_id[:16]} registered (status: candidate; it cannot trade)")
    print(f"  bars {n}  holdout rows {h['rows']}  log-loss {h['log_loss']:.4f}  brier {h['brier']:.4f}")
    print(
        f"  holdout model trades {h['model']['trades']} net {h['model']['net_return']:+.4f} "
        f"(stressed costs {h['model_stressed_costs']['net_return']:+.4f}, CI95 mean {h['model']['ci95']})"
    )
    print(f"  baselines: no-trade 0, fixed momentum {h['baseline_fixed_momentum']['net_return']:+.4f}, "
          f"buy/hold same exposure {h['baseline_buy_hold_same_exposure']:+.4f}")  # fmt: skip
    for warning in training_warnings(h):
        print(f"  WARNING: {warning}")
    return 0


def training_warnings(h: dict) -> list[str]:
    """Plain-language reasons a training result is not evidence of an edge."""
    import math

    out = []
    if h["rows"] < 500 or h["model"]["trades"] < 30:
        out.append(f"insufficient sample ({h['rows']} holdout rows, {h['model']['trades']} trades): not evidence of anything")
    p = h.get("base_rate")
    if p is not None and 0 < p < 1:
        base_ll = -(p * math.log(p) + (1 - p) * math.log(1 - p))
        if h["log_loss"] >= base_ll:
            out.append(f"log-loss {h['log_loss']:.4f} is no better than always predicting the base rate ({base_ll:.4f})")
    if h["model_stressed_costs"]["net_return"] <= 0:
        out.append("holdout loses money once costs are stressed")
    return out


def cmd_models(args) -> int:
    from spotguard.learning.registry import PromotionRejected, Registry

    reg = Registry(Store(args.db), args.models)
    now = utcnow()
    try:
        if args.action == "list":
            for r in reg.store.query("SELECT model_id, status, created_at FROM models ORDER BY created_at"):
                print(f"{r[0][:16]}  {r[1]:9}  {r[2]}")
        elif args.action == "shadow":
            reg.to_shadow(_full_id(reg, args.model), args.by, now)
        elif args.action == "approve":
            print(json.dumps(reg.approve(_full_id(reg, args.model), args.by, args.note, now), indent=2, default=str))
        elif args.action == "rollback":
            reg.rollback(_full_id(reg, args.model), args.by, args.note, now)
    except PromotionRejected as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


def _full_id(reg, prefix: str) -> str:
    rows = reg.store.query("SELECT model_id FROM models WHERE model_id LIKE ?", (prefix + "%",))
    if len(rows) != 1:
        raise SystemExit(f"model id prefix {prefix!r} matches {len(rows)} models")
    return rows[0][0]


def cmd_risk(args) -> int:
    from spotguard.risk import RiskEngine

    policy = _policy(args)
    engine = RiskEngine(policy, Store(args.db), utcnow())
    if args.action == "status":
        print(json.dumps(engine.state.to_dict(), indent=2))
    elif args.action == "reset":
        engine.reset(args.kind, args.by, args.note, utcnow(), Decimal(args.reanchor) if args.reanchor else None)
        print(f"{args.kind} reset by {args.by}")
    return 0


def cmd_kill(args) -> int:
    store = Store(args.db)
    now = utcnow()
    if args.action == "block":
        store.set_control("block_entries", "1", args.by, now)
    elif args.action == "unblock":
        store.set_control("block_entries", "0", args.by, now)
    elif args.action == "flatten":
        store.set_control("flatten", "requested", args.by, now)
    print(f"{args.action} recorded; the running trader applies it within ~1 s")
    return 0


def cmd_export(args) -> int:
    store = Store(args.db)
    rows = store.query("SELECT f.*, o.purpose, o.intent_id FROM fills f JOIN orders o USING (client_order_id) ORDER BY at")
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(rows[0].keys() if rows else ["fill_id"])
        for r in rows:
            w.writerow(list(r))
    print(f"{len(rows)} fills → {args.out} (accounting export; not a complete tax record)")
    return 0


def cmd_dashboard(args) -> int:
    from spotguard.dashboard import make_server

    policy = _policy(args)
    server, _ = make_server(args.db, policy.mode, policy.version, args.host, args.port, Store(args.db))
    print(f"dashboard on http://{args.host}:{args.port}  ({policy.mode.upper()})")
    server.serve_forever()
    return 0


def cmd_matrix(args) -> int:
    for path in MATRIX_CANDIDATES:
        if path.exists():
            print(path.read_text())
            return 0
    print("IMPLEMENTATION_MATRIX.md not found; see the project repository", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spotguard", description=__doc__)
    p.add_argument("--policy", default="policy.toml")
    p.add_argument("--db", default="spotguard.sqlite")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    r = sub.add_parser("replay", help="deterministic offline paper replay (synthetic data)")
    r.add_argument("--data", default="replay.jsonl")
    r.add_argument("--generate", action="store_true")
    r.add_argument("--minutes", type=int, default=180)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--fresh", action="store_true", help="delete the database first")
    r.add_argument("--shadow", default="", help="model id prefix to run in shadow (never trades)")
    r.add_argument("--models", default="models")
    r.set_defaults(func=cmd_replay)
    pp = sub.add_parser("paper", help="paper trading on live public Coinbase data")
    pp.add_argument("--shadow", default="", help="model id prefix to run in shadow (never trades)")
    pp.add_argument("--models", default="models")
    pp.set_defaults(func=cmd_paper)
    sub.add_parser("validate-markets", help="check allowlisted markets against Coinbase").set_defaults(func=cmd_validate_markets)
    s = sub.add_parser("screen", help="screen a DEX token (needs network)")
    s.add_argument("--chain", required=True)
    s.add_argument("--address", required=True)
    s.add_argument("--symbol", required=True, help="symbol you believe this token is (verified, never trusted)")
    s.add_argument("--registry", default="identity_registry.toml")
    s.set_defaults(func=cmd_screen)
    t = sub.add_parser("train", help="train a candidate model on recorded/replayed data")
    t.add_argument("--data", default="replay.jsonl")
    t.add_argument("--market", default="BTC-USD")
    t.add_argument("--horizon", type=int, default=15)
    t.add_argument("--models", default="models")
    t.set_defaults(func=cmd_train)
    m = sub.add_parser("models")
    m.add_argument("action", choices=["list", "shadow", "approve", "rollback"])
    m.add_argument("--model", default="")
    m.add_argument("--by", default="")
    m.add_argument("--note", default="")
    m.add_argument("--models", default="models")
    m.set_defaults(func=cmd_models)
    k = sub.add_parser("risk")
    k.add_argument("action", choices=["status", "reset"])
    k.add_argument("--kind", default="")
    k.add_argument("--by", default="")
    k.add_argument("--note", default="")
    k.add_argument("--reanchor", default="")
    k.set_defaults(func=cmd_risk)
    ks = sub.add_parser("kill", help="kill switch: block/unblock entries, or flatten")
    ks.add_argument("action", choices=["block", "unblock", "flatten"])
    ks.add_argument("--by", required=True)
    ks.set_defaults(func=cmd_kill)
    e = sub.add_parser("export-fills")
    e.add_argument("--out", default="fills.csv")
    e.set_defaults(func=cmd_export)
    d = sub.add_parser("dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    d.set_defaults(func=cmd_dashboard)
    sub.add_parser("matrix", help="print the implemented / not implemented matrix").set_defaults(func=cmd_matrix)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(10 if args.verbose else 20)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
