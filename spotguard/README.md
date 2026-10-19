# spotguard

A **spot-only** crypto trading application. It runs in **PAPER** mode by default and makes no profit claims. It:
- evaluates opportunities with a fixed baseline strategy
- screens token risk per chain
- gates every entry through an independent risk engine
- manages entries and exits with a persisted order state machine
- measures latency
- learns in **shadow** without ever weakening protections

What it never does:
- no leverage, derivatives, borrowing, martingale or averaging down
- no automatic deposits or withdrawals
- no LLM anywhere in the order path

> It does not guarantee profit, does not detect every scam, and does not guarantee stops execute at the stop price. Prices gap, liquidity disappears, and paper fills are more optimistic than live fills. **PASS_POLICY** only means the configured checks passed with fresh evidence. It never means "safe" or "legitimate".

## Status

**205 offline tests pass**, and ruff lint and format checks are clean. Python 3.13.16 on Linux; the code targets 3.11 and later.

| Test file | Tests |
| --- | --- |
| `test_security.py` | 73 |
| `test_execution.py` | 27 |
| `test_dex_and_live.py` | 25 |
| `test_runtime.py` | 21 |
| `test_market_data.py` | 19 |
| `test_policy.py` | 17 |
| `test_risk.py` | 12 |
| `test_learning.py` | 10 |
| `test_properties.py` | 1 (60 generated cases) |

An independent code review (October 19) traced every subsystem against the code. It found ten significant problems, which have since been fixed and covered by regression tests. Smaller limitations it found are listed under **Known gaps**.

**Nothing here has connected to a real exchange, chain or security provider.** The build environment had no network access to them. External formats were checked against the official GoPlus Python SDK (v0.2.6 swagger models), ccxt 4.5.88's Coinbase REST/WebSocket parsers, and Coinbase's documentation via search. Every network adapter is therefore marked **mock-only** in [IMPLEMENTATION_MATRIX.md](IMPLEMENTATION_MATRIX.md).

The spec's reference `guard.py` and its offline replay were **not provided** with the request, so they could not be baselined. `spotguard replay` is the offline replay replacement.

## Quick start

```bash
cd spotguard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock && pip install --no-deps -e .
python -m pytest -q                       # 205 passed

spotguard init                            # creates spotguard.sqlite with $10,000 virtual USD
spotguard replay --generate --fresh       # deterministic offline paper replay (synthetic data)
spotguard dashboard                       # http://127.0.0.1:8765 (localhost only)
```

**On a machine with internet access** (paper only, public data, no API keys):

```bash
spotguard validate-markets                # checks BTC-USD / ETH-USD against Coinbase's product listing
spotguard paper                           # live public Coinbase L2 data → paper fills; Ctrl+C to stop
spotguard screen --chain solana --address <mint> --symbol <SYMBOL>
```

**Learning**: models run in shadow only. Promotion is refused without evidence.

```bash
spotguard train --data replay.jsonl --market BTC-USD      # registers an immutable candidate + prints warnings
spotguard models list
spotguard models shadow  --model <id-prefix> --by <you>
spotguard replay --shadow <id-prefix>                       # or: spotguard paper --shadow <id-prefix>
spotguard models approve --model <id-prefix> --by <you> --note "..."   # refused until 30 days / 200 trades / holdout / folds / no drift
```

**Controls:**

```bash
spotguard kill block --by <you>           # kill switch A: no new entries
spotguard kill flatten --by <you>         # kill switch B: cancel entries, reconcile, exit all within the reviewed impact limit
spotguard risk status
spotguard risk reset --kind daily_loss --by <you> --note "reviewed" --reanchor <equity>
#   latches (daily_loss, drawdown, reconciliation, model_drift) survive restarts; only a named review clears them.
#   A running trader picks up the reset within ~1 s. Without --reanchor a daily_loss/drawdown latch re-trips
#   on the next mark if the loss is still beyond the limit.
spotguard export-fills --out fills.csv    # accounting export (not a complete tax record)
spotguard matrix                          # implemented / not-implemented matrix
```

**Docker** (paper/replay image, non-root, no credentials):

```bash
docker build -t spotguard .
# behind a TLS-inspecting proxy: docker build --secret id=pip_ca,src=/path/to/ca.crt -t spotguard .
docker run --rm --network none -v "$PWD/data:/data" spotguard        # 60-minute offline replay
docker run --rm -v "$PWD/data:/data" spotguard paper                  # needs internet
```

Verified here: the image builds, runs as non-root (uid 10001), and completes the offline replay with `--network none`.

## Architecture

```
Coinbase WS (level2 + heartbeats) ──► L2 handler (seq gaps / dupes / crossed / skew / stale)
        │                                     │
        │                               QuoteBoard (latest book per market)
        │                     ┌───────────────┼──────────────────────────┐
        ▼                     ▼               ▼                          ▼
   reconnect + resync   ConflatingQueue   ExitSupervisor (own loop)   Mark loop (equity, latches,
                        (bounded, latest)  stop/target/max-hold/       dashboard controls)
                              │            deterioration/flatten
                              ▼                   │
                     Strategy (fixed baseline)    │
                              ▼                   │
                     Independent RiskEngine ◄─────┘ (exits are never blocked by entry controls)
                              ▼
                     OrderManager (persisted intents, client ids, state machine, reconcile)
                              ▼
            PaperVenue | CoinbaseLiveAdapter (disabled) | DEX simulation (policy-validated)

Shadow learning (inside the strategy step) ─► predictions/labels ─► drift alarm ─► model_drift latch
`spotguard screen` (separate command, not part of the trading loop) ─► PASS_POLICY / BLOCK / UNKNOWN + evidence
```

**How the pieces connect, exactly:**
- **Controls.** Dashboard and CLI controls are applied only when they change. Each one is credited to whoever made it. A stale row can never undo a later block.
- **Automatic protections are latches.** These are the startup ledger/venue mismatch, model drift, daily loss and drawdown. Pause/resume can't clear a latch; only `risk reset` can.
- **Stop and target travel with the entry intent.** They are written to the position in the same database transaction as the first fill. So any entry that fills, by whatever path, is protected, and a re-entry never inherits an old stop.
- **Early exits for deterioration.** An exchange market is exited early if it is removed from the allowlist. A DEX asset is exited if its latest stored screening report is BLOCK. Product status is validated at startup only.
- **Exit prices.** Exits are priced off the best bid, floored at the reviewed emergency impact below the mid. A wide spread can't block a stop, and a collapsed bid side can't be dumped into.
- **Sizing.** The loss at the stop is computed as stop distance + taker fee on both legs + the maximum exit slippage + an extra allowance.

**Storage:** SQLite in WAL mode with a single writer and an exclusive leader lock (one trading process per database). These are persisted transactionally:
- intents, orders and order events
- fills, positions and cash
- risk state with anchors, high-water mark and latches
- security evidence and reports
- predictions and labels
- models and model events
- alerts and controls

Money is stored as `Decimal` strings, never floats.

## Supported venues and chains

| | Supported | Notes |
| --- | --- | --- |
| CEX | Coinbase Advanced Trade, spot `BTC-USD` and `ETH-USD` (allowlist in `policy.toml`) | Paper only. A live adapter exists but is disabled and has never been run against Coinbase. Coinbase has no ccxt sandbox. Choose a venue that is available in your jurisdiction; don't bypass regional restrictions. |
| Solana | SPL Token and Token-2022 screening | Simulated DEX execution only |
| EVM | Ethereum (`evm:1`), BNB Chain (`evm:56`), Base (`evm:8453`), Arbitrum (`evm:42161`) | Screening, with simulated DEX execution only |
| Bitcoin | CEX market only | Never routed through a token scanner |

**Screening defaults** (`policy.toml`) are starting assumptions, not validated boundaries:
- $250k minimum verified pool liquidity
- 7-day minimum pool age
- top-10 non-pool holders at or below 20%
- buy and sell tax at or below 2%
- at least 95% of LP burned or locked for 30+ days
- no unresolved dangerous authorities
- evidence no older than 60 seconds

Because independent pool verification and balance-delta sell simulation are **not implemented**, every real DEX token currently screens as **UNKNOWN**, which blocks entries. Newly launched meme coins would be excluded regardless.

## Measured latency

These are local computation figures from the 240-minute replay (71,980 messages) on this build machine. They exclude network and scanner time and are not fill-speed promises.

| Stage | p50 | p95 | p99 |
| --- | --- | --- | --- |
| quote → decision (strategy + risk) | 0.115 ms | 0.219 ms | 0.275 ms |
| decision → submit | 0.040 ms | 0.070 ms | 0.078 ms |
| submit → ack/fill (paper venue + SQLite fsync) | 2.77 ms | 5.03 ms | 6.67 ms |

The local p95 target is under 100 ms.

The same replay made 55 entries and 53 round trips, with **−0.53% net after $325 of fees**. This is synthetic data and is **not evidence of profitability either way**. At low Coinbase fee tiers, fees dominate short-horizon momentum.

## Known gaps

The full list is in the [matrix](IMPLEMENTATION_MATRIX.md). **Screening is a separate command:** the trading loop only trades the two allowlisted Coinbase markets and never consults `screen` results for entries. The main gaps:
- Independent pool, sell-simulation, role-reachability and concentrated-liquidity analysis are missing, so DEX tokens screen as UNKNOWN.
- Stops are enforced by the supervisor, not by native exchange stop orders. They can only act while the process runs and has data.
- PostgreSQL and multiple processes are not supported.
- Alerts are not delivered anywhere outside the dashboard and logs.
- No secret-manager integration.
- No rug-incident or delisted-token datasets.
- Models never drive trades; the strategy is a fixed baseline.
- Smaller limitations found in the code review, not yet fixed:
  - During a feed resync, the last good quote can still be used for up to 2 s.
  - A quiet market with no book updates for 2 s counts as stale, which delays exits until the next update.
  - Exception tracebacks bypass log redaction.
  - `replay` and `paper` default to the same database file; use `--db` to separate them.
  - Shadow labels ignore costs, and the approval gate counts closed trades globally rather than per model.

## Before any live setup

Live trading is not part of this delivery. Before enabling it you would need:

1. **Jurisdiction and venue:** confirm Coinbase (or another venue) is available to you.
2. **Approved scope:** exact markets and maximum capital at risk.
3. **Limits and model:** reviewed risk limits (a new `policy.toml` version) and the model id, or none.
4. **Deployment:** the host, timezone for daily anchors, who monitors it, and an alert channel.
5. **API key**, created with trade permission only, **withdrawals disabled** and an IP allowlist. Supply it through environment variables or a secret manager on the server. **Never paste keys, seed phrases or private keys into chat.**
6. **Acceptance gates passed:** 30+ days of paper running, 200+ closed paper trades, the full test suite, a Coinbase order-*preview* round trip, balance reconciliation on the live account, and an independent review.
7. **A signed approval file** (`LiveApproval`) matching all of the above, plus `SPOTGUARD_LIVE_ENABLE=I_ACCEPT_REAL_MONEY_RISK`.
