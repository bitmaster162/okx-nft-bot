"""
cli.py  –  Parasite-Killer v15
==============================
Command-line interface for counter-bidding operations.

Commands:
  add-collection     — Register a collection
  list-collections   — Show all configured collections
  remove-collection  — Remove a collection
  counterbid         — Counter-bid on a single collection
  counterbid-all     — Counter-bid on all enabled collections
  monitor            — Continuous monitoring loop
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

from eth_account import Account

from config import ConfigManager
from counter_bidder import CounterBidder
from okx_api import OKXAPIClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger(__name__)


def _load_private_key() -> str:
    """Load PRIVATE_KEY from environment or .env file."""
    pk = os.environ.get("PRIVATE_KEY")
    if pk:
        return pk

    # Try .env in cwd
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
        "PRIVATE_KEY not found. Set it in .env or environment:\n"
        "  PRIVATE_KEY=0xYOUR_KEY_HERE"
    )


def cmd_add_collection(args):
    """Add a collection to config."""
    config = ConfigManager(args.db)
    cfg = config.add_collection(
        address=args.address,
        chain=args.chain,
        min_price_bnb=args.min_price,
        max_price_bnb=args.max_price,
        margin_bnb=args.margin,
        enabled=True,
    )
    print(f"✓ Added collection: {cfg.address}")
    print(f"  Chain:       {cfg.chain}")
    print(f"  Min price:   {cfg.min_price_bnb:.6f} BNB")
    print(f"  Max price:   {cfg.max_price_bnb:.6f} BNB")
    print(f"  Margin:      {cfg.margin_bnb:.6f} BNB")


def cmd_list_collections(args):
    """List all configured collections."""
    config = ConfigManager(args.db)
    collections = config.list_collections(chain=args.chain or None)

    if not collections:
        print("No collections configured.")
        return

    print(f"\n{'Address':<42} | {'Chain':<8} | {'Min':<10} | {'Max':<10} | {'Margin':<10} | {'Status':<8}")
    print("-" * 110)
    for cfg in collections:
        status = "✓ enabled" if cfg.enabled else "✗ disabled"
        print(
            f"{cfg.address:<42} | {cfg.chain:<8} | "
            f"{cfg.min_price_bnb:<10.6f} | {cfg.max_price_bnb:<10.6f} | "
            f"{cfg.margin_bnb:<10.6f} | {status:<8}"
        )


def cmd_remove_collection(args):
    """Remove a collection from config."""
    config = ConfigManager(args.db)
    if config.remove_collection(args.address):
        print(f"✓ Removed collection: {args.address}")
    else:
        print(f"✗ Collection not found: {args.address}")


def cmd_counterbid(args):
    """Counter-bid on a single collection (dry-run by default)."""
    print("=" * 70)
    print("  PARASITE-KILLER v15  |  Counter-bid Single Collection")
    print("=" * 70)

    # Load credentials
    try:
        private_key = _load_private_key()
        account = Account.from_key(private_key)
        offerer = account.address
    except EnvironmentError as e:
        print(f"✗ {e}")
        sys.exit(1)

    # Init clients
    config = ConfigManager(args.db)
    okx = OKXAPIClient()
    bidder = CounterBidder(config, okx, offerer, private_key)

    print(f"\n[+] Offerer:     {offerer}")
    print(f"[+] Collection:  {args.collection}")
    print(f"[+] Chain:       {args.chain}")
    print(f"[+] Dry run:     {args.dry_run}")

    # Process single collection
    task = bidder.process_single_collection(args.collection, args.chain, dry_run=True)

    print(f"\n── ANALYSIS ──────────────────────────────────────────────────────")
    print(f"Parasite offer:  {task.parasite_offer_bnb:.6f} BNB")
    print(f"Counter bid:     {task.counter_price_bnb:.6f} BNB")
    print(f"Reason:          {task.reason}")
    print(f"Valid:           {'✓ YES' if task.valid else '✗ NO'}")
    if task.error:
        print(f"Error:           {task.error}")

    if task.valid and not args.dry_run:
        # Sign and prepare
        try:
            from seaport_signer import get_counter
            counter = get_counter(offerer)
            signed = bidder.build_signed_counter_bid(
                args.collection,
                task.counter_price_bnb,
                counter,
            )
            response = okx.submit_offer(signed, dry_run=False)
            print(f"\n── SUBMISSION ────────────────────────────────────────────────────")
            print(json.dumps(response, indent=2))
            if response.get("code") == "0":
                print("✓ Counter-bid submitted successfully!")
            else:
                print(f"✗ Submission failed: {response}")
        except Exception as e:
            print(f"✗ Error: {e}")
            sys.exit(1)

    print("\n" + "=" * 70)


def cmd_counterbid_all(args):
    """Counter-bid on all enabled collections (dry-run by default)."""
    print("=" * 70)
    print("  PARASITE-KILLER v15  |  Counter-bid All Collections")
    print("=" * 70)

    # Load credentials
    try:
        private_key = _load_private_key()
        account = Account.from_key(private_key)
        offerer = account.address
    except EnvironmentError as e:
        print(f"✗ {e}")
        sys.exit(1)

    # Init clients
    config = ConfigManager(args.db)
    okx = OKXAPIClient()
    bidder = CounterBidder(config, okx, offerer, private_key)

    print(f"\n[+] Offerer:  {offerer}")
    print(f"[+] Chain:    {args.chain}")
    print(f"[+] Dry run:  {args.dry_run}")

    # Process batch
    batch = bidder.process_batch(args.chain, dry_run=True)

    print(f"\n── BATCH RESULTS ─────────────────────────────────────────────────")
    print(f"Total tasks:     {len(batch.tasks)}")
    print(f"Valid bids:      {sum(1 for t in batch.tasks if t.valid)}")
    print(f"Failed/invalid:  {sum(1 for t in batch.tasks if not t.valid)}")

    print(f"\n── DETAILS ────────────────────────────────────────────────────────")
    for task in batch.tasks:
        status = "✓" if task.valid else "✗"
        print(f"{status} {task.collection}")
        print(f"    Parasite: {task.parasite_offer_bnb:.6f} BNB")
        print(f"    Counter:  {task.counter_price_bnb:.6f} BNB")
        print(f"    Reason:   {task.reason}")
        if task.error:
            print(f"    Error:    {task.error}")

    # If not dry-run, submit
    if not args.dry_run:
        print(f"\n── SUBMISSION ────────────────────────────────────────────────────")
        batch = bidder.submit_batch(batch, dry_run=False)
        print(f"Submitted: {batch.submitted_count}/{len(batch.signed_orders)}")
        if batch.failed_count > 0:
            print(f"Failed:    {batch.failed_count}")

    print("\n" + "=" * 70)


def cmd_monitor(args):
    """Continuous monitoring loop."""
    print("=" * 70)
    print("  PARASITE-KILLER v15  |  Continuous Monitor")
    print("=" * 70)

    # Load credentials
    try:
        private_key = _load_private_key()
        account = Account.from_key(private_key)
        offerer = account.address
    except EnvironmentError as e:
        print(f"✗ {e}")
        sys.exit(1)

    # Init clients
    config = ConfigManager(args.db)
    okx = OKXAPIClient()
    bidder = CounterBidder(config, okx, offerer, private_key)

    print(f"\n[+] Offerer:        {offerer}")
    print(f"[+] Chain:          {args.chain}")
    print(f"[+] Interval:       {args.interval} seconds")
    print(f"[+] Dry run:        {args.dry_run}")
    print(f"[+] Max iterations: {args.max_iterations or 'unlimited'}")
    print(f"\nStarting monitor... (Ctrl+C to stop)\n")

    iteration = 0
    try:
        while True:
            iteration += 1
            print(f"\n[{iteration}] {time.strftime('%Y-%m-%d %H:%M:%S')} — Starting batch process")

            # Process + submit
            batch = bidder.process_batch(args.chain, dry_run=True)
            if batch.signed_orders:
                batch = bidder.submit_batch(batch, dry_run=args.dry_run)
                print(f"    Valid: {sum(1 for t in batch.tasks if t.valid)} | "
                      f"Submitted: {batch.submitted_count}")

            if args.max_iterations and iteration >= args.max_iterations:
                print(f"\n[*] Reached max iterations ({args.max_iterations}). Stopping.")
                break

            # Wait for next cycle
            print(f"    Waiting {args.interval}s before next check...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[*] Monitor stopped by user.")


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Parasite-Killer v15 — Counter-bidder with limits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--db",
        default="parasite_killer_v15.db",
        help="SQLite database path (default: parasite_killer_v15.db)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # add-collection
    add_cmd = subparsers.add_parser("add-collection", help="Register a collection")
    add_cmd.add_argument("--address", required=True, help="NFT contract address")
    add_cmd.add_argument("--chain", default="bsc", help="Chain (default: bsc)")
    add_cmd.add_argument("--min-price", type=float, required=True, help="Min offer (BNB)")
    add_cmd.add_argument("--max-price", type=float, required=True, help="Max offer (BNB)")
    add_cmd.add_argument("--margin", type=float, required=True, help="Undercut margin (BNB)")
    add_cmd.set_defaults(func=cmd_add_collection)

    # list-collections
    list_cmd = subparsers.add_parser("list-collections", help="List configured collections")
    list_cmd.add_argument("--chain", help="Filter by chain")
    list_cmd.set_defaults(func=cmd_list_collections)

    # remove-collection
    rm_cmd = subparsers.add_parser("remove-collection", help="Remove a collection")
    rm_cmd.add_argument("--address", required=True, help="NFT contract address")
    rm_cmd.set_defaults(func=cmd_remove_collection)

    # counterbid (single)
    cb_cmd = subparsers.add_parser("counterbid", help="Counter-bid on single collection")
    cb_cmd.add_argument("--collection", required=True, help="NFT contract address")
    cb_cmd.add_argument("--chain", default="bsc", help="Chain (default: bsc)")
    cb_cmd.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit (default: dry-run only)",
    )
    cb_cmd.set_defaults(func=cmd_counterbid, dry_run=True)

    # counterbid-all
    cba_cmd = subparsers.add_parser("counterbid-all", help="Counter-bid on all collections")
    cba_cmd.add_argument("--chain", default="bsc", help="Chain (default: bsc)")
    cba_cmd.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit (default: dry-run only)",
    )
    cba_cmd.set_defaults(func=cmd_counterbid_all, dry_run=True)

    # monitor
    mon_cmd = subparsers.add_parser("monitor", help="Continuous monitor loop")
    mon_cmd.add_argument("--chain", default="bsc", help="Chain (default: bsc)")
    mon_cmd.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Check interval in seconds (default: 300 = 5 min)",
    )
    mon_cmd.add_argument(
        "--max-iterations",
        type=int,
        help="Stop after N iterations (optional)",
    )
    mon_cmd.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit (default: dry-run only)",
    )
    mon_cmd.set_defaults(func=cmd_monitor, dry_run=True)

    args = parser.parse_args()

    # Handle --submit flag
    if hasattr(args, "func"):
        if args.command in ["counterbid", "counterbid-all", "monitor"]:
            args.dry_run = not args.submit

        args.func(args)


if __name__ == "__main__":
    main()
