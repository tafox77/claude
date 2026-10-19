"""Policy validation: typos and nonsense values must fail loudly, never fall back to defaults."""

from __future__ import annotations

import pytest
from conftest import ROOT

from spotguard.config import PolicyError, load_policy
from spotguard.identities import CexMarketId, IdentityError, parse_evm_address

BASE = (ROOT / "policy.toml").read_text()


def write(tmp_path, text):
    p = tmp_path / "policy.toml"
    p.write_text(text)
    return p


def test_shipped_policy_loads():
    p = load_policy(ROOT / "policy.toml")
    assert p.mode == "paper" and len(p.cex_allowlist) == 2


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t.replace("[risk]\n", "[risks]\n"),  # misspelled section
        lambda t: t + '\nmax_leverage = "3"\n',  # unknown top-level key
        lambda t: t.replace('mode = "paper"', 'mode = "sandbox"'),  # not implemented
        lambda t: t.replace('timezone = "UTC"', 'timezone = "Mars/Olympus"'),
        lambda t: t.replace('daily_loss_stop_pct = "0.02"', 'daily_loss_stop_pct = "0"'),
        lambda t: t.replace('heartbeat_timeout_s = "10"', 'heartbeat_timeout_s = "0"'),
        lambda t: t.replace('max_spread_pct = "0.002"', 'max_spread_pct = "5"'),
        lambda t: t.replace("fast_window_s = 30", "fast_window_s = 600"),
        lambda t: t.replace('stop_pct = "0.006"', 'stop_pct = "3"'),
        lambda t: t.replace('majors = ["cex:coinbase:BTC-USD", "cex:coinbase:ETH-USD"]', 'majors = "cex:coinbase:BTC-USD"'),
        lambda t: t.replace('majors = ["cex:coinbase:BTC-USD", "cex:coinbase:ETH-USD"]', 'majors = ["cex:coinbase:SOL-USD"]'),
        lambda t: t + '\n[[allowlist.cex]]\nvenue = "coinbase"\nmarket_id = "BTC-USD"\nbase_id = "BTC"\nquote_id = "USD"\n',
        lambda t: t.replace('venue = "coinbase"\nmarket_id = "ETH-USD"', 'venue = "kraken"\nmarket_id = "ETH-USD"'),
        lambda t: t + "\nnot toml ===",
    ],
)
def test_bad_policies_are_rejected(tmp_path, edit):
    text = edit(BASE)
    assert text != BASE
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, text))


def test_trailing_newline_in_ids_rejected():
    with pytest.raises(IdentityError):
        parse_evm_address("0x" + "11" * 20 + "\n")
    with pytest.raises(IdentityError):
        CexMarketId("coinbase", "BTC-USD\n", "BTC", "USD\n")


def test_loaded_sections_have_the_right_types():
    from spotguard.config import MarketPolicy

    p = load_policy(ROOT / "policy.toml")
    assert isinstance(p.market, MarketPolicy)  # regression: a loop variable once overwrote this
