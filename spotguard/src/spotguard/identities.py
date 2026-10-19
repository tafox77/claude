"""Asset identities.

* CEX markets are identified by venue + immutable market/base/quote IDs (e.g. coinbase BTC-USD).
* DEX assets are identified by chain + canonical contract/mint address.

A ticker symbol is never an identity: two tokens can share a symbol, and an exchange ticker like
"BTC" is not an on-chain contract. Bitcoin has no ERC-20/SPL identity here and can only be traded as
a CEX market.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from Crypto.Hash import keccak
from solders.pubkey import Pubkey


class IdentityError(ValueError):
    pass


class UnsupportedChain(IdentityError):
    pass


class ChainKind(StrEnum):
    EVM = "evm"
    SOLANA = "solana"


@dataclass(frozen=True)
class ChainSpec:
    key: str
    kind: ChainKind
    name: str
    goplus_chain_id: str | None  # None: GoPlus has a separate endpoint (Solana)


# Chains with an implemented adapter. Anything else is unsupported and blocks new exposure.
SUPPORTED_CHAINS: dict[str, ChainSpec] = {
    "solana": ChainSpec("solana", ChainKind.SOLANA, "Solana", None),
    "evm:1": ChainSpec("evm:1", ChainKind.EVM, "Ethereum", "1"),
    "evm:56": ChainSpec("evm:56", ChainKind.EVM, "BNB Smart Chain", "56"),
    "evm:8453": ChainSpec("evm:8453", ChainKind.EVM, "Base", "8453"),
    "evm:42161": ChainSpec("evm:42161", ChainKind.EVM, "Arbitrum One", "42161"),
}


def chain_spec(chain: str) -> ChainSpec:
    spec = SUPPORTED_CHAINS.get(chain)
    if spec is None:
        raise UnsupportedChain(f"Unsupported chain {chain!r}; supported: {', '.join(SUPPORTED_CHAINS)}")
    return spec


_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}\Z")


def _keccak_hex(data: bytes) -> str:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.hexdigest()


def evm_checksum(address: str) -> str:
    """EIP-55 mixed-case checksum encoding."""
    lower = address[2:].lower()
    digest = _keccak_hex(lower.encode())
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(lower))


def parse_evm_address(value: str) -> str:
    """Canonical (lowercase) EVM address. Mixed-case input must carry a valid EIP-55 checksum."""
    if not isinstance(value, str) or not _EVM_RE.match(value):
        raise IdentityError(f"Not an EVM address: {value!r}")
    body = value[2:]
    if body != body.lower() and body != body.upper() and evm_checksum(value) != value:
        raise IdentityError(f"EVM address checksum mismatch: {value}")
    if int(body, 16) == 0:
        raise IdentityError("Zero address is not a token")
    return value.lower()


def parse_solana_address(value: str) -> str:
    """Canonical base58 Solana address (case-sensitive; must round-trip exactly)."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise IdentityError(f"Not a Solana address: {value!r}")
    try:
        key = Pubkey.from_string(value)
    except (ValueError, TypeError) as exc:
        raise IdentityError(f"Not a Solana address: {value!r}") from exc
    if str(key) != value:
        raise IdentityError(f"Non-canonical Solana address encoding: {value!r}")
    return value


@dataclass(frozen=True)
class DexAssetId:
    chain: str
    address: str

    def __post_init__(self) -> None:
        spec = chain_spec(self.chain)
        canonical = parse_solana_address(self.address) if spec.kind is ChainKind.SOLANA else parse_evm_address(self.address)
        object.__setattr__(self, "address", canonical)

    @property
    def key(self) -> str:
        return f"dex:{self.chain}:{self.address}"

    @property
    def spec(self) -> ChainSpec:
        return chain_spec(self.chain)


_MARKET_RE = re.compile(r"^[A-Z0-9]{2,15}-[A-Z0-9]{2,15}\Z")


@dataclass(frozen=True)
class CexMarketId:
    venue: str
    market_id: str
    base_id: str
    quote_id: str

    def __post_init__(self) -> None:
        if self.venue != self.venue.lower() or not self.venue.isalnum():
            raise IdentityError(f"Venue must be a lowercase id: {self.venue!r}")
        if not _MARKET_RE.match(self.market_id):
            raise IdentityError(f"Bad market id: {self.market_id!r}")
        if self.market_id != f"{self.base_id}-{self.quote_id}":
            raise IdentityError(f"{self.market_id} does not match base/quote {self.base_id}/{self.quote_id}")

    @property
    def key(self) -> str:
        return f"cex:{self.venue}:{self.market_id}"


def parse_asset_key(key: str) -> CexMarketId | DexAssetId:
    parts = key.split(":")
    if parts[0] == "cex" and len(parts) == 3:
        base, _, quote = parts[2].partition("-")
        return CexMarketId(parts[1], parts[2], base, quote)
    if parts[0] == "dex" and len(parts) >= 3:
        return DexAssetId(":".join(parts[1:-1]), parts[-1])
    raise IdentityError(f"Unrecognised asset key {key!r}")


# ----- CEX market validation ------------------------------------------------------------


@dataclass(frozen=True)
class ProductRules:
    """Precision and size limits for a validated CEX market, from the venue's own product listing."""

    market: CexMarketId
    base_increment: str
    quote_increment: str
    price_increment: str
    base_min_size: str
    quote_min_size: str
    validated_at: datetime


def validate_coinbase_product(requested: CexMarketId, product: dict, now: datetime) -> ProductRules:
    """Check a Coinbase Advanced Trade product listing against the requested market.

    Blocks on aliases (Coinbase serves BTC-USD and BTC-USDC as aliases of each other), status other
    than online, disabled/cancel-only/limit-only/view-only trading, and any id mismatch.
    """
    problems = []
    if product.get("product_id") != requested.market_id:
        problems.append(f"product_id {product.get('product_id')!r} != requested {requested.market_id}")
    if product.get("base_currency_id") != requested.base_id or product.get("quote_currency_id") != requested.quote_id:
        problems.append("base/quote currency ids differ from the requested market")
    if product.get("product_type") != "SPOT":
        problems.append(f"product_type {product.get('product_type')!r} is not SPOT")
    if product.get("alias"):
        problems.append(f"{requested.market_id} is an alias of {product.get('alias')}; use the primary market id")
    if product.get("status") != "online":
        problems.append(f"status {product.get('status')!r}")
    for flag in ("trading_disabled", "is_disabled", "cancel_only", "limit_only", "view_only", "auction_mode"):
        value = product.get(flag)
        if value is not False:
            problems.append(f"{flag}={value!r} (expected false)")
    fields = ("base_increment", "quote_increment", "price_increment", "base_min_size", "quote_min_size")
    missing = [f for f in fields if not product.get(f)]
    if missing:
        problems.append(f"missing precision fields {missing}")
    if problems:
        raise IdentityError(f"{requested.key} failed validation: " + "; ".join(problems))
    return ProductRules(requested, *(str(product[f]) for f in fields), validated_at=now)
