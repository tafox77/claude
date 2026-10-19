# Implemented / not implemented

Legend:
- **Tested**: implemented and covered by offline tests.
- **Mock-only**: implemented, but exercised only against recorded-format fixtures or fake clients. It has never talked to the real service from this build environment, which had no access to exchanges, chains or providers.
- **Not implemented**: absent. Where screening depends on it, the result is UNKNOWN, which blocks new exposure.

| Area | Component | Status |
| --- | --- | --- |
| Policy | Reviewed TOML policy, frozen dataclasses, policy hash on every intent/report | Tested |
| Identity | CEX market ids; EVM EIP-55 checksum; Solana canonical base58; unsupported chains (incl. Bitcoin as a DEX asset) rejected | Tested |
| Identity | Coinbase product validation: id match, alias refusal, status, trading flags, precision | Tested; REST fetch Mock-only |
| Identity | Reviewed canonical registry with provenance + independent symbol corroboration; lookalike BLOCK | Tested (registry ships empty) |
| Screening | Verdicts PASS_POLICY / BLOCK / UNKNOWN with evidence ids; missing/malformed/stale/future/conflicting → UNKNOWN | Tested |
| Screening | EVM evaluator (GoPlus `token_security` fields per official SDK v0.2.6 + EIP-1967 RPC checks, holders, LP lock expiry, taxes) | Tested (fixtures) |
| Screening | Solana evaluator (on-chain mint/freeze authority, Token-2022 extensions, program ownership, GoPlus Solana fields, LP burn, holder concentration excl. reviewed pool programs) | Tested (fixtures) |
| Screening | GoPlus / Solana RPC / EVM RPC clients, timeouts → UNKNOWN, coverage pinning via `/supported_chains` | Mock-only |
| Screening | Independent pool verification (depth, age, factory/program) | **Not implemented** → UNKNOWN |
| Screening | Balance-delta sell simulation (GoPlus Solana pre-run API and EVM eth_call simulation exist but are not integrated) | **Not implemented** → UNKNOWN |
| Screening | EVM privileged-role reachability analysis (owner present → UNKNOWN) | **Not implemented** |
| Screening | Concentrated-liquidity position analysis | **Not implemented** → UNKNOWN |
| Screening | Deployer-linked / correlated wallet clustering beyond provider tags | **Not implemented** (documented exclusion) |
| Screening | Scheduled rescans / streaming authority & liquidity changes for held DEX assets | **Not implemented** (no DEX trading in this build) |
| Market data | Coinbase L2 handler: snapshot/update, sequence gaps, duplicates, crossed books, malformed levels, alias products, nanosecond timestamps, clock skew, staleness | Tested |
| Market data | Reconnecting WebSocket client with subscribe-within-5s, heartbeats channel, backoff + jitter, resync on gap | Tested with fake socket; Mock-only vs Coinbase |
| Execution | Order state machine (CREATED…UNKNOWN), persisted client ids, reconcile-before-retry, cancel never assumed, fills idempotent | Tested |
| Execution | Paper venue: book-walking marketable limits, partial fills, IOC/GTC, increments/minimums, maker/taker fees, failure injection | Tested |
| Execution | Live Coinbase adapter via ccxt, disabled behind approval file + env flag + policy mode + gates | Gate Tested; adapter Mock-only (fake ccxt); **never run against Coinbase** |
| Execution | Sandbox/testnet | **Not available** (ccxt reports no Coinbase sandbox). Paper is labelled simulation |
| Execution | Native protective (stop) orders on the venue | **Not implemented**: stops are enforced client-side by the supervisor |
| Execution | DEX transaction policy (recipient/program allowlist, exact decimals, min-out, bounded approvals, fee caps, nonce/blockhash), tx tracker (dedupe, finality, reorg), x·y=k pool sim | Tested (simulation only) |
| Execution | DEX wire-format decoding, signing, broadcasting, real chain state | **Not implemented** |
| Risk | Exposure + stop-distance sizing with cost allowance, venue minimums, correlation groups, no averaging down, UNKNOWN orders block entries | Tested |
| Risk | Daily loss (realized + unrealized + fees, tz anchor) and high-water drawdown latches, persisted, reviewer reset, cash-flow adjustment | Tested |
| Risk | Kill switch: block entries (A) and flatten (B) as separate, visible states | Tested |
| Supervisor | Stop / target / max hold / deterioration / manual flatten, gap-through-stop at market, residuals, unsellable alert, stale-book retry | Tested |
| Runtime | Async pipeline with conflating bounded queue, per-asset entry lock, independent supervisor/mark loops, startup order + balance reconciliation, leader lock | Tested |
| Runtime | Deterministic offline replay through the real runtime (synthetic Coinbase-format data with injected anomalies) | Tested |
| Storage | SQLite WAL single writer, migrations, atomic fills, replay-safe event ids | Tested |
| Storage | PostgreSQL / multi-process | **Not implemented** |
| Learning | Causal features, delayed labels with costs, purged walk-forward + embargo, untouched holdout, baselines, bootstrap CI, regimes, rug metrics vs independent incidents | Tested |
| Learning | Immutable hash-verified registry, shadow-only runner, approval gates (30 days / 200 trades / holdout / folds / drift), rollback, drift → no-new-entries | Tested |
| Learning | Models influencing live decisions | **Not implemented by design**: strategy is a fixed baseline; models only run in shadow |
| Learning | Rug-incident dataset, delisted-token data (survivorship bias) | **Not included** |
| Ops | Localhost dashboard with CSRF token, remote bind requires bearer token | Tested |
| Ops | Credential redaction in logs | Tested |
| Ops | Dockerfile (paper/replay image, non-root, optional build-only CA secret) | Tested: built and ran the offline replay with `--network none` |
| Ops | Alert delivery (email/SMS/pager), secret-manager integration | **Not implemented** (alerts go to DB, dashboard and logs; secrets via env) |
