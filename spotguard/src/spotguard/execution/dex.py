"""DEX execution: SIMULATION ONLY.

What is implemented:
* TransactionPolicy.validate(): checks a *decoded* swap transaction against its intent: recipient,
  program/router allowlist, exact raw amounts at the token's decimals, minimum output vs. quote
  and max slippage, bounded (never unlimited) approvals, fee caps, nonce/blockhash validity, and
  simulated balance deltas.
* TxTracker: duplicate-submission prevention per intent, confirmation depth/finality, and reorg
  handling (a confirmed tx whose block is no longer canonical returns to PENDING).
* ConstantProductPool: x*y=k paper simulation with fee, for price impact.

What is NOT implemented (so live DEX trading is impossible in this build): decoding real Solana or
EVM wire-format transactions, signing, broadcasting, and fetching real chain state. A private RPC
or MEV-aware route would not guarantee protection from sandwiching or loss either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

UNLIMITED_APPROVAL = 2**256 - 1


def to_raw(amount: Decimal, decimals: int) -> int:
    """Exact conversion to base units; refuses amounts that don't fit the token's decimals."""
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} has more precision than {decimals} decimals")
    return int(scaled)


@dataclass(frozen=True)
class SwapIntent:
    intent_id: str
    chain: str
    wallet: str
    token_in: str
    token_out: str
    amount_in: Decimal
    decimals_in: int
    quoted_out: Decimal
    decimals_out: int
    max_slippage: Decimal


@dataclass(frozen=True)
class DecodedTx:
    """Fields a decoder must extract from the actual transaction before signing."""

    chain: str
    signer: str
    programs: tuple[str, ...]  # Solana program ids / EVM call targets
    recipient: str
    token_in: str
    token_out: str
    amount_in_raw: int
    min_out_raw: int
    approval_raw: int | None  # EVM ERC-20 approval amount; None if no approval in the bundle
    approval_spender: str | None
    priority_fee: int  # lamports (Solana) or wei per gas (EVM)
    nonce: int | None = None  # EVM
    recent_blockhash: str | None = None  # Solana
    last_valid_block_height: int | None = None  # Solana
    simulated_deltas: dict[str, int] = field(default_factory=dict)  # token -> raw balance change for signer


@dataclass(frozen=True)
class ChainView:
    current_block_height: int
    next_nonce: int | None
    used_nonces: frozenset[int] = frozenset()


@dataclass(frozen=True)
class TransactionPolicy:
    allowed_programs: frozenset[str]
    allowed_spenders: frozenset[str]
    max_priority_fee: int

    def validate(self, tx: DecodedTx, intent: SwapIntent, chain: ChainView) -> list[str]:
        v = []
        if tx.chain != intent.chain:
            v.append(f"chain {tx.chain} != intent chain {intent.chain}")
        if tx.signer != intent.wallet:
            v.append("signer is not the bot wallet")
        if tx.recipient != intent.wallet:
            v.append(f"unexpected recipient {tx.recipient}")
        unknown = [p for p in tx.programs if p not in self.allowed_programs]
        if unknown:
            v.append(f"programs/routers not allowlisted: {unknown}")
        if (tx.token_in, tx.token_out) != (intent.token_in, intent.token_out):
            v.append("token pair differs from intent")
        try:
            want_in = to_raw(intent.amount_in, intent.decimals_in)
        except ValueError as exc:
            v.append(str(exc))
            want_in = -1
        if tx.amount_in_raw != want_in:
            v.append(f"amount_in {tx.amount_in_raw} != intended {want_in} (decimals {intent.decimals_in})")
        floor = intent.quoted_out * (1 - intent.max_slippage) * (Decimal(10) ** intent.decimals_out)
        min_out = int(floor.to_integral_value(rounding=ROUND_DOWN))
        if tx.min_out_raw < min_out:
            v.append(f"min_out {tx.min_out_raw} allows more than {intent.max_slippage:.2%} slippage (needs >= {min_out})")
        if tx.approval_raw is not None:
            if tx.approval_raw >= UNLIMITED_APPROVAL or tx.approval_raw > max(want_in, 0):
                v.append(f"approval {tx.approval_raw} exceeds the swap amount (unlimited approvals are refused)")
            if tx.approval_spender not in self.allowed_spenders:
                v.append(f"approval spender {tx.approval_spender} not allowlisted")
        if tx.priority_fee > self.max_priority_fee:
            v.append(f"priority fee {tx.priority_fee} exceeds cap {self.max_priority_fee}")
        if tx.chain == "solana":
            if not tx.recent_blockhash or tx.last_valid_block_height is None:
                v.append("missing blockhash")
            elif chain.current_block_height > tx.last_valid_block_height:
                v.append("blockhash expired; rebuild the transaction")
        else:
            if tx.nonce is None or chain.next_nonce is None:
                v.append("missing nonce")
            elif tx.nonce in chain.used_nonces or tx.nonce != chain.next_nonce:
                v.append(f"nonce {tx.nonce} collides or is out of order (next {chain.next_nonce})")
        d = tx.simulated_deltas
        if not d:
            v.append("no simulated balance deltas")
        else:
            if d.get(intent.token_in) != -want_in:
                v.append(f"simulated {intent.token_in} change {d.get(intent.token_in)} != -{want_in}")
            if d.get(intent.token_out, 0) < min_out:
                v.append(f"simulated {intent.token_out} received {d.get(intent.token_out, 0)} < min_out {min_out}")
            others = {k: x for k, x in d.items() if k not in (intent.token_in, intent.token_out, "native_fee") and x < 0}
            if others:
                v.append(f"simulation drains other balances: {others}")
        return v


class TxStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FINAL = "FINAL"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class TrackedTx:
    intent_id: str
    signature: str
    status: TxStatus = TxStatus.PENDING
    block_hash: str | None = None
    block_height: int | None = None


class TxTracker:
    def __init__(self, finality_depth: int):
        self.finality_depth = finality_depth
        self.by_intent: dict[str, TrackedTx] = {}

    def submit(self, intent_id: str, signature: str) -> TrackedTx:
        existing = self.by_intent.get(intent_id)
        if existing and existing.status in (TxStatus.PENDING, TxStatus.CONFIRMED, TxStatus.FINAL):
            raise RuntimeError(f"intent {intent_id} already has transaction {existing.signature} ({existing.status.value})")
        tx = TrackedTx(intent_id, signature)
        self.by_intent[intent_id] = tx
        return tx

    def on_included(self, intent_id: str, block_hash: str, block_height: int) -> None:
        tx = self.by_intent[intent_id]
        tx.status, tx.block_hash, tx.block_height = TxStatus.CONFIRMED, block_hash, block_height

    def on_head(self, head_height: int, canonical: dict[int, str]) -> list[str]:
        """Advance finality; reorged-out transactions go back to PENDING for reconciliation."""
        events = []
        for tx in self.by_intent.values():
            if tx.status not in (TxStatus.CONFIRMED, TxStatus.FINAL) or tx.block_height is None:
                continue
            if canonical.get(tx.block_height) not in (None, tx.block_hash):
                if tx.status is TxStatus.FINAL:
                    events.append(f"ALERT: finalized tx {tx.signature} reorged out")
                tx.status, tx.block_hash, tx.block_height = TxStatus.PENDING, None, None
                events.append(f"reorg: {tx.signature} back to PENDING")
            elif head_height - tx.block_height >= self.finality_depth:
                tx.status = TxStatus.FINAL
        return events

    def on_expired(self, intent_id: str) -> None:
        tx = self.by_intent[intent_id]
        if tx.status is TxStatus.PENDING:
            tx.status = TxStatus.EXPIRED  # only now may a new transaction be built for the intent


@dataclass
class ConstantProductPool:
    """Paper x*y=k pool. Simulation only."""

    reserve_in: Decimal
    reserve_out: Decimal
    fee: Decimal = Decimal("0.0025")

    def quote_out(self, amount_in: Decimal) -> Decimal:
        effective = amount_in * (1 - self.fee)
        return self.reserve_out * effective / (self.reserve_in + effective)

    def price_impact(self, amount_in: Decimal) -> Decimal:
        spot = self.reserve_out / self.reserve_in
        return 1 - (self.quote_out(amount_in) / amount_in) / spot

    def swap(self, amount_in: Decimal) -> Decimal:
        out = self.quote_out(amount_in)
        self.reserve_in += amount_in
        self.reserve_out -= out
        return out
