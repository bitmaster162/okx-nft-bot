#!/usr/bin/env python3
"""
Cancel ALL Seaport offers + listings via incrementCounter() on BSC.

Logic: incrementCounter() bumps our wallet's Seaport nonce, invalidating
ALL existing signed orders (offers AND listings) from our wallet on that chain.
Local SQLite seaport_counter table is synced with new on-chain value.

Cost: ~0.0003 BNB gas on BSC.

Usage:
  python3 seaport_cancel_all.py          # dry-run
  python3 seaport_cancel_all.py --execute # send BSC transaction
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/src")

from web3 import Web3
from eth_account import Account

SEAPORT_ADDRESS = "0x0000000000000068f116a894984e2db1123eb395"  # v1.6 same on BSC/ETH

SEAPORT_ABI = json.loads("""[
    {"inputs":[],"name":"incrementCounter","outputs":[{"name":"newCounter","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"offerer","type":"address"}],"name":"getCounter","outputs":[{"name":"counter","type":"uint256"}],"stateMutability":"view","type":"function"}
]""")

DB_PATH = "/app/data/execution.sqlite3"


def sync_db_counter(wallet: str, chain: str, new_counter: int) -> None:
    """Update seaport_counter table to match on-chain value."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            """
            INSERT INTO seaport_counter (wallet, chain, next_counter, last_synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet, chain) DO UPDATE SET
                next_counter = excluded.next_counter,
                last_synced_at = excluded.last_synced_at
            """,
            (wallet.lower(), chain, str(new_counter), now),
        )
        conn.commit()
        print(f"  ✓ seaport_counter table synced: {chain}/{wallet[:10]}... next_counter={new_counter}")
    finally:
        conn.close()


def main():
    execute = "--execute" in sys.argv

    from dotenv import load_dotenv
    load_dotenv("/app/.env", override=False)

    private_key = os.getenv("BUYER_WALLET_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: BUYER_WALLET_PRIVATE_KEY not set")
        sys.exit(1)

    account = Account.from_key(private_key)
    wallet = account.address
    print(f"Wallet: {wallet}")

    rpc = os.getenv("BUYER_RPC_URL", "https://bsc-dataseed.binance.org/").strip()
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to BSC RPC {rpc}")
        sys.exit(1)

    chain_id = w3.eth.chain_id
    print(f"Chain: BSC (id={chain_id})")

    seaport = w3.eth.contract(
        address=Web3.to_checksum_address(SEAPORT_ADDRESS),
        abi=SEAPORT_ABI,
    )
    current_counter = seaport.functions.getCounter(wallet).call()
    print(f"Current Seaport counter: {current_counter}")

    balance = w3.eth.get_balance(wallet)
    balance_bnb = w3.from_wei(balance, "ether")
    print(f"BNB balance: {balance_bnb:.6f}")

    if balance_bnb < 0.001:
        print("WARNING: Very low BNB, may not have enough for gas")

    print(f"\n{'='*60}")
    print(f"incrementCounter() will invalidate ALL Seaport orders")
    print(f"from wallet {wallet} on BSC chain")
    print(f"Counter: {current_counter} → {current_counter + 1}")
    print(f"Invalidates: ALL offers + ALL listings signed with counter={current_counter}")
    print(f"{'='*60}")

    if not execute:
        print("\n*** DRY RUN — add --execute to send the transaction ***")
        return

    print("\nBuilding transaction...")
    nonce = w3.eth.get_transaction_count(wallet)
    gas_price = w3.eth.gas_price

    tx = seaport.functions.incrementCounter().build_transaction({
        "from": wallet,
        "nonce": nonce,
        "gasPrice": gas_price,
        "chainId": chain_id,
    })

    try:
        gas_estimate = w3.eth.estimate_gas(tx)
        tx["gas"] = int(gas_estimate * 1.2)
        cost_bnb = w3.from_wei(gas_price * tx["gas"], "ether")
        print(f"Gas: {gas_estimate} (with 20% buffer: {tx['gas']})")
        print(f"Gas price: {w3.from_wei(gas_price, 'gwei'):.1f} gwei")
        print(f"Estimated cost: {cost_bnb:.6f} BNB")
    except Exception as exc:
        print(f"Gas estimate failed: {exc}")
        tx["gas"] = 100000

    print("\nSigning...")
    signed_tx = account.sign_transaction(tx)

    print("Sending...")
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"TX hash: {tx_hash.hex()}")
    print(f"BSCScan: https://bscscan.com/tx/{tx_hash.hex()}")

    print("\nWaiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = receipt.get("status", 0)
    gas_used = receipt.get("gasUsed", 0)
    cost_bnb = w3.from_wei(gas_price * gas_used, "ether")

    if status == 1:
        new_counter = seaport.functions.getCounter(wallet).call()
        print(f"\n✅ SUCCESS! Counter: {current_counter} → {new_counter}")
        print(f"Gas used: {gas_used} ({cost_bnb:.6f} BNB)")
        sync_db_counter(wallet, "bsc", new_counter)
        print(f"\nAll old BSC Seaport orders INVALID. Bot will place fresh offers next cycle.")
    else:
        print(f"\n❌ FAILED! TX reverted. Gas used: {gas_used}")
        sys.exit(1)


if __name__ == "__main__":
    main()
