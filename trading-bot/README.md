# cryptobot: automated Bitcoin and meme coin trading

A small, self-contained trading bot that runs unattended. It:

- trades **BTC** with a trend-following strategy (EMA crossover with an RSI filter)
- trades **meme coins** (DOGE, PEPE, WIF, BONK, and anything else you list) with a
  momentum-breakout strategy: it buys volume-confirmed breakouts and exits fast when momentum fades
- enforces risk limits on every trade: per-asset allocation, stop-loss, take-profit, a trailing
  stop, a max number of open positions, a hard per-order cap and a **daily-loss kill switch**
- works with any exchange [ccxt](https://github.com/ccxt/ccxt) supports (Kraken, Coinbase,
  Binance.US, OKX and others)
- runs the **same engine** for backtests, paper trading and live trading
- logs every trade and equity snapshot to SQLite and can send alerts to a Discord or Slack webhook

> **Read this first.** Meme coins regularly drop 30 to 90%. No strategy here is proven to make
> money, and a backtest that looks good on past data often fails live. The bot defaults to
> **paper trading** (fake money, real prices). Only go live with money you can afford to lose
> entirely, and only after you have paper traded the exact config for a few weeks.

## Quick start

```bash
cd trading-bot
pip install -e ".[dev]"
cp config.example.toml config.toml

# 1. Backtest on real history (downloads candles from the exchange in config.toml)
cryptobot backtest --days 30

#    No network? Use random synthetic data. This only proves the code runs;
#    its results mean nothing.
cryptobot backtest --synthetic --days 60

# 2. Paper trade against live prices, automatically, until you stop it (Ctrl+C)
cryptobot run

# 3. Check on it from another terminal
cryptobot status
```

## Going live

1. On your exchange, create an API key with **trade permission only. Never enable withdrawals.**
   If the exchange supports it, restrict the key to your server's IP address.
2. Export the key (see `.env.example`):
   ```bash
   export CRYPTOBOT_API_KEY=...  CRYPTOBOT_API_SECRET=...
   ```
3. Set `mode = "live"` in `config.toml`, keep `max_order_usd` small to start, and run:
   ```bash
   cryptobot run --confirm-live I-UNDERSTAND-THIS-TRADES-REAL-MONEY
   ```

The bot only sells coins that it bought itself (tracked in its database). It never touches other
holdings in your account.

## Running it 24/7

The bot polls every `poll_seconds` and survives restarts. Open positions, paper cash and cooldowns
are stored in `db_path`. Pick one way to keep it running:

**systemd** (Linux server or VPS): save as `/etc/systemd/system/cryptobot.service`

```ini
[Unit]
Description=cryptobot
After=network-online.target

[Service]
WorkingDirectory=/opt/cryptobot/trading-bot
EnvironmentFile=/opt/cryptobot/trading-bot/.env
ExecStart=/usr/local/bin/cryptobot run
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cryptobot
journalctl -u cryptobot -f
```

**Docker**

```bash
docker build -t cryptobot .
docker run -d --restart unless-stopped --env-file .env \
  -v $PWD/config.toml:/app/config.toml -v $PWD/data:/data cryptobot
```

## How it decides

Every poll, for each configured asset:

1. Fetch the latest **closed** candles. The candle that is still forming is ignored, so signals
   don't flicker.
2. If a position is open, it exits on the first of: stop-loss, take-profit, trailing stop
   (armed once the price is up by the trail distance), or the strategy's SELL signal.
3. If no position is open and the strategy says BUY, it buys
   `min(equity × allocation_pct, free cash, max_order_usd)`. It skips the trade if the asset is
   in its post-exit cooldown, if `max_open_positions` is reached, if the kill switch has tripped,
   or if the order would be below `min_order_usd`.
4. Kill switch: if equity falls more than `max_daily_loss_pct` below the start of the UTC day,
   no new positions open until the next day. Existing positions keep their stops.

| Strategy        | Buy                                                                      | Sell                                            |
| --------------- | ------------------------------------------------------------------------ | ----------------------------------------------- |
| `trend`         | fast EMA > slow EMA, price above slow EMA, RSI < `rsi_max`               | fast EMA < slow EMA                             |
| `meme_momentum` | close above the highest high of `lookback` bars on ≥ `volume_mult`× avg volume | close below `exit_ema` EMA, or RSI ≥ `rsi_exit` |

All parameters live in `config.toml`. You can override them per asset with `params = { ... }`.
To add a strategy, subclass `Strategy` in `cryptobot/strategies.py` and register it in
`STRATEGIES`.

## Limitations

- **Stops are enforced by the bot, not by the exchange.** If the bot or its server is down, nothing
  closes positions. Run it somewhere reliable and watch the alerts.
- Stops are checked against the last price every `poll_seconds`. A fast wick can fill well past
  the stop level. Backtests check stops against candle closes, which understates wick risk.
- Backtests assume you fill at the candle close plus `slippage_pct`. Thinly traded meme coins can
  slip far more than that.
- It only trades spot markets with market orders. There is no leverage, no shorting and no
  on-chain or DEX tokens. A brand-new memecoin that only trades on a DEX such as Raydium or
  Uniswap needs a different execution layer.
- Symbol names differ by exchange (`BTC/USD` on Kraken, `BTC/USDT` on many others). `run` checks
  that every symbol exists before it starts.

## Development

```bash
pytest          # 27 tests, offline
ruff check . && ruff format --check .
```

Layout: `data.py` (candles, live, replay and synthetic feeds) · `indicators.py` ·
`strategies.py` · `risk.py` · `exchange.py` (paper and ccxt execution) · `engine.py` (the loop) ·
`backtest.py` · `journal.py` (SQLite) · `notify.py` · `__main__.py` (CLI).
