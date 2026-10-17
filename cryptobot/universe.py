"""Automatic coin discovery: finds active markets instead of relying on a fixed symbol list.

* Exchanges: every spot market in your quote currency, ranked by 24h volume.
* Solana: trending and highest-volume pools on GeckoTerminal, which is where most meme coins
  trade.

Every candidate passes cheap liquidity/volume/age filters and then the full safety screen
(scam, rug-pull and impersonation checks) before the bot will watch it. Most new meme coins
fail, which is the point.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from cryptobot.config import Config
from cryptobot.exchange import market_quote_volume
from cryptobot.safety import SafetyChecker, SafetyReport, _num, http_get_json

log = logging.getLogger(__name__)

STABLECOINS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDP", "BUSD", "USDD", "PYUSD", "USDE", "USD1", "RLUSD",
    "EUR", "EURC", "EURT", "GBP", "AEUR", "USTC", "UST", "WSOL",
}  # fmt: skip
LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR|\d+[LS])$")
GECKO_SOLANA = "https://api.geckoterminal.com/api/v2/networks/solana"


@dataclass
class Candidate:
    symbol: str
    contract: dict[str, str] | None = None
    ticker: dict | None = None
    note: str = ""


def exchange_candidates(exchange, quote: str, limit: int) -> list[Candidate]:
    exchange.load_markets()
    try:
        tickers = exchange.fetch_tickers()
    except Exception as exc:
        log.warning("fetch_tickers failed (%s); ranking by market listing volume", exc)
        tickers = {}
    rows = []
    for symbol, market in exchange.markets.items():
        if not market.get("spot") or market.get("active") is False or market.get("quote") != quote:
            continue
        base = market.get("base", "")
        if base in STABLECOINS or LEVERAGED.search(base):
            continue
        ticker = tickers.get(symbol) or {}
        volume = float(ticker.get("quoteVolume") or 0) or market_quote_volume(market)
        rows.append((volume, symbol, None))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [
        Candidate(symbol, ticker=ticker, note=f"24h volume {vol:,.0f} {quote}") for vol, symbol, ticker in rows[:limit]
    ]


def _age_days(iso: str | None, now: float) -> float:
    if not iso:
        return 0.0
    try:
        return (now - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) / 86_400
    except ValueError:
        return 0.0


def solana_candidates(cfg: Config, limit: int, fetch=http_get_json, now: float | None = None) -> list[Candidate]:
    now = now if now is not None else time.time()
    quote = cfg.symbols[0].split("/")[1] if cfg.symbols else "USDC"
    pools: list[dict] = []
    for path, params in (("/trending_pools", {"page": 1}), ("/pools", {"page": 1, "sort": "h24_volume_usd_desc"})):
        try:
            pools += (fetch(f"{GECKO_SOLANA}{path}", params) or {}).get("data") or []
        except Exception as exc:
            log.warning("GeckoTerminal %s failed: %s", path, exc)
    seen: set[str] = set()
    taken = {c["address"]: s for s, c in cfg.token_contracts.items() if c.get("chain") == "solana"}
    out = []
    for pool in pools:
        attrs = pool.get("attributes") or {}
        base_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        mint = base_id.split("_", 1)[1] if base_id.startswith("solana_") else ""
        symbol = str(attrs.get("name", "?")).split(" / ")[0].strip().upper()
        if not mint or mint in seen or symbol in STABLECOINS:
            continue
        seen.add(mint)
        liquidity = _num(attrs.get("reserve_in_usd"))
        volume = _num((attrs.get("volume_usd") or {}).get("h24"))
        age = _age_days(attrs.get("pool_created_at"), now)
        s = cfg.safety
        if liquidity < s.min_liquidity_usd or volume < s.min_volume_24h_usd or age < s.min_pair_age_days:
            continue
        name = taken.get(mint) or f"{symbol}/{quote}"
        if name in cfg.token_contracts and cfg.token_contracts[name]["address"] != mint:
            name = f"{symbol}-{mint[:4]}/{quote}"  # several tokens share a ticker; keep them apart
        out.append(
            Candidate(
                name,
                contract={"chain": "solana", "address": mint},
                note=f"liquidity ${liquidity:,.0f}, 24h volume ${volume:,.0f}, pool age {age:.0f}d",
            )
        )
        if len(out) >= limit:
            break
    return out


class Universe:
    """Keeps the bot's watch list filled with the most active markets that pass the safety screen."""

    def __init__(self, cfg: Config, checker: SafetyChecker, discover, ticker_fn=None, clock=time.time):
        self.cfg = cfg
        self.checker = checker
        self.discover = discover  # callable(limit) -> list[Candidate]
        self.ticker_fn = ticker_fn  # live ticker for exchange candidates
        self.clock = clock
        self.last_refresh: float | None = None
        self.symbols: list[str] = []

    def screen(self) -> list[tuple[Candidate, SafetyReport]]:
        results = []
        for cand in self.discover(self.cfg.scan.candidates):
            if cand.contract:
                report = self.checker.check_contract(cand.contract["chain"], cand.contract["address"])
            else:
                ticker = cand.ticker
                if ticker is None and self.ticker_fn is not None:
                    try:
                        ticker = self.ticker_fn(cand.symbol)
                    except Exception as exc:
                        log.warning("Ticker for %s failed: %s", cand.symbol, exc)
                if not ticker:
                    continue
                report = self.checker.check_market(cand.symbol, ticker, self.cfg.safety.min_history_bars)
            results.append((cand, report))
        return results

    def refresh(self, force: bool = False) -> list[str]:
        due = self.last_refresh is None or self.clock() - self.last_refresh >= self.cfg.scan.refresh_minutes * 60
        if not force and not due:
            return self.symbols
        self.last_refresh = self.clock()
        selected = []
        for cand, report in self.screen():
            if not report.tradable or cand.symbol in self.cfg.symbols:
                continue
            if cand.contract:
                self.cfg.token_contracts[cand.symbol] = cand.contract
            selected.append(cand.symbol)
            if len(selected) >= self.cfg.scan.max_symbols:
                break
        self.symbols = selected
        log.info("Scanner watching %d extra markets: %s", len(selected), ", ".join(selected) or "none passed")
        return selected
