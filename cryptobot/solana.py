"""Solana support: SPL token scam screening, DEX market data, and Jupiter swaps.

* Token screening reads the mint straight from a Solana RPC node (mint/freeze authority,
  Token-2022 extensions, holder concentration), adds pool data from DexScreener, LP-lock and
  risk flags from RugCheck, and simulates a sell through Jupiter to catch honeypots.
* Prices come from GeckoTerminal candles for the token's most liquid pool.
* Live trades are Jupiter swaps signed locally with your wallet key. Nothing leaves your
  machine except the signed transaction.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from functools import partial
from pathlib import Path

from cryptobot.config import Config, SafetyConfig, SolanaConfig
from cryptobot.safety import (
    CANONICAL_TOKENS,
    DEXSCREENER_URL,
    UNKNOWN,
    WELL_KNOWN_SYMBOLS,
    SafetyReport,
    _evaluate_pairs,
    _num,
    http_get_json,
    http_post_json,
)

log = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/solana"
GECKO_TIMEFRAMES = {
    "1m": ("minute", 1),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "4h": ("hour", 4),
    "12h": ("hour", 12),
    "1d": ("day", 1),
}
BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SELL_TEST_USD = 100.0


def is_solana_address(value: str) -> bool:
    return bool(BASE58_ADDRESS.match(value))


def solana_mint(cfg: Config, symbol: str) -> str:
    """Mint address for a symbol: from token_contracts, the known-token list, or the symbol itself."""
    contract = cfg.token_contracts.get(symbol)
    if contract and contract.get("chain") == "solana":
        return contract["address"]
    base = symbol.split("/")[0]
    if is_solana_address(base):
        return base
    canonical = CANONICAL_TOKENS["solana"].get(base.upper())
    if canonical:
        return canonical
    raise ValueError(
        f"Don't know the Solana mint for {symbol}. Add it under token_contracts, "
        f"or write the symbol as <mint address>/USDC"
    )


# ----- RPC and Jupiter clients -------------------------------------------------------


class RpcError(RuntimeError):
    pass


class SolanaRPC:
    def __init__(self, url: str, post=http_post_json):
        self.url = url
        self.post = post

    def call(self, method: str, params: list):
        data = self.post(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if data.get("error"):
            raise RpcError(f"{method}: {data['error']}")
        return data.get("result")

    def mint_info(self, mint: str) -> tuple[str, dict] | None:
        """(owning program, parsed mint info) or None if the account isn't a token mint."""
        value = (self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}).get("value")
        if not value:
            return None
        parsed = (value.get("data") or {}).get("parsed") if isinstance(value.get("data"), dict) else None
        if not parsed or parsed.get("type") != "mint":
            return value.get("owner", ""), {}
        return value.get("owner", ""), parsed.get("info", {})

    def largest_holders(self, mint: str, supply: float) -> list[dict]:
        """Top token accounts with their owner, share of supply, and whether the owner is a wallet.

        Pool vaults and lockers are owned by programs, so owners whose own account belongs to
        a program (not the system program) are marked is_wallet=False.
        """
        accounts = (self.call("getTokenLargestAccounts", [mint]) or {}).get("value") or []
        if not accounts or supply <= 0:
            return []
        addresses = [a["address"] for a in accounts]
        infos = (self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}]) or {}).get("value") or []
        owners = []
        for info in infos:
            parsed = (
                ((info or {}).get("data") or {}).get("parsed", {}) if isinstance((info or {}).get("data"), dict) else {}
            )
            owners.append(parsed.get("info", {}).get("owner", ""))
        unique = sorted({o for o in owners if o})
        owner_accounts = (
            (self.call("getMultipleAccounts", [unique, {"encoding": "base64"}]) or {}).get("value") or []
            if unique
            else []
        )
        program_of = {
            o: (acc or {}).get("owner", SYSTEM_PROGRAM) for o, acc in zip(unique, owner_accounts, strict=False)
        }
        holders = []
        for acc, owner in zip(accounts, owners, strict=False):
            amount = _num(acc.get("uiAmountString", acc.get("uiAmount")))
            holders.append(
                {
                    "owner": owner,
                    "percent": amount / supply,
                    "is_wallet": bool(owner) and owner != INCINERATOR and program_of.get(owner) == SYSTEM_PROGRAM,
                    "burned": owner == INCINERATOR,
                }
            )
        return holders

    def token_balance(self, owner: str, mint: str) -> float:
        if mint == WSOL:
            return self.sol_balance(owner)
        result = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
        total = 0.0
        for item in result.get("value") or []:
            amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += _num(amount.get("uiAmountString", amount.get("uiAmount")))
        return total

    def sol_balance(self, owner: str) -> float:
        return _num((self.call("getBalance", [owner]) or {}).get("value")) / 1e9

    def send_transaction(self, tx_b64: str) -> str:
        return self.call(
            "sendTransaction", [tx_b64, {"encoding": "base64", "maxRetries": 3, "preflightCommitment": "confirmed"}]
        )

    def confirm(self, signature: str, timeout: float = 60, sleep=time.sleep) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            statuses = (self.call("getSignatureStatuses", [[signature]]) or {}).get("value") or [None]
            status = statuses[0]
            if status:
                if status.get("err"):
                    raise RpcError(f"Transaction {signature} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            sleep(1.0)
        return False


class Jupiter:
    """Jupiter swap aggregator (quote + swap transaction)."""

    def __init__(self, cfg: SolanaConfig, fetch=None, post=None):
        self.cfg = cfg
        headers = {"x-api-key": os.environ["JUPITER_API_KEY"]} if os.environ.get("JUPITER_API_KEY") else None
        self.fetch = fetch or partial(http_get_json, headers=headers)
        self.post = post or partial(http_post_json, headers=headers)

    def quote(self, input_mint: str, output_mint: str, amount: int) -> dict | None:
        """Best route for `amount` base units, or None if no route exists (can't be traded)."""
        data = self.fetch(
            f"{self.cfg.jupiter_url}/quote",
            {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount)),
                "slippageBps": self.cfg.slippage_bps,
            },
        )
        if "outAmount" in data:
            return data
        if data.get("error") or data.get("errorCode"):
            return None
        raise RuntimeError(f"Unexpected Jupiter response: {str(data)[:200]}")

    def swap_transaction(self, quote: dict, user: str) -> str:
        data = self.post(
            f"{self.cfg.jupiter_url}/swap",
            {
                "quoteResponse": quote,
                "userPublicKey": user,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": self.cfg.max_priority_fee_lamports,
                        "priorityLevel": "high",
                    }
                },
            },
        )
        if "swapTransaction" not in data:
            raise RuntimeError(f"Jupiter swap failed: {str(data)[:200]}")
        return data["swapTransaction"]


# ----- token screening -----------------------------------------------------------------


def _extension_flags(report: SafetyReport, extensions: list[dict], cfg: SafetyConfig) -> None:
    for ext in extensions or []:
        name = ext.get("extension", "")
        state = ext.get("state") or {}
        if name == "permanentDelegate" and state.get("delegate"):
            report.critical.append("Permanent delegate: someone can move or burn tokens out of your wallet")
        elif name == "nonTransferable":
            report.critical.append("Token is non-transferable")
        elif name == "defaultAccountState" and state.get("accountState") == "frozen":
            report.critical.append("New token accounts start frozen: buyers can't move or sell")
        elif name == "transferFeeConfig":
            bps = max(
                _num((state.get("newerTransferFee") or {}).get("transferFeeBasisPoints")),
                _num((state.get("olderTransferFee") or {}).get("transferFeeBasisPoints")),
            )
            fee = bps / 10_000
            report.info["transfer_fee"] = f"{fee:.2%}"
            if fee >= 0.5:
                report.critical.append(f"Extreme transfer fee ({fee:.0%})")
            elif fee > cfg.max_sell_tax:
                report.warnings.append(f"High transfer fee ({fee:.1%})")
            if state.get("transferFeeConfigAuthority"):
                report.warnings.append("Transfer fee can be raised by its authority")
        elif name == "transferHook" and state.get("programId"):
            report.warnings.append("Transfer hook runs custom code on every transfer (can be used to block sells)")
        elif name == "mintCloseAuthority" and state.get("closeAuthority"):
            report.warnings.append("Mint can be closed by its authority")
        elif name in ("pausableConfig", "pausable"):
            report.warnings.append("Transfers can be paused")


def evaluate_solana_token(
    mint: str,
    mint_account: tuple[str, dict] | None,
    holders: list[dict] | None,
    pairs: list[dict] | None,
    rugcheck: dict | None,
    sell_route: bool | None,
    sell_impact: float | None,
    cfg: SafetyConfig,
    now_ms: int | None = None,
) -> SafetyReport:
    """Turn raw on-chain and API data about an SPL token into a report. Pure function."""
    report = SafetyReport(subject=f"solana:{mint}")
    if mint_account is None or pairs is None:
        report.verdict = UNKNOWN
        report.critical.append("Could not fetch on-chain or market data; refusing to trade unverified token")
        return report.finalize()

    program, info = mint_account
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or not info:
        report.critical.append("Address is not an SPL token mint")
        return report.finalize()

    own_pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    symbol = str(((own_pairs[0] if own_pairs else {}).get("baseToken") or {}).get("symbol") or "").upper()
    report.subject = f"{symbol or '?'} (solana:{mint})"
    report.info["program"] = "Token-2022" if program == TOKEN_2022_PROGRAM else "SPL Token"
    canonical = CANONICAL_TOKENS["solana"]
    trusted = mint in canonical.values()

    if symbol in canonical and canonical[symbol] != mint:
        report.critical.append(f"Impersonation: real {symbol} on Solana is {canonical[symbol]}, not this mint")
    elif symbol in WELL_KNOWN_SYMBOLS and not trusted:
        report.warnings.append(f"Uses well-known symbol {symbol}; verify it's the official mint")

    if not trusted:
        if info.get("mintAuthority"):
            report.critical.append("Mint authority is active: the creator can print unlimited new supply")
        if info.get("freezeAuthority"):
            report.critical.append("Freeze authority is active: the creator can freeze your tokens so you can't sell")
        _extension_flags(report, info.get("extensions") or [], cfg)

        if holders is None:
            report.warnings.append("Holder concentration not verified")
        else:
            wallets = [h for h in holders if h.get("is_wallet")]
            top10 = sum(h["percent"] for h in wallets[:10])
            report.info["top10_wallets"] = f"{top10:.1%}"
            if top10 > cfg.max_top10_holder_pct:
                report.warnings.append(f"Top 10 wallets hold {top10:.0%} of supply (dump risk)")
            if wallets and wallets[0]["percent"] > 0.2:
                report.warnings.append(f"One wallet holds {wallets[0]['percent']:.0%} of supply")

        if rugcheck is None:
            report.warnings.append("RugCheck unavailable: liquidity lock not verified")
        else:
            for risk in rugcheck.get("risks") or []:
                text = f"RugCheck: {risk.get('name', '?')}"
                if risk.get("description"):
                    text += f" ({risk['description']})"
                level = str(risk.get("level", "")).lower()
                if level == "danger":
                    report.critical.append(text)
                elif level == "warn":
                    report.warnings.append(text)
            lp_locked = rugcheck.get("lpLockedPct")
            if lp_locked is not None:
                report.info["lp_locked"] = f"{_num(lp_locked):.1f}%"
                if _num(lp_locked) < cfg.min_lp_locked_pct * 100:
                    report.warnings.append(f"Only {_num(lp_locked):.0f}% of liquidity is locked or burned (rug risk)")

    if sell_route is False:
        report.critical.append("No sell route on Jupiter: you may not be able to sell this token")
    elif sell_route is None:
        report.warnings.append("Sell route not verified")
    elif sell_impact is not None:
        report.info["sell_impact_$100"] = f"{sell_impact:.2%}"
        if sell_impact > 0.05:
            report.warnings.append(f"Selling just ${SELL_TEST_USD:.0f} moves the price {sell_impact:.1%}")

    if any(p.get("dexId") == "pumpfun" for p in own_pairs):
        report.warnings.append("Still trading on the pump.fun bonding curve (not yet on a real DEX pool)")
    _evaluate_pairs(report, mint, pairs, cfg, now_ms, case_sensitive=True)
    return report.finalize()


def check_solana_token(
    mint: str, cfg: SafetyConfig, sol: SolanaConfig, fetch=http_get_json, post=http_post_json
) -> SafetyReport:
    rpc = SolanaRPC(sol.rpc_url, post)
    mint_account = pairs = holders = rugcheck = sell_route = sell_impact = None
    try:
        mint_account = rpc.mint_info(mint)
        if mint_account is None:
            report = SafetyReport(subject=f"solana:{mint}")
            report.critical.append("No account exists at this address")
            return report.finalize()
    except Exception as exc:
        log.warning("Solana RPC lookup failed for %s: %s", mint, exc)
    try:
        data = fetch(DEXSCREENER_URL.format(address=mint), None)
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
    except Exception as exc:
        log.warning("DexScreener lookup failed for %s: %s", mint, exc)
    info = mint_account[1] if mint_account else {}
    decimals = int(_num(info.get("decimals")))
    supply = _num(info.get("supply")) / 10**decimals if info else 0.0
    try:
        holders = rpc.largest_holders(mint, supply)
    except Exception as exc:
        log.warning("Holder lookup failed for %s: %s", mint, exc)
    try:
        rugcheck = fetch(RUGCHECK_URL.format(mint=mint), None)
    except Exception as exc:
        log.warning("RugCheck lookup failed for %s: %s", mint, exc)
    price = next(
        (_num(p.get("priceUsd")) for p in pairs or [] if (p.get("baseToken") or {}).get("address") == mint), 0.0
    )
    if price > 0 and info and mint != QUOTE_MINTS["USDC"]:
        try:
            amount = int(SELL_TEST_USD / price * 10**decimals)
            quote = Jupiter(sol, fetch=fetch).quote(mint, QUOTE_MINTS["USDC"], amount)
            sell_route = quote is not None
            if quote is not None:
                sell_impact = abs(_num(quote.get("priceImpactPct")))
        except Exception as exc:
            log.warning("Jupiter sell simulation failed for %s: %s", mint, exc)
    elif mint == QUOTE_MINTS["USDC"]:
        sell_route = True
    return evaluate_solana_token(mint, mint_account, holders, pairs, rugcheck, sell_route, sell_impact, cfg)


# ----- market data ---------------------------------------------------------------------


class SolanaMarketData:
    """Candles from GeckoTerminal and prices from DexScreener for SPL tokens priced in USD."""

    def __init__(self, cfg: Config, fetch=http_get_json, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.fetch = fetch
        self.clock = clock
        self.sleep = sleep
        self._pools: dict[str, str] = {}
        self._candles: dict[tuple[str, str], list[list[float]]] = {}
        self._last_gecko = 0.0

    def mint(self, symbol: str) -> str:
        return solana_mint(self.cfg, symbol)

    def _pairs(self, mint: str) -> list[dict]:
        data = self.fetch(DEXSCREENER_URL.format(address=mint), None)
        return [
            p
            for p in (data.get("pairs") or [])
            if p.get("chainId") == "solana"
            and mint in ((p.get("baseToken") or {}).get("address"), (p.get("quoteToken") or {}).get("address"))
        ]

    def pool(self, symbol: str) -> str:
        if symbol not in self._pools:
            pairs = self._pairs(self.mint(symbol))
            if not pairs:
                raise ValueError(f"No Solana DEX pool found for {symbol}")
            best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")))
            self._pools[symbol] = best["pairAddress"]
        return self._pools[symbol]

    def _gecko(self, path: str, params: dict) -> dict:
        # Free GeckoTerminal API allows ~30 calls/minute.
        wait = self._last_gecko + 2.1 - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last_gecko = self.clock()
        return self.fetch(f"{GECKO_URL}{path}", params)

    def _candles_before(self, symbol: str, timeframe: str, before_s: int | None, limit: int) -> list[list[float]]:
        if timeframe not in GECKO_TIMEFRAMES:
            raise ValueError(f"Timeframe {timeframe} not available for Solana (use {', '.join(GECKO_TIMEFRAMES)})")
        period, aggregate = GECKO_TIMEFRAMES[timeframe]
        params = {"aggregate": aggregate, "limit": min(limit, 1000), "currency": "usd", "token": self.mint(symbol)}
        if before_s:
            params["before_timestamp"] = before_s
        data = self._gecko(f"/pools/{self.pool(symbol)}/ohlcv/{period}", params)
        rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted([[int(r[0]) * 1000, *map(float, r[1:6])] for r in rows], key=lambda r: r[0])

    @staticmethod
    def _tf_ms(timeframe: str) -> int:
        period, aggregate = GECKO_TIMEFRAMES[timeframe]
        return aggregate * {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}[period]

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        tf_ms = self._tf_ms(timeframe)
        now_ms = int(self.clock() * 1000)
        cached = self._candles.get((symbol, timeframe))
        # Only hit the API once a new candle should have closed.
        if cached and now_ms < cached[-1][0] + 2 * tf_ms:
            return cached
        rows = [r for r in self._candles_before(symbol, timeframe, None, limit) if r[0] + tf_ms <= now_ms]
        self._candles[(symbol, timeframe)] = rows
        return rows

    def fetch_history(self, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None):
        rows: dict[int, list[float]] = {}
        before = int((until_ms or self.clock() * 1000) / 1000)
        while True:
            batch = self._candles_before(symbol, timeframe, before, 1000)
            if not batch:
                break
            for r in batch:
                rows[r[0]] = r
            earliest = batch[0][0]
            if earliest <= since_ms or len(batch) < 2:
                break
            before = earliest // 1000
        return [rows[t] for t in sorted(rows) if t >= since_ms]

    def fetch_ticker(self, symbol: str) -> dict:
        mint = self.mint(symbol)
        pairs = self._pairs(mint)
        price = 0.0
        best = 0.0
        for p in pairs:
            liquidity = _num((p.get("liquidity") or {}).get("usd"))
            if liquidity <= best:
                continue
            if (p.get("baseToken") or {}).get("address") == mint:
                candidate = _num(p.get("priceUsd"))
            else:
                native = _num(p.get("priceNative"))
                candidate = _num(p.get("priceUsd")) / native if native else 0.0
            if candidate > 0:
                price, best = candidate, liquidity
        volume = sum(_num((p.get("volume") or {}).get("h24")) for p in pairs)
        # AMM pools have no order book; trade cost is checked per order via price impact.
        return {"last": price, "bid": price, "ask": price, "quoteVolume": volume}


# ----- live trading --------------------------------------------------------------------


def load_keypair():
    """Wallet from SOLANA_PRIVATE_KEY (base58, as exported by Phantom/Solflare) or SOLANA_KEYPAIR_PATH."""
    from solders.keypair import Keypair

    secret = os.environ.get("SOLANA_PRIVATE_KEY")
    if secret:
        return Keypair.from_base58_string(secret.strip())
    path = os.environ.get("SOLANA_KEYPAIR_PATH")
    if path:
        return Keypair.from_bytes(bytes(json.loads(Path(path).expanduser().read_text())))
    raise RuntimeError("Set SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH for live Solana trading")


class JupiterBroker:
    """Live spot trading on Solana through Jupiter swaps, settled in USDC or USDT."""

    def __init__(self, cfg: Config, market: SolanaMarketData, keypair=None, rpc=None, jupiter=None, sleep=time.sleep):
        self.cfg = cfg
        self.market = market
        self.keypair = keypair or load_keypair()
        self.owner = str(self.keypair.pubkey())
        self.rpc = rpc or SolanaRPC(cfg.solana.rpc_url)
        self.jupiter = jupiter or Jupiter(cfg.solana)
        self.sleep = sleep
        quotes = {s.split("/")[1] for s in cfg.symbols} or {"USDC"}
        if len(quotes) != 1 or not quotes <= set(QUOTE_MINTS):
            raise ValueError("Solana symbols must all be quoted in USDC or all in USDT")
        self.quote_mint = QUOTE_MINTS[quotes.pop()]
        self._decimals: dict[str, int] = {}
        log.info("Solana wallet %s", self.owner)

    def _decimals_of(self, mint: str) -> int:
        if mint not in self._decimals:
            account = self.rpc.mint_info(mint)
            self._decimals[mint] = int(_num((account or ("", {}))[1].get("decimals"), 9))
        return self._decimals[mint]

    def _swap(self, quote: dict) -> bool:
        from solders.transaction import VersionedTransaction

        raw = VersionedTransaction.from_bytes(base64.b64decode(self.jupiter.swap_transaction(quote, self.owner)))
        signed = VersionedTransaction(raw.message, [self.keypair])
        signature = self.rpc.send_transaction(base64.b64encode(bytes(signed)).decode())
        log.info("Sent swap %s", signature)
        return self.rpc.confirm(signature, sleep=self.sleep)

    def _has_fee_balance(self) -> bool:
        sol = self.rpc.sol_balance(self.owner)
        if sol < self.cfg.solana.min_sol_balance:
            log.error("Wallet has %.4f SOL; need %.4f SOL for transaction fees", sol, self.cfg.solana.min_sol_balance)
            return False
        return True

    def buy(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        mint = self.market.mint(symbol)
        if not self._has_fee_balance():
            return 0.0, 0.0
        spend = qty * price
        quote = self.jupiter.quote(self.quote_mint, mint, int(spend * 10 ** self._decimals_of(self.quote_mint)))
        if quote is None:
            log.warning("%s: no Jupiter route to buy", symbol)
            return 0.0, 0.0
        impact = abs(_num(quote.get("priceImpactPct")))
        if impact > self.cfg.solana.max_price_impact:
            log.warning("%s: buy skipped, price impact %.2f%% is above the limit", symbol, 100 * impact)
            return 0.0, 0.0
        before_token = self.rpc.token_balance(self.owner, mint)
        before_cash = self.rpc.token_balance(self.owner, self.quote_mint)
        if not self._swap(quote):
            log.error("%s: buy not confirmed in time; check your wallet", symbol)
            return 0.0, 0.0
        filled = self.rpc.token_balance(self.owner, mint) - before_token
        spent = before_cash - self.rpc.token_balance(self.owner, self.quote_mint)
        return (filled, spent / filled) if filled > 0 else (0.0, 0.0)

    def sell(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        mint = self.market.mint(symbol)
        held = self.rpc.token_balance(self.owner, mint)
        if mint == WSOL:  # never sell the SOL needed for fees
            held = max(0.0, held - self.cfg.solana.min_sol_balance)
        qty = min(qty, held)
        if qty <= 0:
            return 0.0, 0.0
        quote = self.jupiter.quote(mint, self.quote_mint, int(qty * 10 ** self._decimals_of(mint)))
        if quote is None:
            log.error("%s: no Jupiter route to sell!", symbol)
            return 0.0, 0.0
        impact = abs(_num(quote.get("priceImpactPct")))
        if impact > self.cfg.solana.max_price_impact:
            log.warning("%s: selling anyway despite %.2f%% price impact", symbol, 100 * impact)
        before_token = self.rpc.token_balance(self.owner, mint)
        before_cash = self.rpc.token_balance(self.owner, self.quote_mint)
        if not self._swap(quote):
            log.error("%s: sell not confirmed in time; check your wallet", symbol)
            return 0.0, 0.0
        sold = before_token - self.rpc.token_balance(self.owner, mint)
        received = self.rpc.token_balance(self.owner, self.quote_mint) - before_cash
        return (sold, received / sold) if sold > 0 else (0.0, 0.0)

    def free_cash(self) -> float:
        return self.rpc.token_balance(self.owner, self.quote_mint)
