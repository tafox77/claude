# Project instructions

Claude Code reads this file at the start of every session. Keep it short and specific to this repo.

## Overview
Claude Code terminal setup kit (README.md, scripts/, .claude/) plus `cryptobot/`, a Python crypto
trading bot with online learning and scam-token screening. See cryptobot/README.md.

## Commands
- Install: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt ruff`
- Test: `.venv/bin/python -m pytest -q`
- Lint: `.venv/bin/ruff check cryptobot tests && .venv/bin/ruff format --check cryptobot tests`
- Run: `.venv/bin/python -m cryptobot --help`

## Conventions
- Paper trading is the default; never make live trading easier to enable by accident.
- Features and labels must never use future bars (tests in tests/test_features.py guard this).
- Safety checks fail closed: unverifiable tokens are not tradable.
- Tests must run offline; inject fakes for network calls.

## Workflow
- Run tests and lint before committing.
- Keep changes focused; don't reformat unrelated code.
