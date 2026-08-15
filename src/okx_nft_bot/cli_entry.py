from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from okx_nft_bot import cli as legacy_cli
from okx_nft_bot.config import load_settings
from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore
from okx_nft_bot.storage.sqlite import SQLiteStore


def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError('legacy CLI parser has no subparsers')


def _execution_db_path() -> Path:
    return Path(os.getenv('EXECUTION_DB_PATH', './data/execution.sqlite3'))


def cmd_notification_attempts(
    *,
    channel: str | None,
    event_id: str | None,
    limit: int,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_notification_attempts(
        channel=channel,
        event_id=event_id,
        limit=limit,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_notification_resolutions(
    *,
    channel: str | None,
    event_id: str | None,
    resolution: str | None,
    limit: int,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_notification_resolutions(
        channel=channel,
        event_id=event_id,
        resolution=resolution,
        limit=limit,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mark_notification_attempt_ambiguous(
    *,
    channel: str,
    event_id: str,
    sender_stopped: bool,
    force: bool,
    actor: str | None = None,
    reason: str | None = None,
) -> int:
    if not force:
        raise SystemExit('mark-notification-attempt-ambiguous requires --yes')
    if not sender_stopped:
        raise SystemExit('mark-notification-attempt-ambiguous requires --sender-stopped')
    resolved_actor = str(actor or '').strip()
    resolved_reason = str(reason or '').strip()
    if not resolved_actor or not resolved_reason:
        raise SystemExit('mark-notification-attempt-ambiguous requires --actor and --reason')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    marked = store.mark_notification_attempt_ambiguous(
        channel,
        event_id,
        actor=resolved_actor,
        reason=resolved_reason,
    )
    if not marked:
        raise SystemExit(
            f'No active notification attempt: channel={channel} event_id={event_id}'
        )
    print(
        json.dumps(
            {
                'marked_ambiguous': True,
                'channel': channel,
                'event_id': event_id,
                'sender_stopped': True,
                'actor': resolved_actor,
                'reason': resolved_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_resolve_notification_attempt(
    *,
    channel: str,
    event_id: str,
    resolution: str,
    force: bool,
    actor: str | None = None,
    reason: str | None = None,
) -> int:
    if not force:
        raise SystemExit('resolve-notification-attempt requires --yes')
    resolved_actor = str(actor or '').strip()
    resolved_reason = str(reason or '').strip()
    if not resolved_actor or not resolved_reason:
        raise SystemExit('resolve-notification-attempt requires --actor and --reason')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    resolved = store.resolve_notification_attempt(
        channel,
        event_id,
        resolution=resolution,
        actor=resolved_actor,
        reason=resolved_reason,
    )
    if not resolved:
        raise SystemExit(
            f'No matching notification attempt eligible for this resolution: channel={channel} event_id={event_id}'
        )
    print(
        json.dumps(
            {
                'resolved': True,
                'channel': channel,
                'event_id': event_id,
                'resolution': resolution,
                'actor': resolved_actor,
                'reason': resolved_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_instant_buy_claims(
    *,
    wallet: str | None,
    chain: str | None,
    order_id: str | None,
    state: str | None,
    limit: int,
) -> int:
    store = DurablePendingEffectStore(_execution_db_path())
    rows = store.fetch_claims(
        wallet=wallet,
        chain=chain,
        order_id=order_id,
        state=state,
        limit=limit,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_instant_buy_resolutions(
    *,
    wallet: str | None,
    chain: str | None,
    order_id: str | None,
    resolution: str | None,
    limit: int,
) -> int:
    store = DurablePendingEffectStore(_execution_db_path())
    rows = store.fetch_resolutions(
        wallet=wallet,
        chain=chain,
        order_id=order_id,
        resolution=resolution,
        limit=limit,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mark_instant_buy_claim_pending(
    *,
    wallet: str,
    chain: str,
    order_id: str,
    worker_stopped: bool,
    force: bool,
    actor: str | None = None,
    reason: str | None = None,
) -> int:
    if not force:
        raise SystemExit('mark-instant-buy-claim-pending requires --yes')
    if not worker_stopped:
        raise SystemExit('mark-instant-buy-claim-pending requires --worker-stopped')
    resolved_actor = str(actor or '').strip()
    resolved_reason = str(reason or '').strip()
    if not resolved_actor or not resolved_reason:
        raise SystemExit('mark-instant-buy-claim-pending requires --actor and --reason')
    store = DurablePendingEffectStore(_execution_db_path())
    marked = store.mark_reserved_pending(
        wallet=wallet,
        chain=chain,
        order_id=order_id,
        actor=resolved_actor,
        reason=resolved_reason,
    )
    if not marked:
        raise SystemExit(
            'No matching reserved instant-buy claim eligible for pending transition: '
            f'wallet={wallet} chain={chain} order_id={order_id}'
        )
    print(
        json.dumps(
            {
                'marked_pending': True,
                'wallet': wallet,
                'chain': chain,
                'order_id': order_id,
                'worker_stopped': True,
                'actor': resolved_actor,
                'reason': resolved_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_resolve_instant_buy_claim(
    *,
    wallet: str,
    chain: str,
    order_id: str,
    resolution: str,
    worker_stopped: bool = False,
    force: bool,
    actor: str | None = None,
    reason: str | None = None,
) -> int:
    if not force:
        raise SystemExit('resolve-instant-buy-claim requires --yes')
    if resolution == 'release-for-retry' and not worker_stopped:
        raise SystemExit(
            'resolve-instant-buy-claim release-for-retry requires --worker-stopped'
        )
    resolved_actor = str(actor or '').strip()
    resolved_reason = str(reason or '').strip()
    if not resolved_actor or not resolved_reason:
        raise SystemExit('resolve-instant-buy-claim requires --actor and --reason')
    store = DurablePendingEffectStore(_execution_db_path())
    resolved = store.resolve_claim(
        wallet=wallet,
        chain=chain,
        order_id=order_id,
        resolution=resolution,
        actor=resolved_actor,
        reason=resolved_reason,
    )
    if not resolved:
        raise SystemExit(
            'No matching instant-buy claim eligible for this resolution: '
            f'wallet={wallet} chain={chain} order_id={order_id}'
        )
    print(
        json.dumps(
            {
                'resolved': True,
                'wallet': wallet,
                'chain': chain,
                'order_id': order_id,
                'resolution': resolution,
                'actor': resolved_actor,
                'reason': resolved_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = legacy_cli.build_parser()
    subparsers = _subparsers_action(parser)

    attempts = subparsers.add_parser(
        'notification-attempts',
        help='List durable notification delivery attempts',
    )
    attempts.add_argument('--channel', default=None)
    attempts.add_argument('--event-id', default=None)
    attempts.add_argument('--limit', type=int, default=100)

    notification_resolutions = subparsers.add_parser(
        'notification-resolutions',
        help='List durable notification reconciliation history',
    )
    notification_resolutions.add_argument('--channel', default=None)
    notification_resolutions.add_argument('--event-id', default=None)
    notification_resolutions.add_argument(
        '--resolution',
        choices=['mark-ambiguous', 'mark-sent', 'release-for-retry'],
        default=None,
    )
    notification_resolutions.add_argument('--limit', type=int, default=100)

    mark_ambiguous = subparsers.add_parser(
        'mark-notification-attempt-ambiguous',
        help='Mark an active notification attempt ambiguous after sender shutdown is confirmed',
    )
    mark_ambiguous.add_argument('--channel', required=True)
    mark_ambiguous.add_argument('--event-id', required=True)
    mark_ambiguous.add_argument('--sender-stopped', action='store_true', required=True)
    mark_ambiguous.add_argument('--actor', required=True)
    mark_ambiguous.add_argument('--reason', required=True)
    mark_ambiguous.add_argument('--yes', action='store_true')

    resolve = subparsers.add_parser(
        'resolve-notification-attempt',
        help='Explicitly reconcile one notification delivery attempt',
    )
    resolve.add_argument('--channel', required=True)
    resolve.add_argument('--event-id', required=True)
    resolve.add_argument(
        '--resolution',
        choices=['mark-sent', 'release-for-retry'],
        required=True,
    )
    resolve.add_argument('--actor', required=True)
    resolve.add_argument('--reason', required=True)
    resolve.add_argument('--yes', action='store_true')

    claims = subparsers.add_parser(
        'instant-buy-claims',
        help='List durable instant-buy effect claims',
    )
    claims.add_argument('--wallet', default=None)
    claims.add_argument('--chain', default=None)
    claims.add_argument('--order-id', default=None)
    claims.add_argument('--state', choices=['reserved', 'pending', 'completed'], default=None)
    claims.add_argument('--limit', type=int, default=100)

    resolutions = subparsers.add_parser(
        'instant-buy-resolutions',
        help='List durable instant-buy reconciliation history',
    )
    resolutions.add_argument('--wallet', default=None)
    resolutions.add_argument('--chain', default=None)
    resolutions.add_argument('--order-id', default=None)
    resolutions.add_argument(
        '--resolution',
        choices=['mark-pending', 'mark-completed', 'release-for-retry'],
        default=None,
    )
    resolutions.add_argument('--limit', type=int, default=100)

    mark_pending = subparsers.add_parser(
        'mark-instant-buy-claim-pending',
        help='Move a reserved instant-buy claim into pending reconciliation after worker shutdown is confirmed',
    )
    mark_pending.add_argument('--wallet', required=True)
    mark_pending.add_argument('--chain', required=True)
    mark_pending.add_argument('--order-id', required=True)
    mark_pending.add_argument('--worker-stopped', action='store_true', required=True)
    mark_pending.add_argument('--actor', required=True)
    mark_pending.add_argument('--reason', required=True)
    mark_pending.add_argument('--yes', action='store_true')

    resolve_claim = subparsers.add_parser(
        'resolve-instant-buy-claim',
        help='Explicitly reconcile one durable instant-buy effect claim',
    )
    resolve_claim.add_argument('--wallet', required=True)
    resolve_claim.add_argument('--chain', required=True)
    resolve_claim.add_argument('--order-id', required=True)
    resolve_claim.add_argument(
        '--resolution',
        choices=['mark-completed', 'release-for-retry'],
        required=True,
    )
    resolve_claim.add_argument('--worker-stopped', action='store_true')
    resolve_claim.add_argument('--actor', required=True)
    resolve_claim.add_argument('--reason', required=True)
    resolve_claim.add_argument('--yes', action='store_true')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'notification-attempts':
        return cmd_notification_attempts(
            channel=args.channel,
            event_id=args.event_id,
            limit=args.limit,
        )
    if args.command == 'notification-resolutions':
        return cmd_notification_resolutions(
            channel=args.channel,
            event_id=args.event_id,
            resolution=args.resolution,
            limit=args.limit,
        )
    if args.command == 'mark-notification-attempt-ambiguous':
        return cmd_mark_notification_attempt_ambiguous(
            channel=args.channel,
            event_id=args.event_id,
            sender_stopped=args.sender_stopped,
            force=args.yes,
            actor=args.actor,
            reason=args.reason,
        )
    if args.command == 'resolve-notification-attempt':
        return cmd_resolve_notification_attempt(
            channel=args.channel,
            event_id=args.event_id,
            resolution=args.resolution,
            force=args.yes,
            actor=args.actor,
            reason=args.reason,
        )
    if args.command == 'instant-buy-claims':
        return cmd_instant_buy_claims(
            wallet=args.wallet,
            chain=args.chain,
            order_id=args.order_id,
            state=args.state,
            limit=args.limit,
        )
    if args.command == 'instant-buy-resolutions':
        return cmd_instant_buy_resolutions(
            wallet=args.wallet,
            chain=args.chain,
            order_id=args.order_id,
            resolution=args.resolution,
            limit=args.limit,
        )
    if args.command == 'mark-instant-buy-claim-pending':
        return cmd_mark_instant_buy_claim_pending(
            wallet=args.wallet,
            chain=args.chain,
            order_id=args.order_id,
            worker_stopped=args.worker_stopped,
            force=args.yes,
            actor=args.actor,
            reason=args.reason,
        )
    if args.command == 'resolve-instant-buy-claim':
        return cmd_resolve_instant_buy_claim(
            wallet=args.wallet,
            chain=args.chain,
            order_id=args.order_id,
            resolution=args.resolution,
            worker_stopped=args.worker_stopped,
            force=args.yes,
            actor=args.actor,
            reason=args.reason,
        )
    return legacy_cli.main()


if __name__ == '__main__':
    raise SystemExit(main())
