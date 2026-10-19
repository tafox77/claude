"""Network providers for screening evidence. Each returns Evidence/facts or raises ProviderError.

Endpoints:
* GoPlus (https://api.gopluslabs.io): GET /api/v1/token_security/{chain_id}?contract_addresses=,
  GET /api/v1/solana/token_security?contract_addresses=, GET /api/v1/supported_chains.
  Optional header "Authorization: Bearer <token>" (GOPLUS_ACCESS_TOKEN). Free access is not assumed;
  rate limits apply.
* Solana JSON-RPC: getAccountInfo(jsonParsed), getTokenLargestAccounts, getMultipleAccounts.
* EVM JSON-RPC: eth_getCode, eth_getStorageAt (EIP-1967 slots), eth_call decimals()/symbol().
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from spotguard.security.facts import EvmRpcFacts, SolanaHolder, SolanaHolderFacts, SolanaMintFacts
from spotguard.security.schema import Evidence

GOPLUS_BASE = "https://api.gopluslabs.io"
EIP1967_IMPLEMENTATION = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"


class ProviderError(RuntimeError):
    pass


class GoPlusClient:
    def __init__(self, client: httpx.AsyncClient, base: str = GOPLUS_BASE, token: str | None = None):
        self.client = client
        self.base = base.rstrip("/")
        self.token = token if token is not None else os.environ.get("GOPLUS_ACCESS_TOKEN")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = self.token if self.token.startswith("Bearer ") else f"Bearer {self.token}"
        return h

    async def _get(self, path: str, params: dict, now: datetime) -> Evidence:
        url = f"{self.base}{path}"
        try:
            resp = await self.client.get(url, params=params, headers=self._headers())
            resp.raise_for_status()
            raw = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"GoPlus {path}: {exc}") from exc
        return Evidence("goplus", f"{url}?{httpx.QueryParams(params)}", now, raw)

    async def evm_token(self, chain_id: str, address: str, now: datetime) -> Evidence:
        return await self._get(f"/api/v1/token_security/{chain_id}", {"contract_addresses": address}, now)

    async def solana_token(self, mint: str, now: datetime) -> Evidence:
        return await self._get("/api/v1/solana/token_security", {"contract_addresses": mint}, now)

    async def supported_chains(self, now: datetime) -> set[str]:
        ev = await self._get("/api/v1/supported_chains", {}, now)
        if not isinstance(ev.raw, dict) or ev.raw.get("code") != 1 or not isinstance(ev.raw.get("result"), list):
            raise ProviderError("GoPlus supported_chains: unexpected response")
        return {str(c.get("id")) for c in ev.raw["result"] if isinstance(c, dict)}


class JsonRpc:
    def __init__(self, client: httpx.AsyncClient, url: str, name: str):
        self.client = client
        self.url = url
        self.name = name

    async def call(self, method: str, params: list) -> Any:
        try:
            resp = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"{self.name} {method}: {exc}") from exc
        if not isinstance(data, dict) or data.get("error") is not None or "result" not in data:
            raise ProviderError(f"{self.name} {method}: {data.get('error') if isinstance(data, dict) else data}")
        return data["result"]


class SolanaRpc(JsonRpc):
    def __init__(self, client: httpx.AsyncClient, url: str):
        super().__init__(client, url, "solana-rpc")

    async def mint_facts(self, mint: str, now: datetime) -> SolanaMintFacts:
        result = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (result or {}).get("value")
        if not value:
            raise ProviderError(f"no account at {mint}")
        data = value.get("data")
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict) or parsed.get("type") != "mint" or not isinstance(parsed.get("info"), dict):
            return SolanaMintFacts(Evidence("solana-rpc", f"getAccountInfo:{mint}", now, value), value.get("owner", ""),
                                   None, None, 0, 0, ())  # fmt: skip
        info = parsed["info"]
        try:
            decimals, supply = int(info["decimals"]), int(info["supply"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed mint info for {mint}") from exc
        return SolanaMintFacts(
            Evidence("solana-rpc", f"getAccountInfo:{mint}", now, value),
            value.get("owner", ""),
            info.get("mintAuthority"),
            info.get("freezeAuthority"),
            decimals,
            supply,
            tuple(info.get("extensions") or ()),
        )

    async def holder_facts(self, mint: str, supply_raw: int, now: datetime) -> SolanaHolderFacts:
        largest = (await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]) or {}).get("value")
        if not isinstance(largest, list):
            raise ProviderError("getTokenLargestAccounts: malformed")
        accounts = [a["address"] for a in largest]
        infos = (await self.call("getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}]) or {}).get("value") or []
        owners = []
        for info in infos:
            parsed = (info or {}).get("data", {}).get("parsed", {}) if isinstance((info or {}).get("data"), dict) else {}
            owners.append(parsed.get("info", {}).get("owner"))
        unique = sorted({o for o in owners if o})
        owner_accounts = []
        if unique:
            owner_accounts = (await self.call("getMultipleAccounts", [unique, {"encoding": "base64"}]) or {}).get("value") or []
        program_of = {o: (acc or {}).get("owner") for o, acc in zip(unique, owner_accounts, strict=False)}
        holders = []
        for acc, owner in zip(largest, owners, strict=False):
            if not owner:
                raise ProviderError("token account owner unreadable")
            holders.append(SolanaHolder(acc["address"], owner, program_of.get(owner), int(acc["amount"])))
        raw = {"largest": largest, "owners": owners, "owner_programs": program_of, "supply_raw": supply_raw}
        return SolanaHolderFacts(Evidence("solana-rpc", f"holders:{mint}", now, raw), tuple(holders))


def _decode_abi_string(hexdata: str) -> str | None:
    try:
        b = bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata)
        if len(b) >= 64:
            length = int.from_bytes(b[32:64], "big")
            return b[64 : 64 + length].decode("utf-8")
        if len(b) == 32:  # some old tokens return bytes32
            return b.rstrip(b"\x00").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return None


class EvmRpc(JsonRpc):
    def __init__(self, client: httpx.AsyncClient, url: str):
        super().__init__(client, url, "evm-rpc")

    async def facts(self, address: str, now: datetime) -> EvmRpcFacts:
        code = await self.call("eth_getCode", [address, "latest"])
        impl = await self.call("eth_getStorageAt", [address, EIP1967_IMPLEMENTATION, "latest"])
        admin = await self.call("eth_getStorageAt", [address, EIP1967_ADMIN, "latest"])
        decimals_raw = await self.call("eth_call", [{"to": address, "data": "0x313ce567"}, "latest"])
        symbol_raw = await self.call("eth_call", [{"to": address, "data": "0x95d89b41"}, "latest"])

        def slot_address(v: str) -> str | None:
            n = int(v, 16) if isinstance(v, str) and v.startswith("0x") and len(v) > 2 else 0
            return "0x" + f"{n:040x}"[-40:] if n else None

        try:
            decimals = int(decimals_raw, 16) if decimals_raw not in (None, "0x") else None
        except (TypeError, ValueError):
            decimals = None
        raw = {
            "code_len": len(code or ""),
            "impl": impl,
            "admin": admin,
            "decimals": decimals_raw,
            "symbol": symbol_raw,
        }
        return EvmRpcFacts(
            Evidence("evm-rpc", f"state:{address}", now, raw),
            has_code=isinstance(code, str) and len(code) > 2,
            implementation=slot_address(impl),
            admin=slot_address(admin),
            decimals=decimals,
            symbol=_decode_abi_string(symbol_raw) if isinstance(symbol_raw, str) else None,
        )
