from __future__ import annotations

import argparse
import json

from okx_nft_bot import cli as legacy_cli
from okx_nft_bot.config import load_settings
from okx_nft_bot.storage.sqlite import SQLiteStore


def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError('legacy CLI parser has no subparsers')


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


def cmd_resolve_notification_attempt(
    *,
    channel: str,
    event_id: str,
    resolution: str,
    force: bool,
) -> int:
    if not force:
        raise SystemExit('resolve-notification-attempt requires --yes')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    resolved = store.resolve_notification_attempt(
        channel,
        event_id,
        resolution=resolution,
    )
    if not resolved:
        raise SystemExit(
            f'No matching notification attempt: channel={channel} event_id={event_id}'
        )
    print(
        json.dumps(
            {
                'resolved': True,
                'channel': channel,
                'event_id': event_id,
                'resolution': resolution,
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
        help='List durable ambiguous notification delivery attempts',
    )
    attempts.add_argument('--channel', default=None)
    attempts.add_argument('--event-id', default=None)
    attempts.add_argument('--limit', type=int, default=100)

    resolve = subparsers.add_parser(
        'resolve-notification-attempt',
        help='Explicitly reconcile one ambiguous notification delivery attempt',
    )
    resolve.add_argument('--channel', required=True)
    resolve.add_argument('--event-id', required=True)
    resolve.add_argument(
        '--resolution',
        choices=['mark-sent', 'release-for-retry'],
        required=True,
    )
    resolve.add_argument('--yes', action='store_true')
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
    if args.command == 'resolve-notification-attempt':
        return cmd_resolve_notification_attempt(
            channel=args.channel,
            event_id=args.event_id,
            resolution=args.resolution,
            force=args.yes,
        )
    return legacy_cli.main()


if __name__ == '__main__':
    raise SystemExit(main())
