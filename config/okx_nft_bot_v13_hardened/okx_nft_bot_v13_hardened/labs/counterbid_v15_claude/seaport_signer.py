"""
seaport_signer.py  –  Parasite-Killer v14
==========================================
Seaport v1.5 / EIP-712  order builder + signer for BSC (chainId 56).

DRY-RUN ONLY.  No transactions are submitted.  No real execution.
All signing is local; private key is read exclusively from .env / environment.

Usage (as library):
    from seaport_signer import build_order_payload, sign_order, get_counter

Usage (CLI):
    python -m okx_nft_bot.seaport_signer preview-counterbid \
        --collection 0xABC... --price 0.05 --chain bsc
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAIN_ID = 56
SEAPORT_ADDRESS = "0x00000000000000ADc04C56Bf30aC9d3c0aAF14dC"
WBNB_ADDRESS    = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
CONDUIT_KEY     = "0x618Cf13c76c1FFC2168fC47c98453dCc6134F5c8888888888888888888888888"
BSC_RPC         = "https://bsc-dataseed.binance.org/"

# Seaport zone used by OKX on BSC (open zone = zero address → no zone validation)
ZERO_ADDRESS  = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32  = "0x" + "00" * 32

# getCounter(address) selector
GET_COUNTER_SELECTOR = "0xf07ec373"

DRY_RUN            = True
EXECUTION_ENABLED  = False


# ---------------------------------------------------------------------------
# Seaport type enums
# ---------------------------------------------------------------------------
class ItemType(IntEnum):
    NATIVE         = 0
    ERC20          = 1
    ERC721         = 2
    ERC1155        = 3
    ERC721_CRITERIA  = 4   # collection offer (any token id)
    ERC1155_CRITERIA = 5


class OrderType(IntEnum):
    FULL_OPEN    = 0
    PARTIAL_OPEN = 1
    FULL_RESTRICTED   = 2
    PARTIAL_RESTRICTED = 3


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class OfferItem:
    item_type:                ItemType
    token:                    str
    identifier_or_criteria:  int  # 0 for ERC20; 0 for criteria-based (any token)
    start_amount:             int  # wei
    end_amount:               int  # wei


@dataclass
class ConsiderationItem:
    item_type:                ItemType
    token:                    str
    identifier_or_criteria:  int
    start_amount:             int
    end_amount:               int
    recipient:                str


@dataclass
class OrderComponents:
    offerer:        str
    zone:           str
    offer:          list[OfferItem]
    consideration:  list[ConsiderationItem]
    order_type:     OrderType
    start_time:     int
    end_time:       int
    zone_hash:      str
    salt:           int
    conduit_key:    str
    counter:        int


@dataclass
class SignedOrder:
    parameters:  dict[str, Any]
    signature:   str


# ---------------------------------------------------------------------------
# EIP-712 type definitions  (Seaport v1.5 canonical)
# ---------------------------------------------------------------------------
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name",              "type": "string"},
        {"name": "version",           "type": "string"},
        {"name": "chainId",           "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "OrderComponents": [
        {"name": "offerer",       "type": "address"},
        {"name": "zone",          "type": "address"},
        {"name": "offer",         "type": "OfferItem[]"},
        {"name": "consideration", "type": "ConsiderationItem[]"},
        {"name": "orderType",     "type": "uint8"},
        {"name": "startTime",     "type": "uint256"},
        {"name": "endTime",       "type": "uint256"},
        {"name": "zoneHash",      "type": "bytes32"},
        {"name": "salt",          "type": "uint256"},
        {"name": "conduitKey",    "type": "bytes32"},
        {"name": "counter",       "type": "uint256"},
    ],
    "OfferItem": [
        {"name": "itemType",               "type": "uint8"},
        {"name": "token",                  "type": "address"},
        {"name": "identifierOrCriteria",   "type": "uint256"},
        {"name": "startAmount",            "type": "uint256"},
        {"name": "endAmount",              "type": "uint256"},
    ],
    "ConsiderationItem": [
        {"name": "itemType",               "type": "uint8"},
        {"name": "token",                  "type": "address"},
        {"name": "identifierOrCriteria",   "type": "uint256"},
        {"name": "startAmount",            "type": "uint256"},
        {"name": "endAmount",              "type": "uint256"},
        {"name": "recipient",              "type": "address"},
    ],
}

EIP712_DOMAIN = {
    "name":              "Seaport",
    "version":           "1.5",
    "chainId":           CHAIN_ID,
    "verifyingContract": SEAPORT_ADDRESS,
}


# ---------------------------------------------------------------------------
# Counter fetch via raw JSON-RPC (no web3.py dependency required)
# ---------------------------------------------------------------------------
def get_counter(offerer: str, rpc_url: str = BSC_RPC) -> int:
    """
    Call Seaport.getCounter(address offerer) via raw eth_call.
    Returns the current counter as int.
    """
    # ABI-encode the address: pad to 32 bytes
    addr_clean = offerer.lower().replace("0x", "")
    calldata = GET_COUNTER_SELECTOR + addr_clean.zfill(64)

    payload = {
        "jsonrpc": "2.0",
        "method":  "eth_call",
        "params":  [
            {"to": SEAPORT_ADDRESS, "data": calldata},
            "latest",
        ],
        "id": 1,
    }
    resp = requests.post(rpc_url, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")

    hex_val = result["result"]
    return int(hex_val, 16)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------
def build_order_payload(
    offerer:    str,
    collection: str,
    price_wei:  int,
    counter:    int,
    duration_s: int = 86_400,   # 24 h default
    salt:       Optional[int] = None,
    zone:       str = ZERO_ADDRESS,
    zone_hash:  str = ZERO_BYTES32,
    conduit_key: str = CONDUIT_KEY,
) -> dict[str, Any]:
    """
    Build a Seaport v1.5 collection-offer order payload.

    Offer:          WBNB (ERC20)  –  price_wei
    Consideration:  Any NFT from <collection>  (ERC721_CRITERIA, tokenId=0)
                    + optional fee items can be appended later
    Order type:     FULL_OPEN (0)
    """
    now       = int(time.time())
    start_time = now
    end_time   = now + duration_s

    if salt is None:
        # deterministic but unique per (offerer, collection, price, time)
        raw = f"{offerer}{collection}{price_wei}{now}".encode()
        salt = int(hashlib.sha256(raw).hexdigest(), 16) % (2**256)

    offer_items = [
        {
            "itemType":             int(ItemType.ERC20),
            "token":                WBNB_ADDRESS,
            "identifierOrCriteria": 0,
            "startAmount":          str(price_wei),
            "endAmount":            str(price_wei),
        }
    ]

    # collection offer: ERC721_CRITERIA, tokenId=0 means "any token in collection"
    consideration_items = [
        {
            "itemType":             int(ItemType.ERC721_CRITERIA),
            "token":                collection,
            "identifierOrCriteria": 0,
            "startAmount":          "1",
            "endAmount":            "1",
            "recipient":            offerer,
        }
    ]

    return {
        "offerer":       offerer,
        "zone":          zone,
        "offer":         offer_items,
        "consideration": consideration_items,
        "orderType":     int(OrderType.FULL_OPEN),
        "startTime":     str(start_time),
        "endTime":       str(end_time),
        "zoneHash":      zone_hash,
        "salt":          str(salt),
        "conduitKey":    conduit_key,
        "counter":       str(counter),
        "totalOriginalConsiderationItems": len(consideration_items),
    }


# ---------------------------------------------------------------------------
# EIP-712 signing
# ---------------------------------------------------------------------------
def _to_typed_data_message(payload: dict[str, Any]) -> dict:
    """Convert string amounts back to int for eth_account structured signing."""
    def conv_item(item: dict, with_recipient: bool = False) -> dict:
        d = {
            "itemType":             int(item["itemType"]),
            "token":                item["token"],
            "identifierOrCriteria": int(item["identifierOrCriteria"]),
            "startAmount":          int(item["startAmount"]),
            "endAmount":            int(item["endAmount"]),
        }
        if with_recipient:
            d["recipient"] = item["recipient"]
        return d

    return {
        "offerer":       payload["offerer"],
        "zone":          payload["zone"],
        "offer":         [conv_item(i) for i in payload["offer"]],
        "consideration": [conv_item(i, with_recipient=True) for i in payload["consideration"]],
        "orderType":     int(payload["orderType"]),
        "startTime":     int(payload["startTime"]),
        "endTime":       int(payload["endTime"]),
        "zoneHash":      bytes.fromhex(payload["zoneHash"].replace("0x", "")),
        "salt":          int(payload["salt"]),
        "conduitKey":    bytes.fromhex(payload["conduitKey"].replace("0x", "")),
        "counter":       int(payload["counter"]),
    }


def sign_order(payload: dict[str, Any], private_key: str) -> str:
    """
    Sign the order payload with EIP-712.
    Returns hex signature string (0x-prefixed).

    private_key must be a hex string (0x-prefixed or not).
    """
    message_data = _to_typed_data_message(payload)

    structured_data = {
        "types":              EIP712_TYPES,
        "domain":             EIP712_DOMAIN,
        "primaryType":        "OrderComponents",
        "message":            message_data,
    }

    # eth_account encode_typed_data expects bytes32 as bytes
    encoded = encode_typed_data(full_message=structured_data)
    signed  = Account.sign_message(encoded, private_key=private_key)
    return signed.signature.hex() if not signed.signature.hex().startswith("0x") else "0x" + signed.signature.hex().lstrip("0x")


# ---------------------------------------------------------------------------
# Stubs  (execution_enabled = False → will never reach network)
# ---------------------------------------------------------------------------
def submit_order(signed_order: SignedOrder, *, dry_run: bool = DRY_RUN) -> dict:
    """
    STUB — submit order to OKX Web3 API.
    Returns a fake success envelope; never sends real transactions.
    """
    if not dry_run or EXECUTION_ENABLED:
        raise RuntimeError("submit_order: live execution is disabled in v14.")
    return {
        "stub":    True,
        "dry_run": True,
        "message": "submit_order not implemented (v14 dry-run only)",
        "payload": signed_order,
    }


def cancel_order(order_hash: str, *, dry_run: bool = DRY_RUN) -> dict:
    """
    STUB — cancel order on-chain via Seaport.
    Never sends a real transaction.
    """
    if not dry_run or EXECUTION_ENABLED:
        raise RuntimeError("cancel_order: live execution is disabled in v14.")
    return {
        "stub":       True,
        "dry_run":    True,
        "message":    "cancel_order not implemented (v14 dry-run only)",
        "order_hash": order_hash,
    }


# ---------------------------------------------------------------------------
# CLI  (python seaport_signer.py preview-counterbid ...)
# ---------------------------------------------------------------------------
def _load_private_key() -> str:
    """Load PRIVATE_KEY from environment / .env file."""
    pk = os.environ.get("PRIVATE_KEY")
    if pk:
        return pk

    # Try to read .env in cwd
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("PRIVATE_KEY="):
                    pk = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if pk:
                        return pk

    raise EnvironmentError(
        "PRIVATE_KEY not set. Add it to your .env file:\n  PRIVATE_KEY=0xYOUR_KEY_HERE"
    )


def cli_preview_counterbid(
    collection: str,
    price_bnb:  float,
    chain:      str = "bsc",
) -> None:
    """
    preview-counterbid  –  print order payload + signature.
    DRY RUN ONLY — nothing is submitted.
    """
    print("=" * 60)
    print("  PARASITE-KILLER v14  |  preview-counterbid")
    print("  *** DRY RUN ONLY — NO REAL SUBMISSION ***")
    print("=" * 60)

    if chain.lower() != "bsc":
        raise ValueError(f"Only 'bsc' is supported in v14; got: {chain!r}")

    # Load key
    private_key = _load_private_key()
    account     = Account.from_key(private_key)
    offerer     = account.address

    price_wei = int(price_bnb * 10**18)

    print(f"\n[+] Offerer:     {offerer}")
    print(f"[+] Collection:  {collection}")
    print(f"[+] Price:       {price_bnb} BNB  ({price_wei} wei)")
    print(f"[+] Chain:       {chain.upper()}  (chainId={CHAIN_ID})")

    # Fetch counter
    print(f"\n[+] Fetching counter from Seaport ({BSC_RPC})...")
    counter = get_counter(offerer)
    print(f"[+] Counter:     {counter}")

    # Build payload
    payload = build_order_payload(
        offerer    = offerer,
        collection = collection,
        price_wei  = price_wei,
        counter    = counter,
    )

    # Sign
    print("\n[+] Signing order (EIP-712)...")
    signature = sign_order(payload, private_key)

    # Output
    print("\n── ORDER PAYLOAD ─────────────────────────────────────")
    print(json.dumps(payload, indent=2))

    print("\n── SIGNATURE ─────────────────────────────────────────")
    print(signature)

    print("\n── SUMMARY ───────────────────────────────────────────")
    print(f"  Offerer:      {offerer}")
    print(f"  Collection:   {collection}")
    print(f"  Bid amount:   {price_bnb} WBNB")
    print(f"  Valid until:  +{payload['endTime']} unix")
    print(f"  Order type:   FULL_OPEN ({OrderType.FULL_OPEN})")
    print(f"  Counter:      {counter}")
    print(f"  Salt:         {payload['salt']}")
    print(f"  Conduit key:  {payload['conduitKey'][:12]}...")

    print("\n" + "=" * 60)
    print("  *** DRY RUN COMPLETE — PAYLOAD NOT SUBMITTED ***")
    print("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Parasite-Killer v14 — Seaport signer (dry-run)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser(
        "preview-counterbid",
        help="Build + sign a collection counter-bid (dry-run only)",
    )
    preview.add_argument("--collection", required=True, help="NFT collection contract address")
    preview.add_argument("--price",      required=True, type=float, help="Bid price in BNB")
    preview.add_argument("--chain",      default="bsc", help="Chain (only 'bsc' supported)")

    args = parser.parse_args()

    if args.command == "preview-counterbid":
        cli_preview_counterbid(
            collection = args.collection,
            price_bnb  = args.price,
            chain      = args.chain,
        )
