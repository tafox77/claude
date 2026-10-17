"""cryptobot: risk-managed crypto trading with online learning and scam-token screening."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

from cryptobot.config import load_config
from cryptobot.features import to_frame
from cryptobot.model import OnlineModel
from cryptobot.safety import SafetyChecker, resolve_contract


def _market(cfg):
    if cfg.venue == "solana":
        from cryptobot.solana import SolanaMarketData

        return SolanaMarketData(cfg)
    from cryptobot.exchange import MarketData, make_exchange

    return MarketData(make_exchange(cfg.exchange))


def _checker(cfg) -> SafetyChecker:
    return SafetyChecker(cfg.safety, solana=cfg.solana)


def _universe(cfg, checker, market):
    from cryptobot.universe import Universe, exchange_candidates, solana_candidates

    if cfg.venue == "solana":
        return Universe(cfg, checker, lambda n: solana_candidates(cfg, n))
    quote = cfg.symbols[0].split("/")[1]
    return Universe(cfg, checker, lambda n: exchange_candidates(market.exchange, quote, n), market.fetch_ticker)


def cmd_check_token(args, cfg) -> int:
    report = _checker(cfg).check_contract(args.chain, args.address)
    print(report.summary())
    return 0 if report.tradable else 2


def cmd_check_market(args, cfg) -> int:
    market = _market(cfg)
    checker = _checker(cfg)
    worst = 0
    for symbol in args.symbols or cfg.symbols:
        bars = len(market.fetch_closed_ohlcv(symbol, cfg.timeframe, cfg.history_bars))
        report = checker.check_market(symbol, market.fetch_ticker(symbol), bars)
        contract = resolve_contract(cfg, symbol)
        print(report.summary())
        if contract:
            token = checker.check_contract(contract["chain"], contract["address"])
            print(token.summary())
            worst = max(worst, 0 if token.tradable else 2)
        worst = max(worst, 0 if report.tradable else 2)
        print()
    return worst


def cmd_scan(args, cfg) -> int:
    market = _market(cfg)
    universe = _universe(cfg, _checker(cfg), market)
    where = "Solana DEX pools" if cfg.venue == "solana" else f"{cfg.exchange} markets"
    print(f"Screening the most active {where} (up to {cfg.scan.candidates})...\n", file=sys.stderr)
    results = universe.screen()
    if not results:
        print("No markets found. Check your internet connection and the exchange/venue in your config.")
        return 1
    passed = [c.symbol for c, r in results if r.tradable]
    for cand, report in results:
        if args.all or report.tradable:
            print(f"{cand.symbol}  [{cand.note}]")
            print(report.summary())
            print()
    print(f"{len(passed)} of {len(results)} passed the safety screen: {', '.join(passed) or 'none'}")
    if not args.all and len(passed) < len(results):
        print("Run with --all to see why the others were rejected.")
    return 0


def cmd_backtest(args, cfg) -> int:
    from cryptobot.backtest import run_backtest, synthetic_ohlcv

    if args.synthetic is not None:
        df = synthetic_ohlcv(args.bars, seed=args.seed, signal=args.synthetic)
        datasets = {f"synthetic(signal={args.synthetic})": df}
    elif args.csv:
        df = pd.read_csv(args.csv)
        datasets = {Path(args.csv).stem: df[["timestamp", "open", "high", "low", "close", "volume"]]}
    else:
        market = _market(cfg)
        since = int((time.time() - args.days * 86_400) * 1000)
        datasets = {}
        for symbol in args.symbols or cfg.symbols:
            source = "GeckoTerminal" if cfg.venue == "solana" else cfg.exchange
            print(f"Downloading {args.days} days of {cfg.timeframe} {symbol} from {source}...", file=sys.stderr)
            datasets[symbol] = to_frame(market.fetch_history(symbol, cfg.timeframe, since))

    model = OnlineModel(cfg.model) if args.save_model else None
    for name, df in datasets.items():
        result = run_backtest(df, cfg, name, model=model)
        print(result.report())
        print()
        if args.trades and result.trades:
            pd.DataFrame([vars(t) for t in result.trades]).to_csv(args.trades, index=False)
    if model is not None:
        path = Path(cfg.state_dir) / "model.joblib"
        model.save(path)
        print(f"Saved model trained on this history to {path} (paper/live will continue from it)")
    return 0


def cmd_run(args, cfg) -> int:
    from cryptobot.engine import Engine
    from cryptobot.exchange import LiveBroker, PaperBroker

    if args.command == "live":
        if not args.i_understand_the_risks or cfg.mode != "live":
            print(
                "Live trading needs `mode: live` in the config AND --i-understand-the-risks.\n"
                "Run `paper` first and only go live with money you can afford to lose.",
                file=sys.stderr,
            )
            return 1
        quote = cfg.symbols[0].split("/")[1]
        if any(s.split("/")[1] != quote for s in cfg.symbols):
            print("All symbols must share one quote currency in live mode", file=sys.stderr)
            return 1
    market = _market(cfg)
    if args.command == "live" and cfg.venue == "solana":
        from cryptobot.solana import JupiterBroker

        broker = JupiterBroker(cfg, market)
    elif args.command == "live":
        broker = LiveBroker(cfg.exchange, quote)
    else:
        cfg.mode = "paper"
        broker = PaperBroker(cfg.starting_cash, cfg.fee_rate, cfg.slippage)
    model = OnlineModel.load_or_new(Path(cfg.state_dir) / "model.joblib", cfg.model)
    checker = _checker(cfg)
    universe = _universe(cfg, checker, market) if cfg.scan.enabled else None
    engine = Engine(cfg, market, broker, model, checker, universe=universe)
    if args.once:
        engine.step()
    else:
        engine.run_forever()
    return 0


def cmd_status(args, cfg) -> int:
    state_dir = Path(cfg.state_dir)
    model_path = state_dir / "model.joblib"
    if model_path.exists():
        print("Model:", json.dumps(OnlineModel.load_or_new(model_path, cfg.model).stats(), indent=2, default=str))
    else:
        print("Model: not trained yet")
    for mode in ("paper", "live"):
        state = state_dir / f"state_{mode}.json"
        if state.exists():
            data = json.loads(state.read_text())
            print(f"\n[{mode}] open positions: {json.dumps(data.get('positions', {}), indent=2)}")
            if "paper_cash" in data:
                print(f"[{mode}] cash: {data['paper_cash']:.2f}")
            if data.get("kill_switch", {}).get("tripped"):
                print(f"[{mode}] KILL SWITCH TRIPPED; review trades, then delete {state} to reset")
        trades = state_dir / f"trades_{mode}.csv"
        if trades.exists():
            df = pd.read_csv(trades)
            print(
                f"[{mode}] {len(df)} closed trades, total pnl {df['pnl'].sum():+.2f}, "
                f"win rate {(df['pnl'] > 0).mean():.1%}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cryptobot", description=__doc__)
    parser.add_argument("-c", "--config", help="YAML config file (default: built-in paper defaults)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-token", help="Scan a token contract for scam / rug-pull red flags")
    p.add_argument("chain", help="solana, ethereum, bsc, base, arbitrum, polygon, optimism, avalanche")
    p.add_argument("address", help="Token contract address (the mint address on Solana)")
    p.set_defaults(func=cmd_check_token)

    p = sub.add_parser("check-market", help="Run the pre-trade safety checks on exchange symbols")
    p.add_argument("symbols", nargs="*")
    p.set_defaults(func=cmd_check_market)

    p = sub.add_parser("scan", help="Find the most active coins (incl. meme coins) and safety-screen them")
    p.add_argument("--all", action="store_true", help="Also show rejected coins and why")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("backtest", help="Walk-forward backtest with online learning")
    p.add_argument("symbols", nargs="*", help="Symbols to download (default: config symbols)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--csv", help="Use a CSV with timestamp,open,high,low,close,volume instead")
    p.add_argument("--synthetic", type=float, metavar="SIGNAL", help="Use generated data (0 = pure noise)")
    p.add_argument("--bars", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--trades", help="Write the trade list to this CSV")
    p.add_argument("--save-model", action="store_true", help="Keep the trained model for paper/live")
    p.set_defaults(func=cmd_backtest)

    for name, help_text in (
        ("paper", "Trade with simulated money on live prices"),
        ("live", "Trade real money (requires explicit opt-in)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--once", action="store_true", help="Run a single cycle and exit")
        if name == "live":
            p.add_argument("--i-understand-the-risks", action="store_true")
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="Show model track record, positions and P&L")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args.config)
    return args.func(args, cfg)
