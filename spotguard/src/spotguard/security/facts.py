"""Independent facts gathered from RPC nodes, pool verification and transaction simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from spotguard.security.schema import Evidence


@dataclass(frozen=True)
class PoolFacts:
    """Independently verified pool: owning program/factory, depth and creation time."""

    evidence: Evidence
    pool_address: str
    owner_program: str  # Solana pool program id, or EVM factory address
    liquidity_usd: Decimal
    created_at: datetime
    concentrated: bool


@dataclass(frozen=True)
class SellSimulation:
    """Result of simulating a small sell of the token (balance deltas, not just a route quote)."""

    evidence: Evidence
    succeeded: bool
    expected_out: Decimal
    simulated_out: Decimal

    @property
    def effective_cost(self) -> Decimal:
        if self.expected_out <= 0:
            return Decimal(1)
        return max(Decimal(0), 1 - self.simulated_out / self.expected_out)


@dataclass(frozen=True)
class EvmRpcFacts:
    evidence: Evidence
    has_code: bool
    implementation: str | None  # EIP-1967 implementation slot (None when zero)
    admin: str | None  # EIP-1967 admin slot
    decimals: int | None
    symbol: str | None


@dataclass(frozen=True)
class SolanaMintFacts:
    evidence: Evidence
    owner_program: str
    mint_authority: str | None
    freeze_authority: str | None
    decimals: int
    supply_raw: int
    extensions: tuple[dict, ...]


@dataclass(frozen=True)
class SolanaHolder:
    token_account: str
    owner: str
    owner_program: str | None  # program that owns the holder's owner account (system program = wallet)
    amount_raw: int


@dataclass(frozen=True)
class SolanaHolderFacts:
    evidence: Evidence
    holders: tuple[SolanaHolder, ...]
