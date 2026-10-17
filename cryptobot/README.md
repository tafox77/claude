# cryptobot

A spot crypto trading bot that:

- **Enters and exits quickly with fixed risk.** Every trade gets a stop-loss and a take-profit sized from volatility (ATR). It risks 1% of equity per trade by default, and positions are capped at 20% of equity.
- **Screens out rug pulls and fake tokens** before buying (see below). If a check can't be completed, the bot doesn't trade.
- **Learns over time.** An online model updates from every bar once that bar's outcome is known. It keeps learning in paper and live mode and saves itself between runs.
- **Won't trade until it has proven itself.** Each prediction is scored *before* the model learns from it. The bot only trades when that out-of-sample record shows (a) better predictions than a simple baseline and (b) a positive average return after fees on the trades it would have taken.

> **Read this first.** No bot can guarantee profitable trades, and most trading strategies lose money after fees. This one is built to be cautious. On random price data it mostly refuses to trade, but that also means it may sit idle for long stretches on real markets. Backtest it, then paper trade for weeks before considering live mode, and only use money you can afford to lose. This is not financial advice.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit symbols, exchange, risk
```

## Usage

```bash
# Scan any token contract for scam / rug-pull red flags
python -m cryptobot check-token ethereum 0x6982508145454Ce325dDbE47a25d4ec3d2311933

# Pre-trade safety checks for your configured exchange symbols
python -m cryptobot -c config.yaml check-market

# Walk-forward backtest on real history (downloads from the exchange)
python -m cryptobot -c config.yaml backtest --days 120
python -m cryptobot backtest --synthetic 0     # pure noise: it should barely trade
python -m cryptobot backtest --synthetic 0.4   # hidden pattern: it should find it

# Warm-start: train on history, then keep learning in paper mode
python -m cryptobot -c config.yaml backtest --days 180 --save-model
python -m cryptobot -c config.yaml paper

# See the model's track record, open positions and P&L
python -m cryptobot -c config.yaml status
```

**Live trading** needs three things: `mode: live` in your config, the `EXCHANGE_API_KEY` and `EXCHANGE_API_SECRET` environment variables, and the `--i-understand-the-risks` flag:

```bash
python -m cryptobot -c config.yaml live --i-understand-the-risks
```

Give the API key trade permission only, with **withdrawals disabled**. Stops and targets are enforced by the bot, not placed on the exchange, so keep it running while it holds positions (on a server or with `tmux`).

## How it decides

Each cycle (every 30s by default), for each symbol:

1. **Learn.** It fetches closed candles and computes 13 features (returns, EMA spread, RSI, ATR, volatility regime, volume z-score, Bollinger position and more). Every bar whose 12-bar outcome window has closed gets a label: "would a trade entered here have been profitable after fees and slippage, using the bot's own stop, target and time-exit rules?". The model learns from that.
2. **Exit fast.** Open positions are checked against the *live* price every cycle, so a stop or target triggers within seconds rather than waiting for the candle to close. On each new candle it also checks the time exit (12 bars) and exits early if the model's confidence drops below `exit_threshold`.
3. **Enter carefully.** On each new candle it buys only if all of these hold:
   - P(profitable) ≥ `entry_threshold`
   - the model is trusted
   - there's a free position slot
   - the daily loss limit (3%) hasn't been hit
   - the drawdown kill switch (15% from peak) hasn't tripped
   - the safety checks pass

Every closed trade is written to `state/trades_<mode>.csv`. The model and open positions are saved under `state/`, so restarting picks up where it left off.

## Scam and rug-pull detection

`check-token` and the pre-trade gate combine several sources.

**Contract risks** (from the GoPlus security API)

These block a trade outright:
- honeypot (buyers can't sell)
- holders can't sell their full balance
- owner can change balances
- hidden owner
- ownership can be reclaimed
- self-destruct
- creator has deployed honeypots before
- unverified source code
- buy or sell tax of 50% or more

These are warnings: mintable supply, upgradeable proxy, pausable transfers, blacklist, owner-adjustable taxes, trading cooldowns.

**Rug-pull signals**
- a single wallet controls most of the unlocked liquidity (the developer can pull the pool)
- low liquidity, a pool younger than 7 days, low volume
- the top 10 wallets hold over 50% of supply, or the owner or creator holds a large stake
- valuation over 200× pool liquidity
- lots of buys with almost no sells (a classic honeypot pattern)

**Fake or impersonation tokens.** If a token uses the symbol of a major asset (USDT, USDC, WETH, WBTC, PEPE, ...) but its contract isn't the official one on that chain, it's flagged as an impersonation and blocked.

**Exchange markets.** Symbols are also checked for minimum 24h volume and maximum bid/ask spread, and get a warning if they have too little price history (a sign of a fresh listing).

Supported chains: ethereum, bsc, base, arbitrum, polygon, optimism, avalanche. Solana isn't supported yet. To run on-chain checks on an exchange symbol, map it to its contract under `token_contracts` in the config.

## Limitations

- Spot and long-only: no shorting or leverage.
- One shared model learns across all symbols.
- Only tested offline, against recorded API formats and synthetic data. Try it on real data with `check-token` and `backtest` before relying on it.
- Backtests use candle highs and lows. When a stop and a target both fall inside one candle, the stop is assumed to hit first.
