# Project instructions

Claude Code reads this file at the start of every session. Keep it short and specific to this repo.

## Overview
Claude Code terminal setup kit (README.md, scripts/, .claude/) plus `cryptobot/`, a Python crypto
trading bot with online learning and scam-token screening. It trades on ccxt exchanges (incl. Coinbase)
or on Solana via Jupiter, and can auto-discover coins (`scan`). See cryptobot/README.md.
`spotguard/` is a separate, self-contained, stricter paper-first application (own pyproject, venv, tests,
Dockerfile). See spotguard/README.md and spotguard/IMPLEMENTATION_MATRIX.md.

## Commands
- Install: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt ruff`
- Test: `.venv/bin/python -m pytest -q`
- Lint: `.venv/bin/ruff check cryptobot tests && .venv/bin/ruff format --check cryptobot tests`
- Run: `.venv/bin/python -m cryptobot --help`
- spotguard (run inside `spotguard/`): `python3 -m venv .venv && .venv/bin/pip install -r requirements.lock &&
  .venv/bin/pip install --no-deps -e .`, then `.venv/bin/python -m pytest -q` and
  `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests`

## Conventions
- Paper trading is the default; never make live trading easier to enable by accident.
- Features and labels must never use future bars (tests in tests/test_features.py guard this).
- Safety checks fail closed: unverifiable tokens are not tradable.
- Tests must run offline; inject fakes for network calls.

## Workflow
- Run tests and lint before committing.
- Keep changes focused; don't reformat unrelated code.
