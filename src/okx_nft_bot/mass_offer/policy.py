from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(slots=True)
class CollectionMassOfferPolicy:
    collection: str
    chain: str = "bsc"
    enabled: bool = True
    dry_run_only: bool = False
    max_total_cap: int | None = None
    min_delay_seconds: float | None = None
    max_existing_offer_cap: float | None = None
    max_active_offers: int | None = None
    max_active_exposure_bnb: float | None = None
    preferred_max_total: int | None = None
    preferred_delay_seconds: float | None = None
    expires_at: str | None = None
    notes: tuple[str, ...] = ()
    source: str = "file"

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "chain": self.chain,
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "max_total_cap": self.max_total_cap,
            "min_delay_seconds": self.min_delay_seconds,
            "max_existing_offer_cap": self.max_existing_offer_cap,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": self.max_active_exposure_bnb,
            "preferred_max_total": self.preferred_max_total,
            "preferred_delay_seconds": self.preferred_delay_seconds,
            "expires_at": self.expires_at,
            "notes": list(self.notes),
            "source": self.source,
        }


@dataclass(slots=True)
class AppliedMassOfferPolicy:
    collection: str
    chain: str
    source: str
    policy_found: bool
    enabled: bool
    effective_dry_run: bool
    max_total: int
    delay_seconds: float
    max_existing_offer: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    preferred_max_total: int | None = None
    preferred_delay_seconds: float | None = None
    expires_at: str | None = None
    notes: tuple[str, ...] = ()
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "chain": self.chain,
            "source": self.source,
            "policy_found": self.policy_found,
            "enabled": self.enabled,
            "effective_dry_run": self.effective_dry_run,
            "max_total": self.max_total,
            "delay_seconds": self.delay_seconds,
            "max_existing_offer": self.max_existing_offer,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": self.max_active_exposure_bnb,
            "preferred_max_total": self.preferred_max_total,
            "preferred_delay_seconds": self.preferred_delay_seconds,
            "expires_at": self.expires_at,
            "notes": list(self.notes),
            "blocked_reason": self.blocked_reason,
        }


class MassOfferPolicyRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[tuple[str, str], CollectionMassOfferPolicy] | None = None

    def load(self) -> dict[tuple[str, str], CollectionMassOfferPolicy]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        raw_map = payload.get("collections") if isinstance(payload, dict) and isinstance(payload.get("collections"), dict) else payload
        result: dict[tuple[str, str], CollectionMassOfferPolicy] = {}
        now = datetime.now(timezone.utc)
        if not isinstance(raw_map, dict):
            self._cache = result
            return self._cache
        for raw_collection, raw_policy in raw_map.items():
            if not isinstance(raw_collection, str) or not isinstance(raw_policy, dict):
                continue
            collection = raw_collection.strip().lower()
            if not collection:
                continue
            chain = str(raw_policy.get("chain") or "bsc").strip().lower()
            expires_at = _coerce_iso_datetime(raw_policy.get("expires_at"))
            if expires_at is not None:
                expires_dt = datetime.fromisoformat(expires_at)
                if expires_dt <= now:
                    continue
            result[(chain, collection)] = CollectionMassOfferPolicy(
                collection=collection,
                chain=chain,
                enabled=bool(raw_policy.get("enabled", True)),
                dry_run_only=bool(raw_policy.get("dry_run_only", False)),
                max_total_cap=_coerce_int(raw_policy.get("max_total_cap")),
                min_delay_seconds=_coerce_float(raw_policy.get("min_delay_seconds")),
                max_existing_offer_cap=_coerce_float(raw_policy.get("max_existing_offer_cap")),
                max_active_offers=_coerce_int(raw_policy.get("max_active_offers")),
                max_active_exposure_bnb=_coerce_positive_float(raw_policy.get("max_active_exposure_bnb")),
                preferred_max_total=_coerce_int(raw_policy.get("preferred_max_total")),
                preferred_delay_seconds=_coerce_positive_float(raw_policy.get("preferred_delay_seconds")),
                expires_at=expires_at,
                notes=tuple(str(item) for item in raw_policy.get("notes", []) if str(item).strip()),
                source=str(raw_policy.get("source") or self.path.name),
            )
        self._cache = result
        return self._cache

    def count(self) -> int:
        return len(self.load())

    def get(self, *, collection: str, chain: str) -> CollectionMassOfferPolicy | None:
        collection_key = collection.strip().lower()
        chain_key = chain.strip().lower()
        mapping = self.load()
        return mapping.get((chain_key, collection_key)) or mapping.get(("*", collection_key))

    def clear_cache(self) -> None:
        self._cache = None


def merge_mass_offer_policies(
    *,
    collection: str,
    chain: str,
    policies: Iterable[CollectionMassOfferPolicy | None],
) -> CollectionMassOfferPolicy | None:
    active = [policy for policy in policies if policy is not None]
    if not active:
        return None
    collection_key = collection.strip().lower()
    chain_key = chain.strip().lower()
    enabled = True
    dry_run_only = False
    max_total_cap: int | None = None
    min_delay_seconds: float | None = None
    max_existing_offer_cap: float | None = None
    max_active_offers: int | None = None
    max_active_exposure_bnb: float | None = None
    preferred_max_total: int | None = None
    preferred_delay_seconds: float | None = None
    expires_at: str | None = None
    notes: list[str] = []
    sources: list[str] = []

    for policy in active:
        enabled = enabled and policy.enabled
        dry_run_only = dry_run_only or policy.dry_run_only
        if policy.max_total_cap is not None and policy.max_total_cap > 0:
            max_total_cap = policy.max_total_cap if max_total_cap is None else min(max_total_cap, policy.max_total_cap)
        if policy.min_delay_seconds is not None and policy.min_delay_seconds >= 0:
            min_delay_seconds = policy.min_delay_seconds if min_delay_seconds is None else max(min_delay_seconds, policy.min_delay_seconds)
        if policy.max_existing_offer_cap is not None and policy.max_existing_offer_cap > 0:
            max_existing_offer_cap = (
                policy.max_existing_offer_cap
                if max_existing_offer_cap is None
                else min(max_existing_offer_cap, policy.max_existing_offer_cap)
            )
        if policy.max_active_offers is not None and policy.max_active_offers > 0:
            max_active_offers = policy.max_active_offers if max_active_offers is None else min(max_active_offers, policy.max_active_offers)
        if policy.max_active_exposure_bnb is not None and policy.max_active_exposure_bnb > 0:
            max_active_exposure_bnb = (
                policy.max_active_exposure_bnb
                if max_active_exposure_bnb is None
                else min(max_active_exposure_bnb, policy.max_active_exposure_bnb)
            )
        if policy.preferred_max_total is not None and policy.preferred_max_total > 0:
            preferred_max_total = policy.preferred_max_total
        if policy.preferred_delay_seconds is not None and policy.preferred_delay_seconds > 0:
            preferred_delay_seconds = policy.preferred_delay_seconds
        if policy.expires_at:
            expires_at = policy.expires_at if expires_at is None else max(expires_at, policy.expires_at)
        notes.extend(policy.notes)
        if policy.source:
            sources.append(policy.source)

    deduped_notes = tuple(dict.fromkeys(note for note in notes if note))
    source = "+".join(dict.fromkeys(sources)) if sources else "merged"
    return CollectionMassOfferPolicy(
        collection=collection_key,
        chain=chain_key,
        enabled=enabled,
        dry_run_only=dry_run_only,
        max_total_cap=max_total_cap,
        min_delay_seconds=min_delay_seconds,
        max_existing_offer_cap=max_existing_offer_cap,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        preferred_max_total=preferred_max_total,
        preferred_delay_seconds=preferred_delay_seconds,
        expires_at=expires_at,
        notes=deduped_notes,
        source=source,
    )


def apply_mass_offer_policy(
    *,
    collection: str,
    chain: str,
    policy: CollectionMassOfferPolicy | None,
    requested_dry_run: bool,
    requested_max_total: int,
    requested_delay_seconds: float,
    requested_max_existing_offer: float | None,
    allow_preferred_total: bool = False,
    allow_preferred_delay: bool = False,
) -> AppliedMassOfferPolicy:
    collection_key = collection.strip().lower()
    chain_key = chain.strip().lower()
    max_total = max(int(requested_max_total), 1)
    delay_seconds = max(float(requested_delay_seconds), 0.0)
    max_existing_offer = requested_max_existing_offer
    effective_dry_run = bool(requested_dry_run)
    notes: list[str] = []
    blocked_reason: str | None = None
    max_active_offers: int | None = None
    max_active_exposure_bnb: float | None = None
    preferred_max_total: int | None = None
    preferred_delay_seconds: float | None = None
    expires_at: str | None = None
    source = "defaults"
    enabled = True
    policy_found = policy is not None
    if policy is not None:
        source = policy.source
        enabled = policy.enabled
        notes.extend(policy.notes)
        preferred_max_total = policy.preferred_max_total
        preferred_delay_seconds = policy.preferred_delay_seconds
        expires_at = policy.expires_at
        if not policy.enabled:
            blocked_reason = "policy_disabled"
        if policy.dry_run_only:
            effective_dry_run = True
            notes.append("policy_forces_dry_run")
        if allow_preferred_total and policy.preferred_max_total is not None and policy.preferred_max_total > 0:
            if max_total != policy.preferred_max_total:
                notes.append(f"preferred_max_total:{max_total}->{policy.preferred_max_total}")
            max_total = max(int(policy.preferred_max_total), 1)
        if allow_preferred_delay and policy.preferred_delay_seconds is not None and policy.preferred_delay_seconds > 0:
            if abs(delay_seconds - float(policy.preferred_delay_seconds)) > 1e-12:
                notes.append(f"preferred_delay:{delay_seconds}->{policy.preferred_delay_seconds}")
            delay_seconds = max(float(policy.preferred_delay_seconds), 0.0)
        if policy.max_total_cap is not None and policy.max_total_cap > 0:
            if max_total > policy.max_total_cap:
                notes.append(f"max_total_clamped:{max_total}->{policy.max_total_cap}")
            max_total = min(max_total, policy.max_total_cap)
        if policy.min_delay_seconds is not None and policy.min_delay_seconds >= 0:
            if delay_seconds < policy.min_delay_seconds:
                notes.append(f"delay_raised:{delay_seconds}->{policy.min_delay_seconds}")
            delay_seconds = max(delay_seconds, policy.min_delay_seconds)
        if policy.max_existing_offer_cap is not None:
            if max_existing_offer is None or max_existing_offer > policy.max_existing_offer_cap:
                notes.append(
                    f"max_existing_offer_clamped:{max_existing_offer}->{policy.max_existing_offer_cap}"
                )
                max_existing_offer = policy.max_existing_offer_cap
        if policy.max_active_offers is not None and policy.max_active_offers > 0:
            max_active_offers = policy.max_active_offers
        if policy.max_active_exposure_bnb is not None and policy.max_active_exposure_bnb > 0:
            max_active_exposure_bnb = policy.max_active_exposure_bnb
    return AppliedMassOfferPolicy(
        collection=collection_key,
        chain=chain_key,
        source=source,
        policy_found=policy_found,
        enabled=enabled,
        effective_dry_run=effective_dry_run,
        max_total=max_total,
        delay_seconds=delay_seconds,
        max_existing_offer=max_existing_offer,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        preferred_max_total=preferred_max_total,
        preferred_delay_seconds=preferred_delay_seconds,
        expires_at=expires_at,
        notes=tuple(dict.fromkeys(notes)),
        blocked_reason=blocked_reason,
    )


def format_mass_offer_policy_preview(payload: dict[str, Any]) -> str:
    lines = [
        "mass_offer_policy",
        f"collection={payload.get('collection')}",
        f"chain={payload.get('chain')}",
        f"policy_found={payload.get('policy_found')}",
        f"effective_dry_run={payload.get('effective_dry_run')}",
        f"max_total={payload.get('max_total')}",
        f"delay_seconds={payload.get('delay_seconds')}",
        f"max_existing_offer={payload.get('max_existing_offer')}",
        f"max_active_offers={payload.get('max_active_offers')}",
        f"max_active_exposure_bnb={payload.get('max_active_exposure_bnb')}",
        f"preferred_max_total={payload.get('preferred_max_total')}",
        f"preferred_delay_seconds={payload.get('preferred_delay_seconds')}",
        f"expires_at={payload.get('expires_at')}",
        f"active_offer_count={payload.get('active_offer_count')}",
        f"active_exposure_bnb={payload.get('active_exposure_bnb')}",
    ]
    projected_exposure = payload.get("projected_active_exposure_bnb")
    if projected_exposure is not None:
        lines.append(f"projected_active_exposure_bnb={projected_exposure}")
    if payload.get("blocked_reason"):
        lines.append(f"blocked_reason={payload['blocked_reason']}")
    notes = payload.get("notes") or []
    for note in notes[:6]:
        lines.append(f"- {note}")
    return "\n".join(lines)


def format_mass_offer_capital_text(payload: dict[str, Any]) -> str:
    lines = [
        "mass_offer_capital",
        f"chain={payload.get('chain')}",
        f"active_offers={payload.get('active_offer_count')}",
        f"active_exposure_bnb={payload.get('active_exposure_bnb')}",
        f"collections={payload.get('collection_count')}",
        f"hourly_submits={payload.get('hourly_submit_count')}/{payload.get('hourly_limit')}",
        f"daily_submit_bnb={payload.get('daily_submit_bnb')}/{payload.get('daily_limit_bnb')}",
        f"policy_entries={payload.get('policy_entries')}",
        f"collections_with_exposure_caps={payload.get('collections_with_exposure_caps')}",
        f"at_or_over_exposure_caps={payload.get('collections_at_or_over_exposure_caps')}",
    ]
    for item in payload.get("top_collections", [])[:5]:
        cap = item.get("cap_bnb")
        headroom = item.get("headroom_bnb")
        if cap is None:
            cap_text = "cap=none"
        else:
            cap_text = f"cap={cap:.6f}"
        if headroom is None:
            headroom_text = "headroom=n/a"
        else:
            headroom_text = f"headroom={headroom:.6f}"
        lines.append(
            f"- {item['collection']} | exposure={item['exposure_bnb']:.6f} | offers={item['active_offer_count']} | {cap_text} | {headroom_text}"
        )
    return "\n".join(lines)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)



def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)



def _coerce_positive_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    resolved = float(value)
    return resolved if resolved > 0 else None


def _coerce_iso_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()
