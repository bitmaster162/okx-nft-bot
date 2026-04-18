from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid import CounterBidder, CounterbidConfigManager
from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.signing import preview_counterbid
from okx_nft_bot.undercutter import PositionState, UndercutEngine, UndercutScheduler


def _ensure_bsc(chain: str) -> str:
    resolved = chain.lower()
    if resolved != "bsc":
        raise SystemExit("Execution track currently supports only --chain bsc")
    return resolved


def _load_state(settings) -> PositionState:
    state = PositionState(settings.execution_db_path)
    state.audit_integrity()
    return state


def cmd_preview_counterbid(collection: str, price: float, chain: str) -> int:
    settings = load_settings()
    payload = preview_counterbid(settings=settings, collection=collection, price_bnb=price, chain=_ensure_bsc(chain))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_counterbid_config(
    *,
    action: str,
    address: str | None,
    chain: str,
    min_price: float,
    max_price: float,
    margin: float,
) -> int:
    settings = load_settings()
    manager = CounterbidConfigManager(settings.execution_db_path)
    chain = _ensure_bsc(chain)
    if action == "list":
        print(json.dumps(manager.describe(), ensure_ascii=False, indent=2))
        return 0
    if not address:
        raise SystemExit("--address is required for this action")
    if action == "add":
        config = manager.add_collection(
            address=address,
            chain=chain,
            min_price_bnb=min_price,
            max_price_bnb=max_price,
            margin_bnb=margin,
        )
        print(json.dumps(asdict(config), ensure_ascii=False, indent=2))
        return 0
    if action == "remove":
        print(json.dumps({"removed": manager.remove_collection(address)}, ensure_ascii=False, indent=2))
        return 0
    if action == "enable":
        print(json.dumps({"enabled": manager.enable_collection(address)}, ensure_ascii=False, indent=2))
        return 0
    if action == "disable":
        print(json.dumps({"disabled": manager.disable_collection(address)}, ensure_ascii=False, indent=2))
        return 0
    raise SystemExit(f"Unsupported action: {action}")


def cmd_counterbid_scan(*, collection: str | None, chain: str, refresh: bool) -> int:
    settings = load_settings()
    bidder = CounterBidder(settings=settings)
    result = bidder.process_batch(chain=_ensure_bsc(chain), refresh=refresh, sign_preview=False, collection=collection)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_counterbid_run(*, collection: str | None, chain: str, refresh: bool) -> int:
    settings = load_settings()
    bidder = CounterBidder(settings=settings)
    result = bidder.process_batch(chain=_ensure_bsc(chain), refresh=refresh, sign_preview=True, collection=collection)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_counterbid_status(*, chain: str) -> int:
    settings = load_settings()
    bidder = CounterBidder(settings=settings)
    print(json.dumps(bidder.status(chain=_ensure_bsc(chain)), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_undercut_run(*, chain: str, refresh: bool) -> int:
    settings = load_settings()
    engine = UndercutEngine(settings=settings)
    actions = engine.run_cycle(chain=_ensure_bsc(chain), refresh=refresh)
    print(json.dumps([asdict(action) for action in actions], ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_undercut_daemon(*, chain: str, cycles: int, interval_seconds: int | None, refresh: bool) -> int:
    settings = load_settings()
    engine = UndercutEngine(settings=settings)
    scheduler = UndercutScheduler(engine=engine, settings=settings)
    result = scheduler.run_daemon(
        cycles=cycles,
        interval_seconds=interval_seconds,
        chain=_ensure_bsc(chain),
        refresh=refresh,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_undercut_status(*, chain: str) -> int:
    settings = load_settings()
    engine = UndercutEngine(settings=settings)
    print(json.dumps(engine.status(chain=_ensure_bsc(chain)), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_undercut_history(*, collection: str | None, limit: int) -> int:
    settings = load_settings()
    state = _load_state(settings)
    print(json.dumps(state.list_action_history(collection=collection, limit=limit), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_undercut_withdraw(*, collection: str) -> int:
    settings = load_settings()
    state = _load_state(settings)
    count = state.withdraw_collection(collection)
    state.log_action(
        action_type="WITHDRAW",
        collection=collection,
        chain=settings.execution_chain,
        order_hash=None,
        old_price_bnb=None,
        new_price_bnb=None,
        reason="Manual withdraw command",
        executed=True,
        payload={"count": count},
    )
    print(json.dumps({"withdrawn": count, "collection": collection.lower()}, ensure_ascii=False, indent=2))
    return 0


def cmd_reconcile_state(*, chain: str) -> int:
    settings = load_settings()
    governor = ExecutionGovernor(settings=settings)
    result = governor.reconcile_active_offers(chain=_ensure_bsc(chain))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_audit_state() -> int:
    settings = load_settings()
    state = PositionState(settings.execution_db_path)
    print(json.dumps(state.audit_integrity().to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_arm_live(*, minutes: int, reason: str | None) -> int:
    settings = load_settings()
    state = _load_state(settings)
    payload = state.arm_live(minutes=minutes, actor="execution_cli", reason=reason)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_disarm_live(*, reason: str | None) -> int:
    settings = load_settings()
    state = _load_state(settings)
    payload = state.disarm_live(actor="execution_cli", reason=reason)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="okx-nft-exec")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview-counterbid", help="Build and sign a dry-run Seaport counterbid preview")
    preview.add_argument("--collection", required=True)
    preview.add_argument("--price", required=True, type=float)
    preview.add_argument("--chain", default="bsc")

    counter_scan = subparsers.add_parser("counterbid-scan", help="Scan configured collections for parasite offers")
    counter_scan.add_argument("--collection", default=None)
    counter_scan.add_argument("--chain", default="bsc")
    counter_scan.add_argument("--refresh", action="store_true")

    counter_run = subparsers.add_parser("counterbid-run", help="Dry-run counterbid cycle with optional signed preview payloads")
    counter_run.add_argument("--collection", default=None)
    counter_run.add_argument("--chain", default="bsc")
    counter_run.add_argument("--refresh", action="store_true")

    counter_config = subparsers.add_parser("counterbid-config", help="Manage execution collection configs")
    counter_config.add_argument("action", choices=["add", "remove", "list", "enable", "disable"])
    counter_config.add_argument("--address", default=None)
    counter_config.add_argument("--chain", default="bsc")
    counter_config.add_argument("--min-price", type=float, default=0.01)
    counter_config.add_argument("--max-price", type=float, default=1.0)
    counter_config.add_argument("--margin", type=float, default=0.001)

    counter_status = subparsers.add_parser("counterbid-status", help="Show execution collection config and parasite wallet status")
    counter_status.add_argument("--chain", default="bsc")

    undercut_run = subparsers.add_parser("undercut-run", help="Run one dry-run undercut cycle")
    undercut_run.add_argument("--chain", default="bsc")
    undercut_run.add_argument("--refresh", action="store_true")

    undercut_daemon = subparsers.add_parser("undercut-daemon", help="Run repeated dry-run undercut cycles")
    undercut_daemon.add_argument("--chain", default="bsc")
    undercut_daemon.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 = infinite)")
    undercut_daemon.add_argument("--interval-seconds", "--interval", dest="interval_seconds", type=int, default=None)
    undercut_daemon.add_argument("--refresh", action="store_true")

    undercut_status = subparsers.add_parser("undercut-status", help="Show active dry-run offers and recent actions")
    undercut_status.add_argument("--chain", default="bsc")

    undercut_history = subparsers.add_parser("undercut-history", help="Show recent undercut action history")
    undercut_history.add_argument("--collection", default=None)
    undercut_history.add_argument("--limit", type=int, default=20)

    undercut_withdraw = subparsers.add_parser("undercut-withdraw", help="Mark all active dry-run offers for a collection as withdrawn")
    undercut_withdraw.add_argument("--collection", required=True)

    reconcile = subparsers.add_parser("reconcile-state", help="Reconcile local execution state against active OKX offers")
    reconcile.add_argument("--chain", default="bsc")

    subparsers.add_parser("audit-state", help="Audit and quarantine malformed execution state")

    arm_live = subparsers.add_parser("arm-live", help="Open a short-lived live execution window")
    arm_live.add_argument("--minutes", type=int, default=15)
    arm_live.add_argument("--reason", default=None)

    disarm_live = subparsers.add_parser("disarm-live", help="Close the current live execution window")
    disarm_live.add_argument("--reason", default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "preview-counterbid":
        return cmd_preview_counterbid(collection=args.collection, price=args.price, chain=args.chain)
    if args.command == "counterbid-config":
        return cmd_counterbid_config(
            action=args.action,
            address=args.address,
            chain=args.chain,
            min_price=args.min_price,
            max_price=args.max_price,
            margin=args.margin,
        )
    if args.command == "counterbid-scan":
        return cmd_counterbid_scan(collection=args.collection, chain=args.chain, refresh=args.refresh)
    if args.command == "counterbid-run":
        return cmd_counterbid_run(collection=args.collection, chain=args.chain, refresh=args.refresh)
    if args.command == "counterbid-status":
        return cmd_counterbid_status(chain=args.chain)
    if args.command == "undercut-run":
        return cmd_undercut_run(chain=args.chain, refresh=args.refresh)
    if args.command == "undercut-daemon":
        return cmd_undercut_daemon(
            chain=args.chain,
            cycles=args.cycles,
            interval_seconds=args.interval_seconds,
            refresh=args.refresh,
        )
    if args.command == "undercut-status":
        return cmd_undercut_status(chain=args.chain)
    if args.command == "undercut-history":
        return cmd_undercut_history(collection=args.collection, limit=args.limit)
    if args.command == "undercut-withdraw":
        return cmd_undercut_withdraw(collection=args.collection)
    if args.command == "reconcile-state":
        return cmd_reconcile_state(chain=args.chain)
    if args.command == "audit-state":
        return cmd_audit_state()
    if args.command == "arm-live":
        return cmd_arm_live(minutes=args.minutes, reason=args.reason)
    if args.command == "disarm-live":
        return cmd_disarm_live(reason=args.reason)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
