from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

# Atomic counter to prevent salt collisions when multiple sigs happen in the same second
_salt_counter = 0
_salt_lock = __import__('threading').Lock()

from eth_account import Account
from eth_account.messages import encode_typed_data

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings
from okx_nft_bot.wei_utils import to_wei

CHAIN_ID = 56
# Seaport 1.6 on BSC — used by OKX marketplace (protocolAddress from live parasite offers)
SEAPORT_ADDRESS = "0x0000000000000068F116a894984e2DB1123eB395"
WBNB_ADDRESS = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
# OKX conduit key — from live offers on OKX marketplace
CONDUIT_KEY = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
BSC_RPC = "https://bsc-dataseed.binance.org/"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)
# OKX zone address — required for FULL_RESTRICTED orders on OKX
OKX_ZONE = "0xdf2d4bffec010debd302674c9fb9cda99bb5e852"
GET_COUNTER_SELECTOR = "0xf07ec373"


class ItemType(IntEnum):
    NATIVE = 0
    ERC20 = 1
    ERC721 = 2
    ERC1155 = 3
    ERC721_CRITERIA = 4
    ERC1155_CRITERIA = 5


class OrderType(IntEnum):
    FULL_OPEN = 0
    PARTIAL_OPEN = 1
    FULL_RESTRICTED = 2
    PARTIAL_RESTRICTED = 3


@dataclass(slots=True)
class OrderComponents:
    offerer: str
    zone: str
    offer: list[dict[str, Any]]
    consideration: list[dict[str, Any]]
    order_type: OrderType
    start_time: int
    end_time: int
    zone_hash: str
    salt: int
    conduit_key: str
    counter: int


@dataclass(slots=True)
class SignedOrder:
    parameters: dict[str, Any]
    signature: str


EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "OrderComponents": [
        {"name": "offerer", "type": "address"},
        {"name": "zone", "type": "address"},
        {"name": "offer", "type": "OfferItem[]"},
        {"name": "consideration", "type": "ConsiderationItem[]"},
        {"name": "orderType", "type": "uint8"},
        {"name": "startTime", "type": "uint256"},
        {"name": "endTime", "type": "uint256"},
        {"name": "zoneHash", "type": "bytes32"},
        {"name": "salt", "type": "uint256"},
        {"name": "conduitKey", "type": "bytes32"},
        {"name": "counter", "type": "uint256"},
    ],
    "OfferItem": [
        {"name": "itemType", "type": "uint8"},
        {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"},
        {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
    ],
    "ConsiderationItem": [
        {"name": "itemType", "type": "uint8"},
        {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"},
        {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
        {"name": "recipient", "type": "address"},
    ],
}

EIP712_DOMAIN = {
    "name": "Seaport",
    "version": "1.6",
    "chainId": CHAIN_ID,
    "verifyingContract": SEAPORT_ADDRESS,
}


def _eip712_domain(chain_id: int = CHAIN_ID) -> dict[str, Any]:
    """Build EIP-712 domain with correct chainId (56 for BSC, 1 for ETH)."""
    return {
        "name": "Seaport",
        "version": "1.6",
        "chainId": chain_id,
        "verifyingContract": SEAPORT_ADDRESS,
    }


def _default_transport() -> StdlibHttpTransport:
    return StdlibHttpTransport(timeout=10, max_retries=3, rate_limit_per_sec=2.0)


def _rpc_request_json(
    *,
    rpc_url: str,
    payload: dict[str, Any],
    transport: StdlibHttpTransport | None = None,
) -> dict[str, Any]:
    request_transport = transport or _default_transport()
    return request_transport.request_json(
        method="POST",
        url=rpc_url,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        body=json.dumps(payload),
    )


def get_counter(
    offerer: str,
    rpc_url: str | None = None,
    transport: StdlibHttpTransport | None = None,
    rpc_urls: tuple[str, ...] | list[str] | None = None,
) -> int:
    """Read Seaport counter for ``offerer``.

    Accepts either a single ``rpc_url`` (legacy) or a list via ``rpc_urls``.
    When neither is provided, falls back to ``config.get_rpc_urls("bsc")``
    so a chain of RPC endpoints is tried in order (RISK-3: failover).
    """
    addr_clean = offerer.lower().replace("0x", "")
    calldata = GET_COUNTER_SELECTOR + addr_clean.zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": SEAPORT_ADDRESS, "data": calldata},
            "latest",
        ],
        "id": 1,
    }
    import time as _time
    import random as _random

    if rpc_urls:
        urls = tuple(rpc_urls)
    elif rpc_url:
        urls = (rpc_url,)
    else:
        from okx_nft_bot.config import get_rpc_urls as _get_rpc_urls
        urls = _get_rpc_urls("bsc") or (BSC_RPC,)

    last_err: Any = None
    for url in urls:
        for attempt in range(3):
            try:
                result = _rpc_request_json(rpc_url=url, payload=payload, transport=transport)
            except Exception as exc:
                last_err = exc
                if attempt < 2:
                    _time.sleep(1 + _random.uniform(0, 0.5))
                continue
            if "error" not in result:
                return int(str(result["result"]), 16)
            last_err = result["error"]
            if attempt < 2:
                _time.sleep(1 + _random.uniform(0, 0.5))
    raise RuntimeError(f"RPC error after exhausting {len(urls)} endpoint(s): {last_err}")


def build_order_payload(
    offerer: str,
    collection: str,
    price_wei: int,
    counter: int,
    duration_s: int = 86_400,
    salt: int | None = None,
    zone: str = OKX_ZONE,
    zone_hash: str = ZERO_BYTES32,
    conduit_key: str = CONDUIT_KEY,
    offer_token: str | None = None,
) -> dict[str, Any]:
    return _build_offer_payload(
        offerer=offerer,
        collection=collection,
        price_wei=price_wei,
        counter=counter,
        consideration_item_type=ItemType.ERC721_CRITERIA,
        consideration_identifier=0,
        duration_s=duration_s,
        salt=salt,
        zone=zone,
        zone_hash=zone_hash,
        conduit_key=conduit_key,
        salt_scope="collection",
        offer_token=offer_token,
    )


def build_per_item_offer(
    offerer: str,
    collection: str,
    token_id: int,
    price_wei: int,
    counter: int,
    duration_s: int = 86_400,
    salt: int | None = None,
    zone: str = OKX_ZONE,
    zone_hash: str = ZERO_BYTES32,
    conduit_key: str = CONDUIT_KEY,
    offer_token: str | None = None,
) -> dict[str, Any]:
    return _build_offer_payload(
        offerer=offerer,
        collection=collection,
        price_wei=price_wei,
        counter=counter,
        consideration_item_type=ItemType.ERC721,
        consideration_identifier=token_id,
        duration_s=duration_s,
        salt=salt,
        zone=zone,
        zone_hash=zone_hash,
        conduit_key=conduit_key,
        salt_scope=f"token:{token_id}",
        offer_token=offer_token,
    )


def _build_offer_payload(
    *,
    offerer: str,
    collection: str,
    price_wei: int,
    counter: int,
    consideration_item_type: ItemType,
    consideration_identifier: int,
    duration_s: int,
    salt: int | None,
    zone: str,
    zone_hash: str,
    conduit_key: str,
    salt_scope: str,
    offer_token: str | None = None,
) -> dict[str, Any]:
    global _salt_counter
    now = int(time.time())
    if salt is None:
        with _salt_lock:
            _salt_counter += 1
            _sc = _salt_counter
        entropy = os.urandom(8).hex()
        raw = f"{offerer}:{collection}:{consideration_identifier}:{price_wei}:{now}:{salt_scope}:{_sc}:{entropy}".encode("utf-8")
        salt = int(hashlib.sha256(raw).hexdigest(), 16) % (2**256)

    consideration = [
        {
            "itemType": int(consideration_item_type),
            "token": collection,
            "identifierOrCriteria": consideration_identifier,
            "startAmount": "1",
            "endAmount": "1",
            "recipient": offerer,
        }
    ]
    offer = [
        {
            "itemType": int(ItemType.ERC20),
            "token": offer_token or WBNB_ADDRESS,
            "identifierOrCriteria": 0,
            "startAmount": str(price_wei),
            "endAmount": str(price_wei),
        }
    ]
    return {
        "offerer": offerer,
        "zone": zone,
        "offer": offer,
        "consideration": consideration,
        "orderType": int(OrderType.FULL_RESTRICTED),
        "startTime": str(now),
        "endTime": str(now + duration_s),
        "zoneHash": zone_hash,
        "salt": str(salt),
        "conduitKey": conduit_key,
        "counter": str(counter),
        "totalOriginalConsiderationItems": len(consideration),
    }


def _to_typed_data_message(payload: dict[str, Any]) -> dict[str, Any]:
    def _offer_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "itemType": int(item["itemType"]),
            "token": item["token"],
            "identifierOrCriteria": int(item["identifierOrCriteria"]),
            "startAmount": int(item["startAmount"]),
            "endAmount": int(item["endAmount"]),
        }

    def _consideration_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "itemType": int(item["itemType"]),
            "token": item["token"],
            "identifierOrCriteria": int(item["identifierOrCriteria"]),
            "startAmount": int(item["startAmount"]),
            "endAmount": int(item["endAmount"]),
            "recipient": item["recipient"],
        }

    return {
        "offerer": payload["offerer"],
        "zone": payload["zone"],
        "offer": [_offer_item(item) for item in payload["offer"]],
        "consideration": [_consideration_item(item) for item in payload["consideration"]],
        "orderType": int(payload["orderType"]),
        "startTime": int(payload["startTime"]),
        "endTime": int(payload["endTime"]),
        "zoneHash": bytes.fromhex(str(payload["zoneHash"]).replace("0x", "")),
        "salt": int(payload["salt"]),
        "conduitKey": bytes.fromhex(str(payload["conduitKey"]).replace("0x", "")),
        "counter": int(payload["counter"]),
    }


def sign_order(payload: dict[str, Any], private_key: str, chain_id: int = CHAIN_ID) -> str:
    structured = {
        "types": EIP712_TYPES,
        "domain": _eip712_domain(chain_id),
        "primaryType": "OrderComponents",
        "message": _to_typed_data_message(payload),
    }
    encoded = encode_typed_data(full_message=structured)
    signed = Account.sign_message(encoded, private_key=private_key)
    return "0x" + signed.signature.hex()


def submit_order(signed_order: SignedOrder, *, dry_run: bool = True) -> dict[str, Any]:
    if not dry_run:
        raise RuntimeError("submit_order: live execution is disabled")
    return {
        "stub": True,
        "dry_run": True,
        "message": "submit_order not implemented (execution track is dry-run only)",
        "payload": {
            "parameters": signed_order.parameters,
            "signature": signed_order.signature,
        },
    }


def cancel_order(order_hash: str, *, dry_run: bool = True) -> dict[str, Any]:
    if not dry_run:
        raise RuntimeError("cancel_order: live execution is disabled")
    return {
        "stub": True,
        "dry_run": True,
        "message": "cancel_order not implemented (execution track is dry-run only)",
        "order_hash": order_hash,
    }


def preview_counterbid(
    *,
    settings: Settings,
    collection: str,
    price_bnb: float,
    chain: str = "bsc",
    rpc_url: str = BSC_RPC,
    transport: StdlibHttpTransport | None = None,
) -> dict[str, Any]:
    # BSC-only: BSC_RPC default + BNB-denominated price_bnb; EIP-712 domain ties to BSC chain id
    if chain.lower() != "bsc":
        raise ValueError(f"Only 'bsc' is supported; got {chain!r}")
    if not settings.buyer_wallet_private_key or not settings.buyer_wallet_address:
        raise RuntimeError("BUYER_WALLET_ADDRESS and BUYER_WALLET_PRIVATE_KEY are required")

    account = Account.from_key(settings.buyer_wallet_private_key)
    if account.address.lower() != settings.buyer_wallet_address.lower():
        raise RuntimeError("BUYER_WALLET_ADDRESS does not match BUYER_WALLET_PRIVATE_KEY")

    price_wei = to_wei(price_bnb)
    counter = get_counter(account.address, rpc_url=rpc_url, transport=transport)
    payload = build_order_payload(
        offerer=account.address,
        collection=collection,
        price_wei=price_wei,
        counter=counter,
    )
    signature = sign_order(payload, settings.buyer_wallet_private_key)
    return {
        "dry_run": settings.dry_run,
        "chain": chain.lower(),
        "offerer": account.address,
        "collection": collection,
        "price_bnb": price_bnb,
        "price_wei": price_wei,
        "counter": counter,
        "parameters": payload,
        "signature": signature,
    }
