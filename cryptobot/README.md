# cryptobot

A spot crypto trading bot that:

- **Enters and exits quickly with fixed risk.** Every trade gets a stop-loss and a take-profit sized from volatility (ATR). It risks 1% of equity per trade by default, and positions are capped at 20% of equity.
- **Screens out rug pulls and fake tokens** before buying (see below). If a check can't be completed, the bot doesn't trade.
- **Learns over time.** An online model updates from every bar once that bar's outcome is known. It keeps learning in paper and live mode and saves itself between runs.
- **Trades on exchanges or on Solana.** Use any exchange ccxt supports (Binance, Coinbase, Kraken, ...), or trade Solana tokens directly from your own wallet through Jupiter.
- **Finds coins by itself, meme coins included.** The scanner looks at the most active markets and keeps the ones that pass the safety screen.
- **Won't trade until it has proven itself.** Each prediction is scored *before* the model learns from it. The bot only trades when that out-of-sample record shows (a) better predictions than a simple baseline and (b) a positive average return after fees on the trades it would have taken.

> **Read this first.** No bot can guarantee profitable trades, and most trading strategies lose money after fees. This one is built to be cautious. On random price data it mostly refuses to trade, but that also means it may sit idle for long stretches on real markets. Backtest it, then paper trade for weeks before considering live mode, and only use money you can afford to lose. This is not financial advice.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit symbols, exchange, risk
```

Ready-made starting points:

| File | For |
| --- | --- |
| `config.example.yaml` | Binance and other ccxt exchanges |
| `config.coinbase.example.yaml` | Coinbase (works in the US). Uses 1h candles because Coinbase fees are high |
| `config.solana.example.yaml` | Solana tokens and meme coins, with the scanner turned on |

## Usage

```bash
# Scan any token contract for scam / rug-pull red flags
python -m cryptobot check-token ethereum 0x6982508145454Ce325dDbE47a25d4ec3d2311933
python -m cryptobot check-token solana DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263

# Find the most active coins (meme coins included) and show which pass the safety screen
python -m cryptobot -c config.yaml scan          # add --all to see why the others failed

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

Give the API key trade permission only, with **withdrawals disabled**. For Coinbase, create the key at the Coinbase Developer Platform: the key *name* (`organizations/.../apiKeys/...`) is `EXCHANGE_API_KEY`, and the private key is `EXCHANGE_API_SECRET`. You can paste the private key on one line with `\n` where the line breaks go. Stops and targets are enforced by the bot, not placed on the exchange, so keep it running while it holds positions (on a server or with `tmux`).

## Solana

Set `venue: solana` (see `config.solana.example.yaml`). Symbols are quoted in USDC, like `BONK/USDC`:

- Well-known tokens (SOL, JUP, BONK, WIF, JTO, RAY, PYTH, mSOL, JitoSOL) work by name.
- Any other token works by mint address: either write the symbol as `<mint address>/USDC`, or name it under `token_contracts`.

Prices come from GeckoTerminal candles for the token's most liquid pool. In paper mode nothing touches a wallet.

**Live Solana trading** swaps through Jupiter and signs transactions on your computer. Your key never leaves your machine. Setup:

1. Use a **separate wallet** holding only what you're willing to lose. Fund it with USDC plus about 0.05 SOL for fees. The bot always keeps `min_sol_balance` SOL in reserve.
2. Export the wallet's private key (in Phantom: Settings → Manage Accounts → Show Private Key) and set `SOLANA_PRIVATE_KEY` to it. You can instead set `SOLANA_KEYPAIR_PATH` to a `solana-keygen` JSON file.
3. Set `mode: live` and run `python -m cryptobot -c config.yaml live --i-understand-the-risks`.

Buys are skipped when the swap would move the pool price more than `max_price_impact` (1%). Sells always go through, so you can get out. The public Solana RPC is slow and rate limited, so a free RPC URL from Helius or QuickNode in `solana.rpc_url` works better. If Jupiter's free endpoint changes, set `solana.jupiter_url` and, if needed, `JUPITER_API_KEY`.

## Finding coins automatically (scanner)

Turn on `scan: {enabled: true}` and the bot keeps a list of up to `max_symbols` extra markets on top of your `symbols`, refreshed every hour:

- **On an exchange:** every spot market in your quote currency, ranked by 24h volume. Stablecoins and leveraged tokens are skipped.
- **On Solana:** trending and highest-volume pools on GeckoTerminal, which is where meme coins trade. Pools below your liquidity, volume and age minimums are dropped first.

Every candidate then goes through the full safety screen. **Most new meme coins fail it**, usually because:
- the creator can still mint more supply or freeze holders' tokens
- most of the liquidity is unlocked
- a few wallets hold most of the supply
- the pool is only days old

That's deliberate. Run `scan --all` to see each coin's reasons. Coins you hold stay watched even after they drop off the list, so they can still be sold.

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

`check-token` and the pre-trade gate combine several sources. The first parts below cover EVM chains like Ethereum. Solana has its own checks, listed under **Solana tokens** at the end of this section.

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

**Solana tokens.** These are read directly from the chain, plus DexScreener, RugCheck and a simulated Jupiter sell.

These block a trade outright:
- the mint authority is still active (the creator can print more supply)
- the freeze authority is active (the creator can freeze your tokens so you can't sell)
- a Token-2022 permanent delegate (someone can take tokens out of your wallet)
- non-transferable tokens, or new accounts that start frozen
- a transfer fee of 50% or more
- RugCheck "danger" risks
- **no sell route on Jupiter** (a honeypot)

These are warnings:
- smaller transfer fees, or a fee authority that can raise them
- transfer hooks
- unlocked liquidity
- whale wallets
- still on the pump.fun bonding curve
- big price impact on a $100 sell

Pool vaults, lockers and burned tokens don't count toward holder concentration.

Supported chains: solana, ethereum, bsc, base, arbitrum, polygon, optimism, avalanche. To run on-chain checks on an exchange symbol, map it to its contract under `token_contracts` in the config. On `venue: solana` every symbol is checked automatically.

## Limitations

- Spot and long-only: no shorting or leverage.
- One shared model learns across all symbols. Meme coins behave differently from BTC, so it may be worth running them with a separate `state_dir` (and therefore a separate model).
- Newly found coins have little price history, so most trades come from coins that have been around a while.
- Free price APIs are rate limited. On Solana, watch at most about 10–15 coins with `poll_seconds: 60`.
- Only tested offline, against the APIs' documented formats and synthetic data. The Solana, Coinbase and scanner code has not yet run against the live services. Try it on real data with `check-token` and `backtest` before relying on it.
- Backtests use candle highs and lows. When a stop and a target both fall inside one candle, the stop is assumed to hit first.
