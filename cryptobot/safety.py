"""Scam, rug-pull and fake-token screening.

Two layers:
* Token checks (on-chain): contract risk flags from the GoPlus token security API, pool
  liquidity, age and trading activity from DexScreener, and an impersonation check against
  the real contract addresses of well-known tokens.
* Market checks (exchange): 24h volume, bid/ask spread and listing history.

Anything that can't be verified fails closed: an API error makes the token UNKNOWN, and
the bot treats UNKNOWN like DANGER.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from cryptobot.config import SafetyConfig

log = logging.getLogger(__name__)

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "base": "8453",
    "optimism": "10",
    "avalanche": "43114",
}

# Real contracts for commonly impersonated tokens. A token using one of these symbols at
# a different address on the same chain is almost certainly a fake.
CANONICAL_TOKENS: dict[str, dict[str, str]] = {
    "ethereum": {
        "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
        "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
        "SHIB": "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce",
        "PEPE": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
    },
    "bsc": {
        "USDT": "0x55d398326f99059ff775485246999027b3197955",
        "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
        "WBNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "BUSD": "0xe9e7cea3dedca5984780bafc599bd69add087d56",
        "CAKE": "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",
    },
    "base": {
        "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "WETH": "0x4200000000000000000000000000000000000006",
    },
    "arbitrum": {
        "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "USDT": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
        "WETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    },
    "polygon": {
        "USDC": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "USDT": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        "WETH": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
    },
}
WELL_KNOWN_SYMBOLS = {sym for tokens in CANONICAL_TOKENS.values() for sym in tokens} | {
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "ADA",
    "DOGE",
    "TRX",
    "TON",
    "AVAX",
    "DOT",
    "MATIC",
}

# GoPlus flags that, when "1", mean holders can be trapped or drained.
CRITICAL_FLAGS = {
    "is_honeypot": "Honeypot: buyers cannot sell",
    "cannot_sell_all": "Holders cannot sell their full balance",
    "owner_change_balance": "Owner can change any holder's balance",
    "hidden_owner": "Contract has a hidden owner",
    "can_take_back_ownership": "Renounced ownership can be reclaimed",
    "selfdestruct": "Contract can self-destruct",
    "honeypot_with_same_creator": "Creator has deployed honeypots before",
}
WARNING_FLAGS = {
    "is_mintable": "Owner can mint new supply",
    "is_proxy": "Upgradeable proxy: code can be changed later",
    "transfer_pausable": "Transfers can be paused",
    "is_blacklisted": "Owner can blacklist wallets",
    "slippage_modifiable": "Owner can change trading taxes",
    "personal_slippage_modifiable": "Owner can set taxes per wallet",
    "trading_cooldown": "Trading cooldown restricts selling",
    "external_call": "Contract makes external calls during transfers",
}
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

DANGER, CAUTION, SAFE, UNKNOWN = "DANGER", "CAUTION", "SAFE", "UNKNOWN"


@dataclass
class SafetyReport:
    subject: str
    verdict: str = SAFE
    score: int = 100
    critical: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        return self.verdict in (SAFE, CAUTION)

    def finalize(self) -> SafetyReport:
        if self.verdict == UNKNOWN:
            self.score = 0
            return self
        self.score = max(0, 100 - 100 * len(self.critical) - 15 * len(self.warnings))
        if self.critical or self.score < 50:
            self.verdict = DANGER
        elif self.score < 80:
            self.verdict = CAUTION
        else:
            self.verdict = SAFE
        return self

    def summary(self) -> str:
        lines = [f"{self.subject}: {self.verdict} (score {self.score}/100)"]
        lines += [f"  [CRITICAL] {r}" for r in self.critical]
        lines += [f"  [WARNING]  {r}" for r in self.warnings]
        for key, value in self.info.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def _flag(data: dict, key: str) -> bool:
    return str(data.get(key, "0")) == "1"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


Fetch = Callable[[str, dict | None], dict]


def http_get_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, timeout=15, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def evaluate_contract(
    chain: str,
    address: str,
    goplus: dict | None,
    pairs: list[dict] | None,
    cfg: SafetyConfig,
    now_ms: int | None = None,
) -> SafetyReport:
    """Turn raw GoPlus + DexScreener data into a report. Pure function, no network."""
    address = address.lower()
    report = SafetyReport(subject=f"{chain}:{address}")
    if goplus is None or pairs is None:
        report.verdict = UNKNOWN
        report.critical.append("Could not fetch security data; refusing to trade unverified token")
        return report.finalize()

    symbol = str(goplus.get("token_symbol") or "").upper()
    report.subject = f"{symbol or '?'} ({chain}:{address})"
    report.info["name"] = goplus.get("token_name", "?")

    canonical = CANONICAL_TOKENS.get(chain, {})
    if symbol in canonical and canonical[symbol] != address:
        report.critical.append(f"Impersonation: real {symbol} on {chain} is {canonical[symbol]}, not this contract")
    elif symbol in WELL_KNOWN_SYMBOLS and symbol not in canonical:
        report.warnings.append(f"Uses well-known symbol {symbol}; verify it's the official contract")

    if not goplus:
        report.critical.append("Token unknown to the security scanner")
    if goplus and not _flag(goplus, "is_open_source"):
        report.critical.append("Contract source is not verified")
    for key, reason in CRITICAL_FLAGS.items():
        if _flag(goplus, key):
            report.critical.append(reason)
    for key, reason in WARNING_FLAGS.items():
        if _flag(goplus, key):
            report.warnings.append(reason)

    buy_tax, sell_tax = _num(goplus.get("buy_tax")), _num(goplus.get("sell_tax"))
    report.info["buy_tax"], report.info["sell_tax"] = f"{buy_tax:.1%}", f"{sell_tax:.1%}"
    if sell_tax >= 0.5 or buy_tax >= 0.5:
        report.critical.append(f"Extreme tax (buy {buy_tax:.0%}, sell {sell_tax:.0%})")
    elif sell_tax > cfg.max_sell_tax or buy_tax > cfg.max_buy_tax:
        report.warnings.append(f"High tax (buy {buy_tax:.1%}, sell {sell_tax:.1%})")

    holders = goplus.get("holders") or []
    top10 = sum(
        _num(h.get("percent"))
        for h in holders[:10]
        if not _flag(h, "is_locked") and str(h.get("address", "")).lower() not in BURN_ADDRESSES
    )
    report.info["top10_holders"] = f"{top10:.1%}"
    if top10 > cfg.max_top10_holder_pct:
        report.warnings.append(f"Top 10 wallets hold {top10:.0%} of supply (dump risk)")
    owner_pct = _num(goplus.get("owner_percent")) + _num(goplus.get("creator_percent"))
    if owner_pct > 0.2:
        report.warnings.append(f"Owner/creator hold {owner_pct:.0%} of supply")

    lp_holders = goplus.get("lp_holders") or []
    if lp_holders:
        lp_safe = sum(
            _num(h.get("percent"))
            for h in lp_holders
            if _flag(h, "is_locked") or str(h.get("address", "")).lower() in BURN_ADDRESSES
        )
        top_unlocked = max(
            (
                _num(h.get("percent"))
                for h in lp_holders
                if not _flag(h, "is_locked") and str(h.get("address", "")).lower() not in BURN_ADDRESSES
            ),
            default=0.0,
        )
        report.info["lp_locked_or_burned"] = f"{lp_safe:.1%}"
        if lp_safe < cfg.min_lp_locked_pct and top_unlocked > 0.5:
            report.critical.append(
                f"One wallet controls {top_unlocked:.0%} of unlocked liquidity: it can pull the pool (rug pull)"
            )
        elif lp_safe < cfg.min_lp_locked_pct:
            report.warnings.append(f"Only {lp_safe:.0%} of liquidity is locked or burned")

    _evaluate_pairs(report, address, pairs, cfg, now_ms)
    return report.finalize()


def _evaluate_pairs(
    report: SafetyReport, address: str, pairs: list[dict], cfg: SafetyConfig, now_ms: int | None
) -> None:
    pairs = [p for p in pairs if str(p.get("baseToken", {}).get("address", "")).lower() == address]
    if not pairs:
        report.critical.append("No DEX trading pairs found")
        return
    liquidity = sum(_num(p.get("liquidity", {}).get("usd")) for p in pairs)
    volume = sum(_num(p.get("volume", {}).get("h24")) for p in pairs)
    buys = sum(int(_num(p.get("txns", {}).get("h24", {}).get("buys"))) for p in pairs)
    sells = sum(int(_num(p.get("txns", {}).get("h24", {}).get("sells"))) for p in pairs)
    created = [int(_num(p.get("pairCreatedAt"))) for p in pairs if p.get("pairCreatedAt")]
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    age_days = (now_ms - min(created)) / 86_400_000 if created else 0.0

    report.info.update(
        liquidity_usd=f"${liquidity:,.0f}",
        volume_24h_usd=f"${volume:,.0f}",
        txns_24h=f"{buys} buys / {sells} sells",
        pair_age_days=f"{age_days:.1f}",
    )
    if liquidity < cfg.min_liquidity_usd:
        report.warnings.append(f"Low liquidity (${liquidity:,.0f}); easy to manipulate or rug")
    if volume < cfg.min_volume_24h_usd:
        report.warnings.append(f"Low 24h volume (${volume:,.0f})")
    if age_days < cfg.min_pair_age_days:
        report.warnings.append(f"Pool is only {age_days:.1f} days old; most rug pulls happen early")
    if buys >= 50 and sells <= buys * 0.05:
        report.critical.append(f"{buys} buys but only {sells} sells in 24h: likely cannot sell")
    fdv = _num(pairs[0].get("fdv"))
    if fdv and liquidity and fdv / liquidity > 200:
        report.warnings.append("Valuation is >200x pool liquidity; price can't absorb real selling")


class SafetyChecker:
    def __init__(self, cfg: SafetyConfig, fetch: Fetch = http_get_json):
        self.cfg = cfg
        self.fetch = fetch
        self._cache: dict[str, tuple[float, SafetyReport]] = {}

    def check_contract(self, chain: str, address: str) -> SafetyReport:
        key = f"{chain}:{address.lower()}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cfg.cache_minutes * 60:
            return cached[1]
        chain_id = CHAIN_IDS.get(chain)
        if chain_id is None:
            report = SafetyReport(subject=key, verdict=UNKNOWN)
            report.critical.append(f"Unsupported chain '{chain}' (supported: {', '.join(CHAIN_IDS)})")
            return report.finalize()
        goplus = pairs = None
        try:
            data = self.fetch(GOPLUS_URL.format(chain_id=chain_id), {"contract_addresses": address.lower()})
            if str(data.get("code")) == "1":
                goplus = (data.get("result") or {}).get(address.lower(), {})
        except Exception as exc:  # any failure leaves the token unverified
            log.warning("GoPlus lookup failed for %s: %s", key, exc)
        try:
            data = self.fetch(DEXSCREENER_URL.format(address=address), None)
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == chain]
        except Exception as exc:
            log.warning("DexScreener lookup failed for %s: %s", key, exc)
        report = evaluate_contract(chain, address, goplus, pairs, self.cfg)
        if report.verdict != UNKNOWN:
            self._cache[key] = (time.time(), report)
        return report

    def check_market(self, symbol: str, ticker: dict, history_bars: int) -> SafetyReport:
        """Exchange-level sanity checks that catch thin, illiquid or newly listed markets."""
        report = SafetyReport(subject=symbol)
        quote_volume = _num(ticker.get("quoteVolume"))
        bid, ask = _num(ticker.get("bid")), _num(ticker.get("ask"))
        spread = (ask - bid) / ask if bid > 0 and ask > 0 else float("inf")
        report.info.update(quote_volume_24h=f"{quote_volume:,.0f}", spread=f"{spread:.3%}", history_bars=history_bars)
        if quote_volume < self.cfg.min_quote_volume_24h:
            report.critical.append(f"24h volume {quote_volume:,.0f} below {self.cfg.min_quote_volume_24h:,.0f}")
        if spread > self.cfg.max_spread_pct:
            report.critical.append(f"Spread {spread:.2%} too wide")
        if history_bars < self.cfg.min_history_bars:
            report.warnings.append(f"Only {history_bars} bars of history; market may be newly listed")
        return report.finalize()
