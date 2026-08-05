from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from okx_nft_bot.config import Settings
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.sqlite import SQLiteStore


CurrencyBreakdown = dict[str, float]


@dataclass(slots=True)
class ClosedPosition:
    collection: str
    contract_address: str | None
    token_id: str
    quantity: int
    currency: str
    buy_event_id: str
    sell_event_id: str
    entry_market: str
    exit_market: str
    entry_time: str
    exit_time: str
    entry_unit_price: float
    exit_unit_price: float
    entry_total: float
    exit_total: float
    realized_pnl: float
    realized_pnl_pct: float | None
    hold_hours: float
    counterparty_in: str | None = None
    counterparty_out: str | None = None
    entry_order_hash: str | None = None
    entry_fill_engine: str | None = None
    entry_fill_confidence: float | None = None
    entry_fill_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "contract_address": self.contract_address,
            "token_id": self.token_id,
            "quantity": self.quantity,
            "currency": self.currency,
            "buy_event_id": self.buy_event_id,
            "sell_event_id": self.sell_event_id,
            "entry_market": self.entry_market,
            "exit_market": self.exit_market,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_unit_price": round(self.entry_unit_price, 6),
            "exit_unit_price": round(self.exit_unit_price, 6),
            "entry_total": round(self.entry_total, 6),
            "exit_total": round(self.exit_total, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "realized_pnl_pct": round(self.realized_pnl_pct, 4) if self.realized_pnl_pct is not None else None,
            "hold_hours": round(self.hold_hours, 2),
            "counterparty_in": self.counterparty_in,
            "counterparty_out": self.counterparty_out,
            "entry_order_hash": self.entry_order_hash,
            "entry_fill_engine": self.entry_fill_engine,
            "entry_fill_confidence": round(self.entry_fill_confidence, 4) if self.entry_fill_confidence is not None else None,
            "entry_fill_confirmed": self.entry_fill_confirmed,
        }


@dataclass(slots=True)
class OpenPosition:
    collection: str
    contract_address: str | None
    token_id: str
    quantity: int
    currency: str
    entry_event_id: str
    entry_market: str
    entry_time: str
    entry_unit_price: float
    entry_total: float
    age_hours: float
    reference_unit_price: float | None
    reference_total: float | None
    reference_source: str | None
    reference_event_time: str | None
    unrealized_pnl: float | None
    unrealized_pnl_pct: float | None
    counterparty_in: str | None = None
    entry_order_hash: str | None = None
    entry_fill_engine: str | None = None
    entry_fill_confidence: float | None = None
    entry_fill_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "contract_address": self.contract_address,
            "token_id": self.token_id,
            "quantity": self.quantity,
            "currency": self.currency,
            "entry_event_id": self.entry_event_id,
            "entry_market": self.entry_market,
            "entry_time": self.entry_time,
            "entry_unit_price": round(self.entry_unit_price, 6),
            "entry_total": round(self.entry_total, 6),
            "age_hours": round(self.age_hours, 2),
            "reference_unit_price": round(self.reference_unit_price, 6) if self.reference_unit_price is not None else None,
            "reference_total": round(self.reference_total, 6) if self.reference_total is not None else None,
            "reference_source": self.reference_source,
            "reference_event_time": self.reference_event_time,
            "unrealized_pnl": round(self.unrealized_pnl, 6) if self.unrealized_pnl is not None else None,
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 4) if self.unrealized_pnl_pct is not None else None,
            "counterparty_in": self.counterparty_in,
            "entry_order_hash": self.entry_order_hash,
            "entry_fill_engine": self.entry_fill_engine,
            "entry_fill_confidence": round(self.entry_fill_confidence, 4) if self.entry_fill_confidence is not None else None,
            "entry_fill_confirmed": self.entry_fill_confirmed,
        }


@dataclass(slots=True)
class CollectionPnlSummary:
    collection: str
    contract_address: str | None
    currencies: tuple[str, ...]
    markets: tuple[str, ...]
    closed_position_count: int
    open_position_count: int
    priced_open_position_count: int
    orphan_sale_count: int
    win_rate: float | None
    average_hold_hours: float | None
    realized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    unrealized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_deployed_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_released_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_cost_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_value_by_currency: CurrencyBreakdown = field(default_factory=dict)
    latest_trade_at: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[float, int, int, str]:
        magnitude = sum(abs(v) for v in self.realized_pnl_by_currency.values()) + sum(abs(v) for v in self.unrealized_pnl_by_currency.values())
        return (-magnitude, -self.closed_position_count, -self.open_position_count, self.collection)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "contract_address": self.contract_address,
            "currencies": list(self.currencies),
            "markets": list(self.markets),
            "closed_position_count": self.closed_position_count,
            "open_position_count": self.open_position_count,
            "priced_open_position_count": self.priced_open_position_count,
            "orphan_sale_count": self.orphan_sale_count,
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "average_hold_hours": round(self.average_hold_hours, 2) if self.average_hold_hours is not None else None,
            "realized_pnl_by_currency": _round_breakdown(self.realized_pnl_by_currency),
            "unrealized_pnl_by_currency": _round_breakdown(self.unrealized_pnl_by_currency),
            "capital_deployed_by_currency": _round_breakdown(self.capital_deployed_by_currency),
            "capital_released_by_currency": _round_breakdown(self.capital_released_by_currency),
            "inventory_cost_by_currency": _round_breakdown(self.inventory_cost_by_currency),
            "inventory_value_by_currency": _round_breakdown(self.inventory_value_by_currency),
            "latest_trade_at": self.latest_trade_at,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class WalletPnlSummary:
    wallet: str
    trade_count: int
    buy_count: int
    sell_count: int
    open_position_count: int
    priced_open_position_count: int
    orphan_sale_count: int
    win_rate: float | None
    average_hold_hours: float | None
    currencies: tuple[str, ...]
    realized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    unrealized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_deployed_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_released_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_cost_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_value_by_currency: CurrencyBreakdown = field(default_factory=dict)
    latest_trade_at: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "trade_count": self.trade_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "open_position_count": self.open_position_count,
            "priced_open_position_count": self.priced_open_position_count,
            "orphan_sale_count": self.orphan_sale_count,
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "average_hold_hours": round(self.average_hold_hours, 2) if self.average_hold_hours is not None else None,
            "currencies": list(self.currencies),
            "realized_pnl_by_currency": _round_breakdown(self.realized_pnl_by_currency),
            "unrealized_pnl_by_currency": _round_breakdown(self.unrealized_pnl_by_currency),
            "capital_deployed_by_currency": _round_breakdown(self.capital_deployed_by_currency),
            "capital_released_by_currency": _round_breakdown(self.capital_released_by_currency),
            "inventory_cost_by_currency": _round_breakdown(self.inventory_cost_by_currency),
            "inventory_value_by_currency": _round_breakdown(self.inventory_value_by_currency),
            "latest_trade_at": self.latest_trade_at,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class WalletPnlReport:
    generated_at: str
    wallet: str
    summary: WalletPnlSummary
    collections: list[CollectionPnlSummary]
    open_positions: list[OpenPosition]
    closed_positions: list[ClosedPosition]
    orphan_sales: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "summary": self.summary.to_dict(),
            "collections": [item.to_dict() for item in self.collections],
            "open_positions": [item.to_dict() for item in self.open_positions],
            "closed_positions": [item.to_dict() for item in self.closed_positions],
            "orphan_sales": [dict(item) for item in self.orphan_sales],
        }


@dataclass(slots=True)
class _WalletTrade:
    event_id: str
    direction: str
    market: str
    collection: str
    contract_address: str | None
    token_id: str
    quantity: int
    currency: str
    total_price: float
    unit_price: float
    event_time: datetime
    counterparty: str | None
    tx_hash: str | None


@dataclass(slots=True)
class _OpenLot:
    event_id: str
    market: str
    collection: str
    contract_address: str | None
    token_id: str
    quantity: int
    remaining_quantity: int
    currency: str
    unit_price: float
    event_time: datetime
    counterparty: str | None
    order_hash: str | None = None
    fill_engine: str | None = None
    fill_confidence: float | None = None
    fill_confirmed: bool = False


@dataclass(slots=True)
class _PriceReference:
    unit_price: float
    currency: str
    source: str
    event_time: str | None


class WalletPnlAnalyzer:
    def __init__(self, *, settings: Settings, store: SQLiteStore | None = None) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        collection_limit: int | None = None,
        open_limit: int | None = None,
        closed_limit: int | None = None,
    ) -> WalletPnlReport:
        resolved_wallet = _normalize_wallet(wallet or self.settings.buyer_wallet_address)
        now = datetime.now(timezone.utc)
        if not resolved_wallet:
            summary = WalletPnlSummary(
                wallet="",
                trade_count=0,
                buy_count=0,
                sell_count=0,
                open_position_count=0,
                priced_open_position_count=0,
                orphan_sale_count=0,
                win_rate=None,
                average_hold_hours=None,
                currencies=(),
                notes=("wallet_not_configured",),
            )
            return WalletPnlReport(
                generated_at=now.isoformat(),
                wallet="",
                summary=summary,
                collections=[],
                open_positions=[],
                closed_positions=[],
                orphan_sales=[],
            )

        wallet_events = self.store.fetch_wallet_event_models(wallet=resolved_wallet)
        trades = _extract_wallet_trades(wallet_events, wallet=resolved_wallet)
        analysis_rows = self.store.fetch_analysis_events(limit=reference_limit or self.settings.wallet_pnl_reference_event_limit)
        asset_refs, collection_refs = _build_price_references(analysis_rows)
        fill_match_map = _build_fill_match_map(
            self.settings.execution_db_path,
            limit=reference_limit or self.settings.execution_fill_reference_event_limit,
        )

        open_lots: dict[tuple[tuple[str, str], str], deque[_OpenLot]] = defaultdict(deque)
        closed_positions: list[ClosedPosition] = []
        orphan_sales: list[dict[str, Any]] = []

        for trade in trades:
            asset_key = _asset_key(trade.contract_address, trade.token_id, trade.collection)
            queue_key = (asset_key, trade.currency)
            if trade.direction == "buy":
                fill_match = fill_match_map.get(trade.event_id, {})
                open_lots[queue_key].append(
                    _OpenLot(
                        event_id=trade.event_id,
                        market=trade.market,
                        collection=trade.collection,
                        contract_address=trade.contract_address,
                        token_id=trade.token_id,
                        quantity=trade.quantity,
                        remaining_quantity=trade.quantity,
                        currency=trade.currency,
                        unit_price=trade.unit_price,
                        event_time=trade.event_time,
                        counterparty=trade.counterparty,
                        order_hash=fill_match.get("order_hash"),
                        fill_engine=fill_match.get("engine"),
                        fill_confidence=float(fill_match["confidence"]) if fill_match.get("confidence") is not None else None,
                        fill_confirmed=bool(fill_match),
                    )
                )
                continue

            remaining = trade.quantity
            queue = open_lots.get(queue_key)
            while remaining > 0 and queue:
                lot = queue[0]
                matched_quantity = min(remaining, lot.remaining_quantity)
                entry_total = lot.unit_price * matched_quantity
                exit_total = trade.unit_price * matched_quantity
                realized = exit_total - entry_total
                hold_hours = max((trade.event_time - lot.event_time).total_seconds() / 3600.0, 0.0)
                pnl_pct = (realized / entry_total * 100.0) if entry_total > 0 else None
                closed_positions.append(
                    ClosedPosition(
                        collection=lot.collection,
                        contract_address=lot.contract_address,
                        token_id=lot.token_id,
                        quantity=matched_quantity,
                        currency=trade.currency,
                        buy_event_id=lot.event_id,
                        sell_event_id=trade.event_id,
                        entry_market=lot.market,
                        exit_market=trade.market,
                        entry_time=lot.event_time.isoformat(),
                        exit_time=trade.event_time.isoformat(),
                        entry_unit_price=lot.unit_price,
                        exit_unit_price=trade.unit_price,
                        entry_total=entry_total,
                        exit_total=exit_total,
                        realized_pnl=realized,
                        realized_pnl_pct=pnl_pct,
                        hold_hours=hold_hours,
                        counterparty_in=lot.counterparty,
                        counterparty_out=trade.counterparty,
                        entry_order_hash=lot.order_hash,
                        entry_fill_engine=lot.fill_engine,
                        entry_fill_confidence=lot.fill_confidence,
                        entry_fill_confirmed=lot.fill_confirmed,
                    )
                )
                lot.remaining_quantity -= matched_quantity
                remaining -= matched_quantity
                if lot.remaining_quantity <= 0:
                    queue.popleft()
            if remaining > 0:
                orphan_sales.append(
                    {
                        "event_id": trade.event_id,
                        "collection": trade.collection,
                        "contract_address": trade.contract_address,
                        "token_id": trade.token_id,
                        "quantity": remaining,
                        "currency": trade.currency,
                        "total_price": round(trade.unit_price * remaining, 6),
                        "market": trade.market,
                        "event_time": trade.event_time.isoformat(),
                        "reason": "sell_without_recorded_inventory",
                    }
                )

        open_positions: list[OpenPosition] = []
        for (asset_key, currency), queue in open_lots.items():
            for lot in queue:
                if lot.remaining_quantity <= 0:
                    continue
                collection_key = _collection_key(lot.contract_address, lot.collection)
                ref = asset_refs.get((asset_key, currency)) or collection_refs.get((collection_key, currency))
                entry_total = lot.unit_price * lot.remaining_quantity
                reference_total = ref.unit_price * lot.remaining_quantity if ref is not None else None
                unrealized = (reference_total - entry_total) if reference_total is not None else None
                unrealized_pct = (unrealized / entry_total * 100.0) if unrealized is not None and entry_total > 0 else None
                age_hours = max((now - lot.event_time).total_seconds() / 3600.0, 0.0)
                open_positions.append(
                    OpenPosition(
                        collection=lot.collection,
                        contract_address=lot.contract_address,
                        token_id=lot.token_id,
                        quantity=lot.remaining_quantity,
                        currency=currency,
                        entry_event_id=lot.event_id,
                        entry_market=lot.market,
                        entry_time=lot.event_time.isoformat(),
                        entry_unit_price=lot.unit_price,
                        entry_total=entry_total,
                        age_hours=age_hours,
                        reference_unit_price=ref.unit_price if ref is not None else None,
                        reference_total=reference_total,
                        reference_source=ref.source if ref is not None else None,
                        reference_event_time=ref.event_time if ref is not None else None,
                        unrealized_pnl=unrealized,
                        unrealized_pnl_pct=unrealized_pct,
                        counterparty_in=lot.counterparty,
                        entry_order_hash=lot.order_hash,
                        entry_fill_engine=lot.fill_engine,
                        entry_fill_confidence=lot.fill_confidence,
                        entry_fill_confirmed=lot.fill_confirmed,
                    )
                )

        closed_positions.sort(key=lambda item: (item.exit_time, item.sell_event_id), reverse=True)
        open_positions.sort(key=lambda item: (-item.age_hours, -item.entry_total, item.collection, item.token_id))

        collection_snapshots = _build_collection_summaries(
            closed_positions=closed_positions,
            open_positions=open_positions,
            orphan_sales=orphan_sales,
        )
        collection_snapshots.sort(key=lambda item: item.sort_key())

        summary = _build_wallet_summary(
            wallet=resolved_wallet,
            trades=trades,
            closed_positions=closed_positions,
            open_positions=open_positions,
            orphan_sales=orphan_sales,
        )

        if collection_limit is not None:
            collection_snapshots = collection_snapshots[: max(int(collection_limit), 0)]
        if open_limit is not None:
            open_positions = open_positions[: max(int(open_limit), 0)]
        if closed_limit is not None:
            closed_positions = closed_positions[: max(int(closed_limit), 0)]

        return WalletPnlReport(
            generated_at=now.isoformat(),
            wallet=resolved_wallet,
            summary=summary,
            collections=collection_snapshots,
            open_positions=open_positions,
            closed_positions=closed_positions,
            orphan_sales=orphan_sales,
        )

    def write_report(
        self,
        *,
        wallet: str | None = None,
        report_path: Path | None = None,
        reference_limit: int | None = None,
        collection_limit: int | None = None,
        open_limit: int | None = None,
        closed_limit: int | None = None,
    ) -> str:
        report = self.build_report(
            wallet=wallet,
            reference_limit=reference_limit,
            collection_limit=collection_limit,
            open_limit=open_limit,
            closed_limit=closed_limit,
        )
        target = report_path or self.settings.wallet_pnl_report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)


def format_wallet_pnl_text(
    report: WalletPnlReport,
    *,
    collection_limit: int = 5,
    position_limit: int = 5,
) -> str:
    summary = report.summary
    lines = ["wallet_pnl"]
    lines.append(f"wallet={summary.wallet or 'not_configured'}")
    lines.append(f"trades={summary.trade_count} buys={summary.buy_count} sells={summary.sell_count}")
    lines.append(
        f"open_positions={summary.open_position_count} priced_open={summary.priced_open_position_count} orphan_sales={summary.orphan_sale_count}"
    )
    lines.append(f"realized={_format_breakdown(summary.realized_pnl_by_currency, signed=True)}")
    lines.append(f"unrealized={_format_breakdown(summary.unrealized_pnl_by_currency, signed=True)}")
    lines.append(f"deployed={_format_breakdown(summary.capital_deployed_by_currency, signed=False)}")
    lines.append(f"released={_format_breakdown(summary.capital_released_by_currency, signed=False)}")
    lines.append(f"inventory_cost={_format_breakdown(summary.inventory_cost_by_currency, signed=False)}")
    lines.append(f"inventory_value={_format_breakdown(summary.inventory_value_by_currency, signed=False)}")
    if summary.win_rate is not None:
        lines.append(f"win_rate={summary.win_rate:.1f}%")
    if summary.average_hold_hours is not None:
        lines.append(f"avg_hold_hours={summary.average_hold_hours:.2f}")
    if summary.latest_trade_at:
        lines.append(f"latest_trade_at={summary.latest_trade_at}")
    if summary.notes:
        lines.append(f"notes={','.join(summary.notes)}")
    if report.collections:
        lines.append("collections:")
        for item in report.collections[: max(int(collection_limit), 0)]:
            lines.append(
                f"- {item.collection} | open={item.open_position_count} | closed={item.closed_position_count} | "
                f"realized={_format_breakdown(item.realized_pnl_by_currency, signed=True)} | "
                f"unrealized={_format_breakdown(item.unrealized_pnl_by_currency, signed=True)}"
            )
    if report.open_positions:
        lines.append("open_inventory:")
        for item in report.open_positions[: max(int(position_limit), 0)]:
            ref = (
                f"{item.reference_unit_price:.6f} {item.currency} ({item.reference_source})"
                if item.reference_unit_price is not None and item.reference_source
                else "n/a"
            )
            upnl = f"{item.unrealized_pnl:+.6f} {item.currency}" if item.unrealized_pnl is not None else "n/a"
            entry_src = ""
            if item.entry_fill_confirmed and item.entry_fill_engine:
                entry_src = f" | entry={item.entry_fill_engine}:{item.entry_fill_confidence:.2f}" if item.entry_fill_confidence is not None else f" | entry={item.entry_fill_engine}"
            lines.append(
                f"- {item.collection} #{item.token_id} | age={item.age_hours:.2f}h | cost={item.entry_total:.6f} {item.currency} | "
                f"ref={ref} | upnl={upnl}{entry_src}"
            )
    return "\n".join(lines)


def _extract_wallet_trades(events: Iterable[NFTEvent], *, wallet: str) -> list[_WalletTrade]:
    trades: list[_WalletTrade] = []
    resolved_wallet = _normalize_wallet(wallet)
    for event in events:
        if event.event_type != "sale":
            continue
        price = float(event.price or 0.0)
        if price <= 0:
            continue
        quantity = max(int(event.quantity or 1), 1)
        maker = _normalize_wallet(event.maker)
        taker = _normalize_wallet(event.taker)
        direction: str | None = None
        counterparty: str | None = None
        if taker == resolved_wallet and maker != resolved_wallet:
            direction = "buy"
            counterparty = maker or None
        elif maker == resolved_wallet and taker != resolved_wallet:
            direction = "sell"
            counterparty = taker or None
        if direction is None:
            continue
        trades.append(
            _WalletTrade(
                event_id=event.event_id,
                direction=direction,
                market=event.market,
                collection=event.collection,
                contract_address=(event.contract_address or None),
                token_id=str(event.token_id),
                quantity=quantity,
                currency=_currency_key(event.currency),
                total_price=price,
                unit_price=price / quantity,
                event_time=_ensure_utc(event.event_time),
                counterparty=counterparty,
                tx_hash=event.tx_hash,
            )
        )
    trades.sort(key=lambda item: (item.event_time, item.event_id))
    return trades


def _build_price_references(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[tuple[str, str], str], _PriceReference], dict[tuple[str, str], _PriceReference]]:
    asset_refs: dict[tuple[tuple[str, str], str], _PriceReference] = {}
    collection_refs: dict[tuple[str, str], _PriceReference] = {}
    for row in rows:
        currency = _currency_key(row.get("currency"))
        event_time = _maybe_iso(row.get("event_time"))
        unit_price = _coerce_positive_float(row.get("floor_price"))
        source = "floor"
        if unit_price is None:
            unit_price = _coerce_positive_float(row.get("price"))
            source = str(row.get("event_type") or "market")
        if unit_price is None:
            continue
        collection = str(row.get("collection_name") or "unknown")
        contract_address = str(row.get("contract_address") or "").strip().lower() or None
        token_id = str(row.get("token_id") or "")
        asset_key = _asset_key(contract_address, token_id, collection)
        collection_key = _collection_key(contract_address, collection)
        asset_refs.setdefault((asset_key, currency), _PriceReference(unit_price=unit_price, currency=currency, source=source, event_time=event_time))
        collection_refs.setdefault((collection_key, currency), _PriceReference(unit_price=unit_price, currency=currency, source=source, event_time=event_time))
    return asset_refs, collection_refs


def _build_collection_summaries(
    *,
    closed_positions: Iterable[ClosedPosition],
    open_positions: Iterable[OpenPosition],
    orphan_sales: Iterable[Mapping[str, Any]],
) -> list[CollectionPnlSummary]:
    stats: dict[str, dict[str, Any]] = {}

    def bucket_for(collection: str, contract_address: str | None) -> dict[str, Any]:
        key = _collection_key(contract_address, collection)
        bucket = stats.setdefault(
            key,
            {
                "collection": collection,
                "contract_address": contract_address,
                "currencies": set(),
                "markets": set(),
                "closed_position_count": 0,
                "open_position_count": 0,
                "priced_open_position_count": 0,
                "orphan_sale_count": 0,
                "closed_wins": 0,
                "closed_total": 0,
                "hold_hours": [],
                "realized_pnl_by_currency": defaultdict(float),
                "unrealized_pnl_by_currency": defaultdict(float),
                "capital_deployed_by_currency": defaultdict(float),
                "capital_released_by_currency": defaultdict(float),
                "inventory_cost_by_currency": defaultdict(float),
                "inventory_value_by_currency": defaultdict(float),
                "latest_trade_at": None,
                "notes": set(),
            },
        )
        if not bucket.get("contract_address") and contract_address:
            bucket["contract_address"] = contract_address
        return bucket

    for item in closed_positions:
        bucket = bucket_for(item.collection, item.contract_address)
        bucket["currencies"].add(item.currency)
        bucket["markets"].update((item.entry_market, item.exit_market))
        bucket["closed_position_count"] += 1
        bucket["closed_total"] += 1
        if item.realized_pnl > 0:
            bucket["closed_wins"] += 1
        bucket["hold_hours"].append(item.hold_hours)
        bucket["realized_pnl_by_currency"][item.currency] += item.realized_pnl
        bucket["capital_deployed_by_currency"][item.currency] += item.entry_total
        bucket["capital_released_by_currency"][item.currency] += item.exit_total
        latest = bucket.get("latest_trade_at")
        if latest is None or item.exit_time > str(latest):
            bucket["latest_trade_at"] = item.exit_time

    for item in open_positions:
        bucket = bucket_for(item.collection, item.contract_address)
        bucket["currencies"].add(item.currency)
        bucket["markets"].add(item.entry_market)
        bucket["open_position_count"] += 1
        bucket["inventory_cost_by_currency"][item.currency] += item.entry_total
        bucket["capital_deployed_by_currency"][item.currency] += item.entry_total
        if item.unrealized_pnl is not None:
            bucket["priced_open_position_count"] += 1
            bucket["unrealized_pnl_by_currency"][item.currency] += item.unrealized_pnl
            if item.reference_total is not None:
                bucket["inventory_value_by_currency"][item.currency] += item.reference_total
        else:
            bucket["notes"].add("unpriced_inventory")
        latest = bucket.get("latest_trade_at")
        if latest is None or item.entry_time > str(latest):
            bucket["latest_trade_at"] = item.entry_time

    for item in orphan_sales:
        bucket = bucket_for(str(item.get("collection") or "unknown"), item.get("contract_address"))
        currency = _currency_key(item.get("currency"))
        bucket["currencies"].add(currency)
        bucket["orphan_sale_count"] += int(item.get("quantity") or 0)
        bucket["capital_released_by_currency"][currency] += float(item.get("total_price") or 0.0)
        bucket["notes"].add("history_gap_or_untracked_inventory")
        latest = bucket.get("latest_trade_at")
        event_time = str(item.get("event_time") or "")
        if latest is None or event_time > str(latest):
            bucket["latest_trade_at"] = event_time

    snapshots: list[CollectionPnlSummary] = []
    for bucket in stats.values():
        closed_total = int(bucket["closed_total"])
        win_rate = (bucket["closed_wins"] / closed_total * 100.0) if closed_total else None
        hold_hours_values = [float(value) for value in bucket["hold_hours"] if value is not None]
        average_hold = sum(hold_hours_values) / len(hold_hours_values) if hold_hours_values else None
        snapshots.append(
            CollectionPnlSummary(
                collection=str(bucket["collection"]),
                contract_address=bucket.get("contract_address"),
                currencies=tuple(sorted(str(value) for value in bucket["currencies"] if value)),
                markets=tuple(sorted(str(value) for value in bucket["markets"] if value)),
                closed_position_count=int(bucket["closed_position_count"]),
                open_position_count=int(bucket["open_position_count"]),
                priced_open_position_count=int(bucket["priced_open_position_count"]),
                orphan_sale_count=int(bucket["orphan_sale_count"]),
                win_rate=win_rate,
                average_hold_hours=average_hold,
                realized_pnl_by_currency=dict(bucket["realized_pnl_by_currency"]),
                unrealized_pnl_by_currency=dict(bucket["unrealized_pnl_by_currency"]),
                capital_deployed_by_currency=dict(bucket["capital_deployed_by_currency"]),
                capital_released_by_currency=dict(bucket["capital_released_by_currency"]),
                inventory_cost_by_currency=dict(bucket["inventory_cost_by_currency"]),
                inventory_value_by_currency=dict(bucket["inventory_value_by_currency"]),
                latest_trade_at=bucket.get("latest_trade_at"),
                notes=tuple(sorted(str(note) for note in bucket["notes"] if note)),
            )
        )
    return snapshots


def _build_wallet_summary(
    *,
    wallet: str,
    trades: Iterable[_WalletTrade],
    closed_positions: Iterable[ClosedPosition],
    open_positions: Iterable[OpenPosition],
    orphan_sales: Iterable[Mapping[str, Any]],
) -> WalletPnlSummary:
    trade_list = list(trades)
    closed_list = list(closed_positions)
    open_list = list(open_positions)
    orphan_list = list(orphan_sales)
    realized: defaultdict[str, float] = defaultdict(float)
    unrealized: defaultdict[str, float] = defaultdict(float)
    deployed: defaultdict[str, float] = defaultdict(float)
    released: defaultdict[str, float] = defaultdict(float)
    inventory_cost: defaultdict[str, float] = defaultdict(float)
    inventory_value: defaultdict[str, float] = defaultdict(float)
    currencies: set[str] = set()

    for trade in trade_list:
        currencies.add(trade.currency)
        if trade.direction == "buy":
            deployed[trade.currency] += trade.total_price
        else:
            released[trade.currency] += trade.total_price

    for item in closed_list:
        currencies.add(item.currency)
        realized[item.currency] += item.realized_pnl

    for item in open_list:
        currencies.add(item.currency)
        inventory_cost[item.currency] += item.entry_total
        if item.reference_total is not None:
            inventory_value[item.currency] += item.reference_total
        if item.unrealized_pnl is not None:
            unrealized[item.currency] += item.unrealized_pnl

    for item in orphan_list:
        currency = _currency_key(item.get("currency"))
        currencies.add(currency)

    win_rate = None
    if closed_list:
        win_rate = sum(1 for item in closed_list if item.realized_pnl > 0) / len(closed_list) * 100.0
    hold_hours = [item.hold_hours for item in closed_list if item.hold_hours is not None]
    average_hold = sum(hold_hours) / len(hold_hours) if hold_hours else None
    notes: list[str] = []
    if orphan_list:
        notes.append("history_gap_or_untracked_inventory")
    if not trade_list:
        notes.append("no_wallet_trades_found")
    if any(item.reference_total is None for item in open_list):
        notes.append("unpriced_inventory")
    latest_trade_at = max((item.event_time.isoformat() for item in trade_list), default=None)
    return WalletPnlSummary(
        wallet=wallet,
        trade_count=len(trade_list),
        buy_count=sum(1 for item in trade_list if item.direction == "buy"),
        sell_count=sum(1 for item in trade_list if item.direction == "sell"),
        open_position_count=len(open_list),
        priced_open_position_count=sum(1 for item in open_list if item.reference_total is not None),
        orphan_sale_count=sum(int(item.get("quantity") or 0) for item in orphan_list),
        win_rate=win_rate,
        average_hold_hours=average_hold,
        currencies=tuple(sorted(currencies)),
        realized_pnl_by_currency=dict(realized),
        unrealized_pnl_by_currency=dict(unrealized),
        capital_deployed_by_currency=dict(deployed),
        capital_released_by_currency=dict(released),
        inventory_cost_by_currency=dict(inventory_cost),
        inventory_value_by_currency=dict(inventory_value),
        latest_trade_at=latest_trade_at,
        notes=tuple(dict.fromkeys(notes)),
    )


def _build_fill_match_map(execution_db_path: Path, *, limit: int) -> dict[str, dict[str, Any]]:
    if limit <= 0 or not execution_db_path.exists():
        return {}
    try:
        from okx_nft_bot.undercutter.state import PositionState

        state = PositionState(execution_db_path)
        rows = state.list_fill_matches(limit=limit)
    except Exception:
        return {}
    return {str(row.get("market_event_id")): row for row in rows if row.get("market_event_id")}


def _normalize_wallet(value: str | None) -> str:
    return str(value or "").strip().lower()


def _currency_key(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized or "UNKNOWN"


def _asset_key(contract_address: str | None, token_id: str | None, collection: str | None) -> tuple[str, str]:
    contract = str(contract_address or "").strip().lower()
    token = str(token_id or "").strip()
    if contract:
        return (contract, token)
    return (f"collection:{str(collection or 'unknown').strip().lower()}", token)


def _collection_key(contract_address: str | None, collection: str | None) -> str:
    contract = str(contract_address or "").strip().lower()
    if contract:
        return contract
    return str(collection or "unknown").strip().lower()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_positive_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number <= 0:
        return None
    return number


def _maybe_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _round_breakdown(values: Mapping[str, float]) -> dict[str, float]:
    return {str(currency): round(float(amount), 6) for currency, amount in sorted(values.items())}


def _format_breakdown(values: Mapping[str, float], *, signed: bool) -> str:
    if not values:
        return "n/a"
    parts: list[str] = []
    for currency, amount in sorted(values.items()):
        template = f"{float(amount):+.6f}" if signed else f"{float(amount):.6f}"
        parts.append(f"{currency}:{template}")
    return ", ".join(parts)
