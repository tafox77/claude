"""Screening orchestration: gather evidence concurrently, evaluate, persist, cache.

Provider timeouts or errors produce UNKNOWN findings (which block entries). Screening runs as its
own task and never sits in the exit path: exits for held assets proceed regardless of verdicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from spotguard.config import Policy
from spotguard.identities import ChainKind, DexAssetId, UnsupportedChain, chain_spec
from spotguard.security.evm import evaluate_evm
from spotguard.security.facts import PoolFacts, SellSimulation
from spotguard.security.identity import CanonicalRegistry
from spotguard.security.schema import FindingSink, SecurityReport
from spotguard.security.solana import evaluate_solana
from spotguard.storage import Store, iso

log = logging.getLogger(__name__)


class PoolVerifier:
    """Independent pool verification. Not implemented for live providers: always returns None (UNKNOWN)."""

    async def verify(self, asset: DexAssetId, now: datetime) -> PoolFacts | None:
        return None


class SellSimulator:
    """Balance-delta sell simulation. Not implemented for live providers: always returns None (UNKNOWN)."""

    async def simulate(self, asset: DexAssetId, now: datetime) -> SellSimulation | None:
        return None


@dataclass
class Screener:
    policy: Policy
    store: Store | None
    registry: CanonicalRegistry
    goplus: Any = None
    solana_rpc: Any = None
    evm_rpcs: dict[str, Any] = field(default_factory=dict)  # chain key -> EvmRpc
    pools: PoolVerifier = field(default_factory=PoolVerifier)
    sells: SellSimulator = field(default_factory=SellSimulator)
    timeout_s: float = 10.0
    goplus_chains: set[str] | None = None  # pinned coverage from /supported_chains
    cache: dict[str, SecurityReport] = field(default_factory=dict)

    async def _try(self, label: str, coro: Awaitable | None) -> Any:
        if coro is None:
            return None
        try:
            return await asyncio.wait_for(coro, self.timeout_s)
        except Exception as exc:  # timeouts and provider failures become UNKNOWN downstream
            log.warning("screening provider %s failed: %s", label, exc)
            return None

    async def screen(self, chain: str, address: str, claimed_symbol: str, now: datetime) -> SecurityReport:
        try:
            asset = DexAssetId(chain, address)
            spec = chain_spec(chain)
        except (UnsupportedChain, ValueError) as exc:
            report = SecurityReport(f"dex:{chain}:{address}", self.policy.version, now)
            FindingSink(report, now).unknown("asset", str(exc))
            return self._persist(report)

        pool = await self._try("pool", self.pools.verify(asset, now))
        sell = await self._try("sell", self.sells.simulate(asset, now))
        if spec.kind is ChainKind.SOLANA:
            goplus_ev = await self._try("goplus", self.goplus.solana_token(asset.address, now) if self.goplus else None)
            mint = await self._try("mint", self.solana_rpc.mint_facts(asset.address, now) if self.solana_rpc else None)
            holders = None
            if mint is not None and mint.supply_raw > 0:
                holders = await self._try("holders", self.solana_rpc.holder_facts(asset.address, mint.supply_raw, now))
            report = evaluate_solana(asset, claimed_symbol, self.policy.screening, self.policy.version, now,
                                     goplus_ev, mint, holders, pool, sell, self.registry)  # fmt: skip
        else:
            covered = self.goplus_chains is None or spec.goplus_chain_id in self.goplus_chains
            goplus_ev = None
            if self.goplus and covered:
                goplus_ev = await self._try("goplus", self.goplus.evm_token(spec.goplus_chain_id, asset.address, now))
            rpc = self.evm_rpcs.get(chain)
            rpc_facts = await self._try("evm-rpc", rpc.facts(asset.address, now) if rpc else None)
            report = evaluate_evm(asset, claimed_symbol, self.policy.screening, self.policy.version, now,
                                  goplus_ev, rpc_facts, pool, sell, self.registry)  # fmt: skip
            if not covered:
                FindingSink(report, now).unknown("coverage", f"GoPlus does not list chain {spec.goplus_chain_id}")
        return self._persist(report)

    def _persist(self, report: SecurityReport) -> SecurityReport:
        self.cache[report.asset_key] = report
        if self.store is not None:
            with self.store.tx() as c:
                for ev in report.evidence:
                    c.execute(
                        "INSERT OR IGNORE INTO security_evidence VALUES (?, ?, ?, ?, ?)",
                        (ev.evidence_id, ev.source, ev.url, iso(ev.fetched_at), json.dumps(ev.raw, default=str)),
                    )
                c.execute(
                    "INSERT INTO security_reports VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, report.asset_key, report.verdict.value, report.to_json(), iso(report.created_at),
                     report.policy_version),
                )  # fmt: skip
        return report
