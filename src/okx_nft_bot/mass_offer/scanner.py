from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from okx_nft_bot.normalizers.offers import NormalizedOffer


@dataclass(slots=True)
class MassOfferFilters:
    rarity_filter: tuple[str, ...] = ()
    unlisted_only: bool = False
    exclude_own: bool = True
    max_existing_offer: float | None = None
    min_token_id: int | None = None
    max_token_id: int | None = None


@dataclass(slots=True)
class MassOfferCandidate:
    token_id: int
    owner: str | None
    rarity: str | None
    listed: bool
    existing_offer_bnb: float | None
    raw: dict[str, Any]


@dataclass(slots=True)
class MassOfferSkip:
    token_id: int | None
    reason: str
    rarity: str | None = None
    owner: str | None = None
    listed: bool | None = None
    existing_offer_bnb: float | None = None


@dataclass(slots=True)
class CollectionScanResult:
    scanned_count: int
    target_count: int
    targets: list[MassOfferCandidate]
    skipped: list[MassOfferSkip]


def fetch_collection_nfts(
    client: Any,
    *,
    chain: str,
    collection: str,
    limit: int,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        payload = client.get_nft_list(
            chain=chain,
            contract_address=collection,
            limit=limit,
            cursor=cursor,
        )
        assets.extend(_extract_records(payload))
        pages += 1
        cursor = _extract_cursor(payload)
        if not cursor:
            break
        if max_pages is not None and pages >= max_pages:
            break
    return assets


def build_existing_offer_map(offers: Iterable[NormalizedOffer]) -> dict[str, float]:
    result: dict[str, float] = {}
    for offer in offers:
        if not offer.token_id or offer.price is None:
            continue
        current = result.get(str(offer.token_id))
        if current is None or float(offer.price) > current:
            result[str(offer.token_id)] = float(offer.price)
    return result


def select_mass_offer_targets(
    assets: Iterable[dict[str, Any]],
    *,
    filters: MassOfferFilters,
    own_wallet: str | None = None,
    existing_offer_prices: dict[str, float] | None = None,
) -> CollectionScanResult:
    rarity_filter = {value.upper() for value in filters.rarity_filter if value}
    buyer = own_wallet.lower() if own_wallet else None
    highest_offers = existing_offer_prices or {}
    targets: list[MassOfferCandidate] = []
    skipped: list[MassOfferSkip] = []
    scanned_count = 0

    for asset in assets:
        scanned_count += 1
        token_id = _extract_token_id(asset)
        owner = _extract_owner(asset)
        rarity = _extract_rarity(asset)
        listed = _is_listed(asset)
        existing_offer_bnb = highest_offers.get(str(token_id)) if token_id is not None else None

        if token_id is None:
            skipped.append(MassOfferSkip(token_id=None, reason="missing_token_id", rarity=rarity, owner=owner, listed=listed))
            continue
        if filters.min_token_id is not None and token_id < filters.min_token_id:
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="below_min_token_id",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue
        if filters.max_token_id is not None and token_id > filters.max_token_id:
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="above_max_token_id",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue
        if rarity_filter and (rarity is None or rarity.upper() not in rarity_filter):
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="rarity_mismatch",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue
        if filters.unlisted_only and listed:
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="listed",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue
        if filters.exclude_own and buyer and owner and owner.lower() == buyer:
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="owned_by_buyer",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue
        if (
            filters.max_existing_offer is not None
            and existing_offer_bnb is not None
            and existing_offer_bnb > filters.max_existing_offer
        ):
            skipped.append(
                MassOfferSkip(
                    token_id=token_id,
                    reason="existing_offer_above_max",
                    rarity=rarity,
                    owner=owner,
                    listed=listed,
                    existing_offer_bnb=existing_offer_bnb,
                )
            )
            continue

        targets.append(
            MassOfferCandidate(
                token_id=token_id,
                owner=owner,
                rarity=rarity,
                listed=listed,
                existing_offer_bnb=existing_offer_bnb,
                raw=dict(asset),
            )
        )

    targets.sort(key=lambda item: item.token_id)
    return CollectionScanResult(
        scanned_count=scanned_count,
        target_count=len(targets),
        targets=targets,
        skipped=skipped,
    )


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "assets", "items", "records", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("cursor", "nextCursor", "next"):
            value = data.get(key)
            if value not in {None, "", "0"}:
                return str(value)
    for key in ("cursor", "nextCursor", "next"):
        value = payload.get(key)
        if value not in {None, "", "0"}:
            return str(value)
    return None


def _extract_token_id(asset: dict[str, Any]) -> int | None:
    for key in ("tokenId", "token_id", "inscriptionNumber", "identifier"):
        if key not in asset:
            continue
        value = _to_int(asset.get(key))
        if value is not None:
            return value
    return None


def _extract_owner(asset: dict[str, Any]) -> str | None:
    for key in ("ownerAddress", "owner", "holderAddress", "ownerAddr", "ownerWalletAddress"):
        value = asset.get(key)
        if isinstance(value, dict):
            for nested_key in ("address", "ownerAddress", "walletAddress"):
                nested = value.get(nested_key)
                if nested:
                    return str(nested)
        if value:
            return str(value)
    return None


def _extract_rarity(asset: dict[str, Any]) -> str | None:
    for key in ("rarity", "rarityName", "rarityTier", "tier"):
        value = _normalize_rarity(asset.get(key))
        if value:
            return value

    for key in ("traits", "attributes", "properties"):
        value = asset.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            trait_type = str(
                item.get("traitType")
                or item.get("trait_type")
                or item.get("name")
                or item.get("key")
                or ""
            ).strip().lower()
            if trait_type not in {"rarity", "tier"}:
                continue
            rarity = _normalize_rarity(item.get("value"))
            if rarity:
                return rarity
    return None


def _is_listed(asset: dict[str, Any]) -> bool:
    for key in ("listed", "isListed", "is_listed"):
        value = asset.get(key)
        if value is not None:
            return _to_bool(value)

    for key in ("listingStatus", "saleStatus", "status", "state", "sellStatus"):
        value = asset.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        if any(marker in text for marker in ("не выставлено", "unlisted", "not listed", "not_for_sale", "off_market", "inactive", "sold")):
            return False
        if any(marker in text for marker in ("listed", "for sale", "on sale", "active listing", "listing_active", "onsale")):
            return True

    for key in ("listingPrice", "listPrice", "sellPrice", "bestListingPrice"):
        value = _to_float(asset.get(key))
        if value is not None and value > 0:
            return True

    for key in ("listings", "orders"):
        value = asset.get(key)
        if isinstance(value, list) and value:
            return True

    return False


def _normalize_rarity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.upper()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on", "listed", "active"}


def _to_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None
