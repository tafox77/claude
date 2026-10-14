"""Command line interface: backtest, run (paper/live) and status."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import UTC, datetime

from .config import load_config

LIVE_CONFIRM = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def cmd_backtest(args) -> int:
    from .backtest import run_backtest
    from .data import fetch_history, synthetic_history

    cfg = load_config(args.config)
    data = {}
    if args.synthetic:
        bars = int(args.days * 86400 / (_tf_seconds(cfg.timeframe)))
        for a in cfg.assets:
            data[a.symbol] = synthetic_history(a.symbol, cfg.timeframe, bars, seed=args.seed)
    else:
        from .exchange import make_ccxt_client

        client = make_ccxt_client(cfg.exchange)
        for a in cfg.assets:
            print(f"downloading {a.symbol} {cfg.timeframe} history from {cfg.exchange}...")
            data[a.symbol] = fetch_history(client, a.symbol, cfg.timeframe, args.days)
    for sym, candles in data.items():
        if candles:
            print(
                f"  {sym}: {len(candles)} candles {_fmt_ts(candles[0].ts)} -> "
                f"{_fmt_ts(candles[-1].ts)}"
            )
    print()
    print(run_backtest(cfg, data).report())
    return 0


def _tf_seconds(tf: str) -> int:
    from .data import TIMEFRAME_SECONDS

    return TIMEFRAME_SECONDS[tf]


def cmd_run(args) -> int:
    from .data import CcxtFeed
    from .engine import Engine
    from .exchange import CcxtExchange, PaperExchange, make_ccxt_client
    from .journal import Journal
    from .notify import Notifier

    cfg = load_config(args.config)
    if cfg.mode == "live":
        if args.confirm_live != LIVE_CONFIRM:
            print(
                'Refusing to trade real money. Config has mode = "live"; to proceed, pass\n'
                f"  --confirm-live {LIVE_CONFIRM}\n"
                "Make sure you have backtested and paper traded this config first.",
                file=sys.stderr,
            )
            return 2
        client = make_ccxt_client(cfg.exchange, with_keys=True)
        exchange = CcxtExchange(client, cfg.quote)
    else:
        client = make_ccxt_client(cfg.exchange)

    for a in cfg.assets:
        if a.symbol not in client.markets:
            print(f"{a.symbol} is not traded on {cfg.exchange}", file=sys.stderr)
            return 2

    feed = CcxtFeed(client, cfg.timeframe)
    if cfg.mode == "paper":
        exchange = PaperExchange(feed, cfg.starting_cash, cfg.fee_rate, cfg.slippage_pct)
    engine = Engine(cfg, feed, exchange, Journal(cfg.db_path), Notifier(cfg.notify_webhook_url))

    if args.once:
        engine.run_once()
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine.run_forever(stop)
    return 0


def cmd_status(args) -> int:
    from .journal import Journal

    cfg = load_config(args.config)
    j = Journal(cfg.db_path)
    curve = j.equity_curve()
    if curve:
        ts, eq = curve[-1]
        print(f"Mode: {cfg.mode}   Equity: {eq:,.2f} {cfg.quote} (as of {_fmt_ts(ts)} UTC)")
        print(f"Since start: {(eq / curve[0][1] - 1) * 100:+.2f}%")
    else:
        print("No equity recorded yet. Start the bot with `run`.")
    positions = j.load_positions()
    print(f"\nOpen positions ({len(positions)}):")
    for p in positions.values():
        print(
            f"  {p.symbol:<12} qty {p.qty:.8g} @ {p.entry_price:.8g}  "
            f"(cost {p.cost:,.2f}, opened {_fmt_ts(p.entry_ts)})"
        )
    trades = j.trades(limit=args.trades)
    print(f"\nLast {len(trades)} trades:")
    for t in trades:
        pnl = "" if t["pnl"] is None else f"  PnL {t['pnl']:+.2f}"
        print(
            f"  {_fmt_ts(t['ts'])}  {t['side']:<4} {t['symbol']:<12} {t['qty']:.8g} @ "
            f"{t['price']:.8g}{pnl}  [{t['reason']}]"
        )
    realized = sum(t["pnl"] or 0 for t in j.trades())
    print(f"\nRealized PnL: {realized:+,.2f} {cfg.quote}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cryptobot", description=__doc__)
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="replay history through the strategies")
    b.add_argument("--days", type=float, default=30)
    b.add_argument("--synthetic", action="store_true", help="use random data (offline)")
    b.add_argument("--seed", type=int, default=0)
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("run", help="trade automatically (paper or live per config)")
    r.add_argument("--once", action="store_true", help="run a single iteration and exit")
    r.add_argument("--confirm-live", default="", help='required when mode = "live"')
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="show positions, trades and PnL")
    s.add_argument("--trades", type=int, default=10)
    s.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    if args.cmd == "backtest" and not args.verbose:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return args.func(args)
    except Exception as e:
        if type(e).__module__.startswith("ccxt"):
            print(f"exchange error: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
