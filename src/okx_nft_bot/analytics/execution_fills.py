from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
from typing import Any, Iterable

from okx_nft_bot.config import Settings
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_OFFER_ID_RE = re.compile(r"(?:^|[;\s,])offer_id=([^;\s,]+)", re.IGNORECASE)
_TOKEN_ID_RE = re.compile(r"(?:^|[;\s,])token_id=([^;\s,]+)", re.IGNORECASE)


@dataclass(slots=True)
class ExecutionFillMatch:
    market_event_id: str
    submit_log_id: int | None
    order_hash: str | None
    engine: str
    action_type: str
    collection: str
    contract_address: str | None
    token_id: str
    chain: str
    wallet: str
    currency: str
    submit_price: float
    fill_price: float
    slippage: float
    slippage_pct: float | None
    confidence: float
    confidence_label: str
    submit_created_at: str
    fill_event_time: str
    tx_hash: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_event_id": self.market_event_id,
            "submit_log_id": self.submit_log_id,
            "order_hash": self.order_hash,
            "engine": self.engine,
            "action_type": self.action_type,
            "collection": self.collection,
            "contract_address": self.contract_address,
            "token_id": self.token_id,
            "chain": self.chain,
            "wallet": self.wallet,
            "currency": self.currency,
            "submit_price": round(self.submit_price, 6),
            "fill_price": round(self.fill_price, 6),
            "slippage": round(self.slippage, 6),
            "slippage_pct": round(self.slippage_pct, 4) if self.slippage_pct is not None else None,
            "confidence": round(self.confidence, 4),
            "confidence_label": self.confidence_label,
            "submit_created_at": self.submit_created_at,
            "fill_event_time": self.fill_event_time,
            "tx_hash": self.tx_hash,
            "note": self.note,
        }


@dataclass(slots=True)
class ExecutionFillReport:
    generated_at: str
    wallet: str
    scanned_buy_event_count: int
    candidate_submit_count: int
    matched_fill_count: int
    unmatched_buy_event_count: int
    unmatched_submit_count: int
    matched_fill_volume_by_currency: dict[str, float]
    avg_confidence: float | None
    latest_fill_at: str | None
    matches: list[ExecutionFillMatch]
    unmatched_buy_event_ids: list[str]
    unmatched_submit_refs: list[str]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "scanned_buy_event_count": self.scanned_buy_event_count,
            "candidate_submit_count": self.candidate_submit_count,
            "matched_fill_count": self.matched_fill_count,
            "unmatched_buy_event_count": self.unmatched_buy_event_count,
            "unmatched_submit_count": self.unmatched_submit_count,
            "matched_fill_volume_by_currency": {
                key: round(value, 6) for key, value in sorted(self.matched_fill_volume_by_currency.items())
            },
            "avg_confidence": round(self.avg_confidence, 4) if self.avg_confidence is not None else None,
            "latest_fill_at": self.latest_fill_at,
            "matches": [item.to_dict() for item in self.matches],
            "unmatched_buy_event_ids": list(self.unmatched_buy_event_ids),
            "unmatched_submit_refs": list(self.unmatched_submit_refs),
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class _SubmitCandidate:
    submit_log_id: int
    order_hash: str | None
    engine: str
    action_type: str
    collection: str
    contract_address: str | None
    token_id: str | None
    chain: str
    currency: str
    submit_price: float
    created_at: datetime
    raw_reason: str | None = None


@dataclass(slots=True)
class _CandidateScore:
    candidate: _SubmitCandidate
    score: float
    price_diff: float
    price_diff_ratio: float
    time_delta_seconds: float
    note: str


class ExecutionFillReconciler:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
        price_tolerance_pct: float | None = None,
        pre_submit_slack_minutes: int | None = None,
        limit: int | None = None,
    ) -> ExecutionFillReport:
        resolved_wallet = _normalize_wallet(wallet or self.settings.buyer_wallet_address)
        now = datetime.now(timezone.utc)
        if not resolved_wallet:
            return ExecutionFillReport(
                generated_at=now.isoformat(),
                wallet="",
                scanned_buy_event_count=0,
                candidate_submit_count=0,
                matched_fill_count=0,
                unmatched_buy_event_count=0,
                unmatched_submit_count=0,
                matched_fill_volume_by_currency={},
                avg_confidence=None,
                latest_fill_at=None,
                matches=[],
                unmatched_buy_event_ids=[],
                unmatched_submit_refs=[],
                notes=("wallet_not_configured",),
            )

        buy_events = _extract_wallet_buys(
            self.store.fetch_wallet_event_models(wallet=resolved_wallet),
            wallet=resolved_wallet,
        )
        resolved_chain = (chain or self.settings.execution_chain).strip().lower()
        candidates = _build_submit_candidates(self.state, chain=resolved_chain)
        resolved_limit = reference_limit or self.settings.wallet_pnl_reference_event_limit
        if resolved_limit > 0:
            buy_events = buy_events[-int(resolved_limit):]
        resolved_window = timedelta(hours=max(int(window_hours or 72), 1))
        resolved_slack = timedelta(minutes=max(int(pre_submit_slack_minutes or 5), 0))
        resolved_tolerance = max(float(price_tolerance_pct or 0.03), 0.0)

        used_submit_ids: set[int] = set()
        matches: list[ExecutionFillMatch] = []
        unmatched_buy_event_ids: list[str] = []
        matched_fill_volume_by_currency: dict[str, float] = {}

        for event in buy_events:
            scored = [
                score
                for score in (
                    _score_candidate(
                        candidate,
                        event,
                        window=resolved_window,
                        pre_submit_slack=resolved_slack,
                        price_tolerance_pct=resolved_tolerance,
                    )
                    for candidate in candidates
                    if candidate.submit_log_id not in used_submit_ids
                )
                if score is not None
            ]
            scored.sort(
                key=lambda item: (
                    -item.score,
                    item.time_delta_seconds,
                    item.price_diff_ratio,
                    item.candidate.submit_log_id,
                )
            )
            if not scored:
                unmatched_buy_event_ids.append(event.event_id)
                continue

            best = scored[0]
            second = scored[1] if len(scored) > 1 else None
            ambiguous = False
            if second is not None:
                if abs(best.score - second.score) < 5.0 and (
                    best.candidate.token_id is None or second.candidate.token_id is None
                ):
                    ambiguous = True
                if (
                    best.candidate.token_id is None
                    and second.candidate.token_id is None
                    and abs(best.time_delta_seconds - second.time_delta_seconds) < 60.0
                    and abs(best.price_diff_ratio - second.price_diff_ratio) < 0.01
                ):
                    ambiguous = True
            if ambiguous or best.score < 40.0:
                unmatched_buy_event_ids.append(event.event_id)
                continue

            candidate = best.candidate
            used_submit_ids.add(candidate.submit_log_id)
            slippage = float(event.price or 0.0) - candidate.submit_price
            slippage_pct = (slippage / candidate.submit_price * 100.0) if candidate.submit_price > 0 else None
            confidence = min(max(best.score / 180.0, 0.0), 1.0)
            if confidence >= 0.9:
                confidence_label = "high"
            elif confidence >= 0.65:
                confidence_label = "medium"
            else:
                confidence_label = "low"
            note = best.note
            match = ExecutionFillMatch(
                market_event_id=event.event_id,
                submit_log_id=candidate.submit_log_id,
                order_hash=candidate.order_hash,
                engine=candidate.engine,
                action_type=candidate.action_type,
                collection=event.collection,
                contract_address=event.contract_address,
                token_id=str(event.token_id),
                chain=candidate.chain,
                wallet=resolved_wallet,
                currency=_currency_key(event.currency),
                submit_price=candidate.submit_price,
                fill_price=float(event.price or 0.0),
                slippage=slippage,
                slippage_pct=slippage_pct,
                confidence=confidence,
                confidence_label=confidence_label,
                submit_created_at=candidate.created_at.isoformat(),
                fill_event_time=_ensure_utc(event.event_time).isoformat(),
                tx_hash=event.tx_hash,
                note=note,
            )
            matches.append(match)
            matched_fill_volume_by_currency[match.currency] = matched_fill_volume_by_currency.get(match.currency, 0.0) + match.fill_price

        matches.sort(key=lambda item: (item.fill_event_time, item.market_event_id), reverse=True)
        if limit is not None:
            matches = matches[: max(int(limit), 0)]

        avg_confidence = (
            sum(item.confidence for item in matches) / len(matches)
            if matches
            else None
        )
        latest_fill_at = matches[0].fill_event_time if matches else None
        unmatched_submit_refs = [
            _submit_ref(candidate)
            for candidate in candidates
            if candidate.submit_log_id not in used_submit_ids
        ]

        notes: list[str] = []
        if candidates and not matches:
            notes.append("no_execution_fills_matched")
        if unmatched_submit_refs:
            notes.append("unmatched_execution_submits_present")

        return ExecutionFillReport(
            generated_at=now.isoformat(),
            wallet=resolved_wallet,
            scanned_buy_event_count=len(buy_events),
            candidate_submit_count=len(candidates),
            matched_fill_count=len(matches),
            unmatched_buy_event_count=len(unmatched_buy_event_ids),
            unmatched_submit_count=len(unmatched_submit_refs),
            matched_fill_volume_by_currency=matched_fill_volume_by_currency,
            avg_confidence=avg_confidence,
            latest_fill_at=latest_fill_at,
            matches=matches,
            unmatched_buy_event_ids=unmatched_buy_event_ids,
            unmatched_submit_refs=unmatched_submit_refs,
            notes=tuple(dict.fromkeys(notes)),
        )

    def reconcile(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
        price_tolerance_pct: float | None = None,
        pre_submit_slack_minutes: int | None = None,
        limit: int | None = None,
    ) -> ExecutionFillReport:
        report = self.build_report(
            wallet=wallet,
            reference_limit=reference_limit,
            chain=chain,
            window_hours=window_hours,
            price_tolerance_pct=price_tolerance_pct,
            pre_submit_slack_minutes=pre_submit_slack_minutes,
            limit=limit,
        )
        for item in report.matches:
            self.state.record_fill_match(
                market_event_id=item.market_event_id,
                submit_log_id=item.submit_log_id,
                order_hash=item.order_hash,
                engine=item.engine,
                action_type=item.action_type,
                collection=item.collection,
                contract_address=item.contract_address,
                token_id=item.token_id,
                chain=item.chain,
                wallet=item.wallet,
                currency=item.currency,
                submit_price=item.submit_price,
                fill_price=item.fill_price,
                confidence=item.confidence,
                confidence_label=item.confidence_label,
                submit_created_at=item.submit_created_at,
                fill_event_time=item.fill_event_time,
                tx_hash=item.tx_hash,
                note=item.note,
            )
        latest_fill_at = report.latest_fill_at
        self.state.set_runtime_value("last_fill_reconcile_at", report.generated_at)
        self.state.set_runtime_value("last_fill_reconcile_scanned_buys", report.scanned_buy_event_count)
        self.state.set_runtime_value("last_fill_reconcile_matches", report.matched_fill_count)
        self.state.set_runtime_value("last_fill_reconcile_unmatched_submits", report.unmatched_submit_count)
        self.state.set_runtime_value("last_confirmed_fill_at", latest_fill_at)
        return report

    def write_report(
        self,
        *,
        wallet: str | None = None,
        report_path: Path | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
        price_tolerance_pct: float | None = None,
        pre_submit_slack_minutes: int | None = None,
    ) -> str:
        report = self.reconcile(
            wallet=wallet,
            reference_limit=reference_limit,
            chain=chain,
            window_hours=window_hours,
            price_tolerance_pct=price_tolerance_pct,
            pre_submit_slack_minutes=pre_submit_slack_minutes,
        )
        target = report_path or self.settings.execution_fill_report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)


def format_execution_fill_text(report: ExecutionFillReport, *, limit: int = 5) -> str:
    lines = ["execution_fills"]
    lines.append(f"wallet={report.wallet or 'not_configured'}")
    lines.append(
        f"buys_scanned={report.scanned_buy_event_count} submits={report.candidate_submit_count} matched={report.matched_fill_count} unmatched_buys={report.unmatched_buy_event_count} unmatched_submits={report.unmatched_submit_count}"
    )
    lines.append(
        "matched_volume=" + _format_breakdown(report.matched_fill_volume_by_currency)
    )
    if report.avg_confidence is not None:
        lines.append(f"avg_confidence={report.avg_confidence:.2f}")
    if report.latest_fill_at:
        lines.append(f"latest_fill_at={report.latest_fill_at}")
    if report.notes:
        lines.append("notes=" + ",".join(report.notes))
    if report.matches:
        lines.append("matches:")
        for item in report.matches[: max(int(limit), 0)]:
            lines.append(
                f"- {item.collection} #{item.token_id} | {item.engine}:{item.action_type} | submit={item.submit_price:.6f} {item.currency} | fill={item.fill_price:.6f} {item.currency} | conf={item.confidence_label}:{item.confidence:.2f}"
            )
    return "\n".join(lines)


def _extract_wallet_buys(events: Iterable[NFTEvent], *, wallet: str) -> list[NFTEvent]:
    resolved_wallet = _normalize_wallet(wallet)
    buys: list[NFTEvent] = []
    for event in events:
        if event.event_type != "sale":
            continue
        if _normalize_wallet(event.taker) != resolved_wallet:
            continue
        if _normalize_wallet(event.maker) == resolved_wallet:
            continue
        if float(event.price or 0.0) <= 0:
            continue
        buys.append(event)
    buys.sort(key=lambda item: (_ensure_utc(item.event_time), item.event_id))
    return buys


def _build_submit_candidates(state: PositionState, *, chain: str) -> list[_SubmitCandidate]:
    offer_snapshots = {item["order_hash"]: item for item in state.list_offer_snapshots(chain=chain)}
    candidates: list[_SubmitCandidate] = []
    for row in state.list_submit_events(chain=chain):
        if str(row.get("status") or "").strip().lower() != "submitted":
            continue
        order_hash = _parse_reason_value(str(row.get("reason") or ""), _OFFER_ID_RE)
        token_id = _parse_reason_value(str(row.get("reason") or ""), _TOKEN_ID_RE)
        snapshot = offer_snapshots.get(order_hash) if order_hash else None
        preview_payload = snapshot.get("preview_payload") if isinstance(snapshot, dict) else None
        candidate_token_id = _normalize_token_id(token_id or _preview_value(preview_payload, "token_id"))
        collection = str((snapshot or {}).get("collection") or row.get("collection") or "").strip().lower()
        contract_address = collection if collection.startswith("0x") else _normalize_contract_address(_preview_value(preview_payload, "collection"))
        price = _coerce_positive_float(row.get("price_bnb"))
        if price is None and snapshot is not None:
            price = _coerce_positive_float(snapshot.get("price_bnb"))
        if not collection or price is None:
            continue
        created_at = _parse_iso(row.get("created_at"))
        if created_at is None:
            continue
        candidates.append(
            _SubmitCandidate(
                submit_log_id=int(row["id"]),
                order_hash=order_hash,
                engine=str(row.get("engine") or "unknown"),
                action_type=str(row.get("action_type") or "unknown"),
                collection=collection,
                contract_address=contract_address,
                token_id=candidate_token_id,
                chain=str(row.get("chain") or chain).strip().lower() or chain,
                currency=_native_currency_for_chain(str(row.get("chain") or chain)),
                submit_price=price,
                created_at=created_at,
                raw_reason=row.get("reason"),
            )
        )
    candidates.sort(key=lambda item: (item.created_at, item.submit_log_id))
    return candidates


def _score_candidate(
    candidate: _SubmitCandidate,
    event: NFTEvent,
    *,
    window: timedelta,
    pre_submit_slack: timedelta,
    price_tolerance_pct: float,
) -> _CandidateScore | None:
    event_time = _ensure_utc(event.event_time)
    if event_time < candidate.created_at - pre_submit_slack:
        return None
    if event_time > candidate.created_at + window:
        return None
    event_contract = _normalize_contract_address(event.contract_address)
    candidate_contract = _normalize_contract_address(candidate.contract_address)
    event_collection = str(event.collection or "").strip().lower()
    collection_match = False
    score = 0.0
    notes: list[str] = []
    if candidate_contract and event_contract and candidate_contract == event_contract:
        collection_match = True
        score += 45.0
        notes.append("contract_exact")
    elif candidate.collection == event_collection:
        collection_match = True
        score += 20.0
        notes.append("collection_exact")
    elif candidate_contract and candidate_contract == event_collection:
        collection_match = True
        score += 20.0
        notes.append("collection_alias")
    if not collection_match:
        return None

    event_token = _normalize_token_id(event.token_id)
    if candidate.token_id is not None:
        if event_token != candidate.token_id:
            return None
        score += 120.0
        notes.append("token_exact")
    else:
        score += 15.0
        notes.append("collection_level")

    event_currency = _currency_key(event.currency)
    if event_currency != candidate.currency:
        return None
    score += 10.0
    notes.append("currency_exact")

    fill_price = float(event.price or 0.0)
    if fill_price <= 0:
        return None
    price_diff = abs(fill_price - candidate.submit_price)
    tolerance_abs = max(candidate.submit_price * price_tolerance_pct, 0.000001)
    if price_diff > tolerance_abs:
        return None
    price_diff_ratio = price_diff / candidate.submit_price if candidate.submit_price > 0 else 0.0
    score += max(0.0, 25.0 - price_diff_ratio * 500.0)
    if price_diff_ratio <= 0.005:
        notes.append("price_near_exact")
    elif price_diff_ratio <= 0.02:
        notes.append("price_close")

    time_delta_seconds = abs((event_time - candidate.created_at).total_seconds())
    score += max(0.0, 20.0 - min(time_delta_seconds / 300.0, 20.0))
    if time_delta_seconds <= 900:
        notes.append("time_near")
    elif time_delta_seconds <= 3600:
        notes.append("time_close")

    return _CandidateScore(
        candidate=candidate,
        score=score,
        price_diff=price_diff,
        price_diff_ratio=price_diff_ratio,
        time_delta_seconds=time_delta_seconds,
        note=",".join(notes),
    )


def _submit_ref(candidate: _SubmitCandidate) -> str:
    if candidate.order_hash:
        return candidate.order_hash
    return f"submit:{candidate.submit_log_id}"


def _parse_reason_value(value: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(value)
    if not match:
        return None
    parsed = match.group(1).strip()
    return parsed or None


def _preview_value(preview_payload: Any, key: str) -> Any:
    if not isinstance(preview_payload, dict):
        return None
    return preview_payload.get(key)


def _normalize_wallet(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_contract_address(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _normalize_token_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    token = str(value).strip()
    if not token:
        return None
    if token.lower() in {"col", "collection", "none"}:
        return None
    return token


def _currency_key(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {
        "WBNB": "BNB",
        "BNB": "BNB",
        "WETH": "ETH",
        "ETH": "ETH",
        "WMATIC": "MATIC",
        "MATIC": "MATIC",
        "SOL": "SOL",
    }
    return aliases.get(normalized, normalized or "UNKNOWN")


def _native_currency_for_chain(value: str | None) -> str:
    chain = str(value or "").strip().lower()
    mapping = {
        "bsc": "BNB",
        "bnb": "BNB",
        "eth": "ETH",
        "ethereum": "ETH",
        "matic": "MATIC",
        "polygon": "MATIC",
        "sol": "SOL",
        "solana": "SOL",
    }
    return mapping.get(chain, _currency_key(chain))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_positive_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number <= 0:
        return None
    return number


def _format_breakdown(payload: dict[str, float]) -> str:
    if not payload:
        return "none"
    return ", ".join(f"{key}:{value:.6f}" for key, value in sorted(payload.items()))
