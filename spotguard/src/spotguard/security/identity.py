"""Asset identity verification for DEX tokens.

A token's identity is its chain + contract/mint address. A symbol match proves nothing: anyone can
deploy a token called "USDC". To pass, a reviewed canonical reference (with provenance) must name
this exact address for the claimed symbol AND at least one independent machine source (provider
metadata or on-chain state) must agree on the symbol. A reviewed reference naming a different
address for the same symbol is a collision and blocks.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spotguard.identities import DexAssetId
from spotguard.security.schema import Evidence, FindingSink


@dataclass(frozen=True)
class CanonicalEntry:
    chain: str
    symbol: str
    address: str
    provenance: str  # where the reviewer verified it (official docs URL, issuer page, ...)
    reviewed_by: str
    reviewed_at: str


@dataclass(frozen=True)
class SymbolObservation:
    source: str  # "goplus", "evm_rpc", ...
    symbol: str | None
    evidence: Evidence | None


class CanonicalRegistry:
    """Reviewed canonical addresses. Entries without provenance are rejected at load time."""

    def __init__(self, entries: list[CanonicalEntry]):
        self.entries = entries

    @classmethod
    def load(cls, path: str | Path) -> CanonicalRegistry:
        p = Path(path)
        if not p.exists():
            return cls([])
        raw = tomllib.loads(p.read_text())
        entries = []
        for item in raw.get("token", []):
            for field in ("chain", "symbol", "address", "provenance", "reviewed_by", "reviewed_at"):
                if not str(item.get(field, "")).strip():
                    raise ValueError(f"identity registry entry missing {field}: {item}")
            asset = DexAssetId(item["chain"], item["address"])  # validates + canonicalizes
            entries.append(
                CanonicalEntry(
                    asset.chain,
                    item["symbol"].upper(),
                    asset.address,
                    item["provenance"],
                    item["reviewed_by"],
                    str(item["reviewed_at"]),
                )
            )
        return cls(entries)

    def for_symbol(self, chain: str, symbol: str) -> list[CanonicalEntry]:
        return [e for e in self.entries if e.chain == chain and e.symbol == symbol.upper()]


def verify_identity(
    asset: DexAssetId,
    claimed_symbol: str,
    registry: CanonicalRegistry,
    observations: list[SymbolObservation],
    sink: FindingSink,
    now: datetime,
) -> None:
    claimed = claimed_symbol.upper().strip()
    check = "identity"
    if not claimed:
        sink.unknown(check, "no symbol claimed for this asset")
        return
    canon = registry.for_symbol(asset.chain, claimed)
    if any(e.address != asset.address for e in canon) and not any(e.address == asset.address for e in canon):
        others = ", ".join(e.address for e in canon)
        sink.block(check, f"lookalike: reviewed canonical {claimed} on {asset.chain} is {others}, not {asset.address}")
        return
    if len(canon) > 1:
        sink.unknown(check, f"registry lists several addresses for {claimed} on {asset.chain}; ambiguous")
        return
    if not canon:
        sink.unknown(check, f"no reviewed canonical reference for {claimed} on {asset.chain}")
        return
    agreeing = [o for o in observations if o.symbol and o.symbol.upper().strip() == claimed]
    disagreeing = [o for o in observations if o.symbol and o.symbol.upper().strip() != claimed]
    if disagreeing:
        detail = ", ".join(f"{o.source}={o.symbol!r}" for o in disagreeing)
        sink.unknown(check, f"sources disagree on symbol (claimed {claimed}; {detail})")
        return
    if not agreeing:
        sink.unknown(check, "no independent machine source confirms the symbol")
        return
    entry = canon[0]
    sink.ok(
        check,
        f"reviewed reference ({entry.provenance}, {entry.reviewed_by} {entry.reviewed_at}) "
        f"corroborated by {', '.join(o.source for o in agreeing)}",
        agreeing[0].evidence,
    )
