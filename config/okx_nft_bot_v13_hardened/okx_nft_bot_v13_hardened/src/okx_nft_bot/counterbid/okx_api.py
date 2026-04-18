from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("counterbid.okx_api")

_MAX_429_RETRIES = 3
_429_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8

from okx_nft_bot.clients.http import HTTPStatusError, build_url
from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.providers.offers_okx import OKXOffersProvider
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore


_LOG_REDACTION = "***REDACTED***"
_LOG_SENSITIVE_KEYS = frozenset({
    "authorization",
    "buyer_wallet_private_key",
    "ok_access_sign",
    "okx_api_key",
    "okx_api_secret",
    "private_key",
    "protocoldata",
    "r",
    "s",
    "secret",
    "seed",
    "signature",
    "token",
})


def _looks_like_hex_signature(value: str) -> bool:
    text = value.strip()
    if not text.startswith("0x"):
        return False
    if len(text) < 130:
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in text[2:])


def _short_redaction(value: str, *, keep: int = 10) -> str:
    if len(value) <= keep:
        return _LOG_REDACTION
    return value[:keep] + _LOG_REDACTION


def _maybe_parse_json(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _sanitize_payload_for_log(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "<max-depth>"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in _LOG_SENSITIVE_KEYS:
                if key_lower == "protocoldata":
                    parsed = _maybe_parse_json(item) if isinstance(item, str) else item
                    sanitized[key_text] = _sanitize_payload_for_log(parsed, _depth=_depth + 1)
                elif isinstance(item, str) and item:
                    sanitized[key_text] = _short_redaction(item)
                else:
                    sanitized[key_text] = _LOG_REDACTION
                continue
            sanitized[key_text] = _sanitize_payload_for_log(item, _depth=_depth + 1)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload_for_log(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload_for_log(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        parsed = _maybe_parse_json(value)
        if parsed is not value:
            return _sanitize_payload_for_log(parsed, _depth=_depth + 1)
        if _looks_like_hex_signature(value):
            return _short_redaction(value)
        return value
    return value


def _sanitize_json_for_log(value: Any, *, limit: int = 3000) -> str:
    payload = json.dumps(_sanitize_payload_for_log(value), default=str, ensure_ascii=False)
    if len(payload) <= limit:
        return payload
    return payload[:limit] + "…"


class OKXSubmitError(RuntimeError):
    """Base execution-track error for OKX submit API calls."""


class OKXRateLimitError(OKXSubmitError):
    """Raised when OKX returns HTTP 429."""


class OKXAuthError(OKXSubmitError):
    """Raised when OKX rejects authentication or permissions."""


class OKXNetworkError(OKXSubmitError):
    """Raised when transport-level failures prevent the request."""


@dataclass(slots=True)
class OfferRefreshResult:
    collection: str
    chain: str
    fetched: int
    stored: int


class OKXAPIClient:
    """Execution-track client backed by the existing OKX auth + transport plumbing."""

    # Class-level cache: (chain, address_lower) → numeric project ID
    # LRU with max 2000 entries to prevent unbounded growth
    _PROJECT_ID_CACHE_MAX = 2000
    _project_id_cache: OrderedDict[tuple[str, str], int] = OrderedDict()

    @classmethod
    def _cache_put(cls, key: tuple[str, str], value: int) -> None:
        """Insert into LRU cache, evicting oldest if over limit."""
        cls._project_id_cache[key] = value
        cls._project_id_cache.move_to_end(key)
        while len(cls._project_id_cache) > cls._PROJECT_ID_CACHE_MAX:
            cls._project_id_cache.popitem(last=False)

    def _load_project_id_cache(self) -> None:
        """Load persistent project ID cache from disk (format: {'chain:0xaddr': project_id, ...})."""
        if not self._project_id_file_cache_path.exists():
            log.debug("_load_project_id_cache: cache file does not exist at %s",
                     self._project_id_file_cache_path)
            return

        try:
            with open(self._project_id_file_cache_path) as f:
                file_cache = json.load(f)

            if not isinstance(file_cache, dict):
                log.warning("_load_project_id_cache: cache file has invalid format (expected dict)")
                return

            loaded_count = 0
            for key_str, project_id in file_cache.items():
                # Key format: "chain:0xaddress"
                if ":" not in key_str:
                    log.debug("_load_project_id_cache: skipping malformed key %s", key_str)
                    continue

                chain, addr = key_str.split(":", 1)
                cache_key = (chain.lower(), addr.lower())

                if str(project_id).isdigit():
                    self._cache_put(cache_key, int(project_id))
                    loaded_count += 1
                else:
                    log.debug("_load_project_id_cache: skipping non-numeric project_id %s for %s",
                             project_id, key_str)

            log.info("_load_project_id_cache: loaded %d entries from %s",
                    loaded_count, self._project_id_file_cache_path)
        except Exception as exc:
            log.warning("_load_project_id_cache: failed to load cache file: %s", exc)

    def _save_project_id_cache(self) -> None:
        """Save in-memory project ID cache to persistent JSON file."""
        try:
            # Create data directory if it doesn't exist
            self._project_id_file_cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert cache keys (chain, address) → "chain:address" format
            file_cache = {}
            for (chain, addr), project_id in self._project_id_cache.items():
                key_str = f"{chain}:{addr}"
                file_cache[key_str] = project_id

            # Atomic write: write to temp file then rename
            import tempfile
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._project_id_file_cache_path.parent),
                suffix=".tmp",
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(file_cache, f, indent=2)
                os.replace(tmp_path, str(self._project_id_file_cache_path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            log.debug("_save_project_id_cache: saved %d entries to %s",
                     len(file_cache), self._project_id_file_cache_path)
        except Exception as exc:
            log.error("_save_project_id_cache: failed to save cache file: %s", exc)

    def __init__(
        self,
        *,
        settings: Settings,
        offers_store: OffersStore | None = None,
        market_client: OKXMarketplaceClient | None = None,
        provider: OKXOffersProvider | None = None,
    ) -> None:
        self.settings = settings
        self.offers_store = offers_store or OffersStore(settings.offers_db_path)
        self.market_client = market_client
        self.provider = provider
        self._project_id_file_cache_path = Path("data/project_id_cache.json")
        self._load_project_id_cache()
        self._last_onchain_cancel_ts: float = 0.0

    @classmethod
    def register_project_id(cls, collection_address: str, chain: str, project_id: str | int) -> None:
        """Cache a known address → project ID mapping (call from parasite_hunter during scanning)."""
        key = (chain.lower(), collection_address.lower())
        pid = str(project_id)
        if pid and pid.isdigit() and pid != "0":
            cls._cache_put(key, int(pid))
            log.debug("register_project_id: %s/%s → %s", chain, collection_address[:14], pid)

    def fetch_offers(
        self,
        *,
        collection: str,
        chain: str,
        limit: int = 100,
        refresh: bool = False,
        maker: str | None = None,
        status: str = "active",
        max_pages: int | None = None,
    ) -> list[NormalizedOffer]:
        if refresh:
            self.refresh_offers(
                collection=collection,
                chain=chain,
                maker=maker,
                status=status,
                max_pages=max_pages,
            )
        return self.offers_store.query_offers(
            OfferFilters(
                market="okx",
                collection=collection,
                chain=chain,
                maker=maker.lower() if maker else None,
                status=status,
                limit=limit,
            )
        )

    def refresh_offers(
        self,
        *,
        collection: str,
        chain: str,
        maker: str | None = None,
        status: str = "active",
        max_pages: int | None = None,
    ) -> OfferRefreshResult:
        provider = self._provider()
        offers = provider.fetch_all_pages(
            chain=chain,
            collection_address=collection,
            maker=maker,
            status=status,
            max_pages=max_pages or self.settings.offers_max_pages_per_run,
        )
        stored = self.offers_store.upsert_offers(offers)
        return OfferRefreshResult(
            collection=collection.lower(),
            chain=chain.lower(),
            fetched=len(offers),
            stored=stored,
        )

    # ── OKX priapi endpoint (discovered from browser network capture) ──
    _SUBMIT_ORDER_PATH = "/priapi/v1/nft/trading/seaport/step/submitOrder"
    _CHAIN_IDS = {"bsc": 56, "eth": 1, "polygon": 137, "arbitrum": 42161}

    def submit_offer(self, signed_payload: dict[str, object]) -> dict[str, object]:
        """Compatibility wrapper: extracts Seaport parameters + signature from
        the old payload format and delegates to submit_seaport_order().
        """
        chain = str(signed_payload.get("chain", "bsc"))
        offerer = str(signed_payload.get("offerer", ""))
        parameters = signed_payload.get("parameters")
        signature = signed_payload.get("signature", "")

        if not parameters or not signature:
            raise OKXSubmitError("submit_offer: missing 'parameters' or 'signature' in payload")

        log.info("submit_offer → delegating to submit_seaport_order (compat wrapper)")
        return self.submit_seaport_order(
            chain=chain,
            wallet_address=offerer,
            parameters=parameters,
            signature=str(signature),
        )

    # RPC endpoints per chain for get_counter (Seaport contract call)
    _CHAIN_RPCS: dict[str, str] = {
        "bsc": "https://bsc-dataseed.binance.org/",
        "eth": "https://eth.llamarpc.com",
        "polygon": "https://polygon-rpc.com/",
        "arbitrum": "https://arb1.arbitrum.io/rpc",
    }

    # ── Two-step offer flow (priapi) ──
    # POST createCollectionOffer / createOffer → sign EIP-712 → POST submitOrder
    # Requires numeric project ID resolved via _resolve_project_id
    _CREATE_COLLECTION_OFFER_PATH = "/priapi/v1/nft/trading/createCollectionOffer"
    _CREATE_OFFER_PATH = "/priapi/v1/nft/trading/createOffer"
    _CREATE_LISTING_PATH = "/priapi/v1/nft/trading/createListing"

    def create_offer(
        self,
        *,
        chain: str,
        wallet_address: str,
        collection_address: str,
        token_id: str | int = "",
        price_raw: str,
        currency_address: str,
        valid_time: int,
        count: int = 1,
        platform: str = "okx",
        project: str | int | None = None,
    ) -> dict[str, Any]:
        """Two-step offer: step1 → sign EIP-712 → submitOrder."""
        from eth_account import Account

        private_key = self.settings.buyer_wallet_private_key
        if not private_key:
            raise OKXSubmitError("create_offer: BUYER_WALLET_PRIVATE_KEY not set")

        account = Account.from_key(private_key)
        chain_lower = chain.lower()
        chain_id = self._CHAIN_IDS.get(chain_lower, 56)
        is_collection_offer = not token_id or str(token_id) in ("", "0")
        ts = int(time.time() * 1000)

        # Resolve numeric project ID
        project_id = project
        if not project_id:
            project_id = self._resolve_project_id(collection_address, chain_lower)

        item: dict[str, Any] = {
            "project": int(project_id) if str(project_id).isdigit() else project_id,
            "price": str(price_raw),
            "currencyAddress": currency_address,
            "count": str(count),
            "source": 4,
            "validTime": valid_time,
        }
        if not is_collection_offer:
            item["tokenId"] = str(token_id)
            item["collectionAddress"] = collection_address

        if is_collection_offer:
            endpoint = f"{self._CREATE_COLLECTION_OFFER_PATH}?t={ts}"
        else:
            endpoint = f"{self._CREATE_OFFER_PATH}?t={ts}"

        step1_payload: dict[str, Any] = {
            "chain": chain_id,
            "walletAddress": account.address,
            "items": [item],
        }

        # Full-flow retry: if step2 fails with "no longer valid" after on-chain cancel,
        # re-run step1+step2 from scratch (OKX may have invalidated the step1 order).
        recent_onchain = (
            hasattr(self, "_last_onchain_cancel_ts")
            and (time.time() - getattr(self, "_last_onchain_cancel_ts", 0)) < 120
        )
        full_retries = 2 if recent_onchain else 1
        last_exc = None

        for full_attempt in range(1, full_retries + 1):
            if full_attempt > 1:
                log.info("create_offer: full-flow retry %d/%d (re-running step1+step2)",
                         full_attempt, full_retries)
                # Update timestamp for retry
                ts = int(time.time() * 1000)
                if is_collection_offer:
                    endpoint = f"{self._CREATE_COLLECTION_OFFER_PATH}?t={ts}"
                else:
                    endpoint = f"{self._CREATE_OFFER_PATH}?t={ts}"
                step1_payload["walletAddress"] = account.address

            log.info("create_offer: POST %s project=%s payload=%s",
                     endpoint, project_id, _sanitize_json_for_log(step1_payload, limit=2000))

            step1_resp = self._request(method="POST", path=endpoint, payload=step1_payload)

            resp_str = _sanitize_json_for_log(step1_resp, limit=3000)
            log.info("create_offer ← response: %s", resp_str)

            code = str(step1_resp.get("code", ""))
            if code not in ("0", ""):
                raise OKXSubmitError(
                    f"createCollectionOffer failed: code={code} msg={step1_resp.get('msg', resp_str[:500])}"
                )

            try:
                return self._complete_two_step_offer(step1_resp, private_key, chain_id, endpoint)
            except OKXSubmitError as exc:
                last_exc = exc
                if "no longer valid" in str(exc).lower() and full_attempt < full_retries:
                    log.warning("create_offer: step2 rejected with '%s' — will retry full flow in 10s",
                                exc)
                    time.sleep(10)
                    continue
                raise

        raise last_exc or OKXSubmitError("create_offer failed after full-flow retries")

    def _resolve_project_id(self, collection_address: str, chain: str) -> str | int:
        """Resolve collection address → OKX numeric project ID.

        Resolution order:
        1. In-memory + persistent file cache (data/project_id_cache.json auto-loaded on init)
        2. buy_config file (project_id field)
        3. Query priapi offers for this collection → extract project from first offer
        4. Query priapi collection-offers → extract project from first offer
        5. Query public collection detail API with slug (if available in config)
        6. HTML scrape fallback (slow but works as last resort)
        """
        addr_lower = collection_address.lower()
        cache_key = (chain.lower(), addr_lower)

        # ── 1. Check in-memory cache (includes file cache loaded on init) ──
        cached = self._project_id_cache.get(cache_key)
        if cached is not None:
            log.info("_resolve_project_id: cache hit %s → %s", addr_lower[:14], cached)
            return cached

        # ── 2. Check buy_config for project_id ──
        try:
            import os
            buy_cfg_path = Path(os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
            if buy_cfg_path.exists():
                with open(buy_cfg_path) as f:
                    buy_cfg = json.load(f)
                coll_cfg = buy_cfg.get("collections", {}).get(addr_lower, {})
                pid = coll_cfg.get("project_id") or coll_cfg.get("projectId")
                if pid and str(pid).isdigit() and str(pid) != "0":
                    log.info("_resolve_project_id: from buy_config %s → %s", addr_lower[:14], pid)
                    self._cache_put(cache_key, int(pid))
                    self._save_project_id_cache()
                    return int(pid)
        except Exception as exc:
            log.debug("_resolve_project_id: buy_config check failed: %s", exc)

        chain_id = str(self._CHAIN_IDS.get(chain.lower(), 56))

        # ── 2. Query offers for this collection via authenticated client (web3.okx.com) ──
        offer_endpoints = [
            ("/api/v5/mktplace/nft/markets/collection-offers",
             {"chain": chain, "collectionAddress": addr_lower, "status": "active", "limit": "5"}),
            ("/api/v5/mktplace/nft/markets/offers",
             {"chain": chain, "collectionAddress": addr_lower, "status": "active", "limit": "5"}),
        ]
        for ep_path, ep_params in offer_endpoints:
            try:
                resp = self._request(method="GET", path=ep_path, params=ep_params)

                items_container = resp.get("data", {})
                if isinstance(items_container, dict):
                    items = (items_container.get("offers") or items_container.get("data")
                             or items_container.get("items") or items_container.get("list") or [])
                elif isinstance(items_container, list):
                    items = items_container
                else:
                    items = []

                if items and isinstance(items[0], dict):
                    first = items[0]
                    log.info("_resolve_project_id: %s returned %d items, first keys=%s",
                             ep_path.split("/")[-1], len(items), sorted(first.keys()))

                    # Check top-level fields
                    for key in ("project", "projectId", "nftProjectId", "collectionId"):
                        val = first.get(key)
                        if val and str(val).isdigit() and str(val) != "0":
                            log.info("_resolve_project_id: %s → %s (field=%s from %s)",
                                     addr_lower[:14], val, key, ep_path.split("/")[-1])
                            self._cache_put(cache_key, int(val))
                            self._save_project_id_cache()
                            return int(val)

                    # Dig into nested dicts/strings: protocolData, collection, etc.
                    for nested_key in ("protocolData", "collection", "contract"):
                        nested = first.get(nested_key)
                        # Parse JSON strings into dicts
                        if isinstance(nested, str) and nested:
                            log.info("_resolve_project_id: %s '%s' raw (str %d chars): %s",
                                     ep_path.split("/")[-1], nested_key, len(nested), _sanitize_json_for_log(nested, limit=300))
                            try:
                                nested = json.loads(nested)
                            except (json.JSONDecodeError, TypeError):
                                pass

                        if nested is None:
                            log.info("_resolve_project_id: %s '%s' is None",
                                     ep_path.split("/")[-1], nested_key)
                            continue

                        if isinstance(nested, dict):
                            log.info("_resolve_project_id: %s nested '%s' keys=%s",
                                     ep_path.split("/")[-1], nested_key, sorted(nested.keys()))
                            # Search recursively: top level + one level of sub-dicts
                            dicts_to_search = [nested]
                            for sub_key, sub_val in nested.items():
                                if isinstance(sub_val, dict):
                                    dicts_to_search.append(sub_val)

                            for d in dicts_to_search:
                                for key in ("project", "projectId", "nftProjectId",
                                            "collectionId", "id"):
                                    val = d.get(key)
                                    if val and str(val).isdigit() and str(val) != "0":
                                        log.info("_resolve_project_id: %s → %s (field=%s in %s from %s)",
                                                 addr_lower[:14], val, key, nested_key,
                                                 ep_path.split("/")[-1])
                                        self._cache_put(cache_key, int(val))
                                        self._save_project_id_cache()
                                        return int(val)

                    # Log orderId for reference
                    oid = first.get("orderId") or first.get("orderHash")
                    if oid:
                        log.info("_resolve_project_id: %s sample orderId=%s",
                                 ep_path.split("/")[-1], str(oid)[:60])
                else:
                    log.info("_resolve_project_id: %s returned 0 items for %s",
                             ep_path.split("/")[-1], addr_lower[:14])
            except Exception as exc:
                log.info("_resolve_project_id: %s failed: %s", ep_path.split("/")[-1], exc)

        # ── 3. Try public collection detail API with slug ──
        # Try to find slug from collections_registry or whitelist
        slug = self._find_slug_for_address(addr_lower)
        if slug:
            try:
                resp = self._request(method="GET", path="/api/v5/mktplace/nft/collection/detail",
                                     params={"slug": slug})
                data = resp.get("data", resp)
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict):
                    log.info("_resolve_project_id: collection/detail slug=%s keys=%s",
                             slug, sorted(data.keys()))
                    for key in ("project", "projectId", "id", "collectionId",
                                "nftProjectId", "projectNo"):
                        val = data.get(key)
                        if val and str(val).isdigit() and str(val) != "0":
                            log.info("_resolve_project_id: %s → %s (field=%s from collection/detail)",
                                     addr_lower[:14], val, key)
                            self._cache_put(cache_key, int(val))
                            self._save_project_id_cache()
                            return int(val)
            except Exception as exc:
                log.debug("_resolve_project_id: collection/detail slug=%s failed: %s", slug, exc)

        # NOTE: priapi/v5/nft/ec/* endpoints return 404 on web3.okx.com — skipped

        # ── 5. Try asset list → extract project from NFT data ──
        try:
            asset_resp = self._request(
                method="GET",
                path="/api/v5/mktplace/nft/asset/list",
                params={"chain": chain, "contractAddress": addr_lower, "limit": "1"},
            )
            asset_data = asset_resp.get("data", asset_resp)
            if isinstance(asset_data, list) and asset_data:
                first_nft = asset_data[0] if isinstance(asset_data[0], dict) else {}
            elif isinstance(asset_data, dict):
                items = asset_data.get("data") or asset_data.get("items") or asset_data.get("list") or []
                first_nft = items[0] if items and isinstance(items[0], dict) else asset_data
            else:
                first_nft = {}

            if first_nft:
                log.info("_resolve_project_id: asset/list keys=%s", sorted(first_nft.keys()))

                # Check top-level AND nested dicts (collection, assetContract)
                dicts_to_check = [("top", first_nft)]
                for nested_key in ("collection", "assetContract", "contract"):
                    nested = first_nft.get(nested_key)
                    if isinstance(nested, dict):
                        log.info("_resolve_project_id: asset/list nested '%s' keys=%s",
                                 nested_key, sorted(nested.keys()))
                        dicts_to_check.append((nested_key, nested))
                    elif isinstance(nested, str) and nested:
                        # collection might be a slug string
                        log.info("_resolve_project_id: asset/list '%s' = %s (string)", nested_key, nested[:80])

                found_slug = None
                for source_name, d in dicts_to_check:
                    for key in ("project", "projectId", "nftProjectId", "collectionId", "id"):
                        val = d.get(key)
                        if val and str(val).isdigit() and str(val) != "0":
                            log.info("_resolve_project_id: %s → %s (field=%s.%s from asset/list)",
                                     addr_lower[:14], val, source_name, key)
                            self._cache_put(cache_key, int(val))
                            self._save_project_id_cache()
                            return int(val)
                    # Collect slug from any level
                    s = d.get("slug") or d.get("collectionSlug")
                    if s and not found_slug:
                        found_slug = s

                # Also check if 'collection' is a string slug
                coll_val = first_nft.get("collection")
                if isinstance(coll_val, str) and coll_val and not coll_val.startswith("0x"):
                    found_slug = found_slug or coll_val

                if found_slug:
                    log.info("_resolve_project_id: found slug=%s from asset, trying detail", found_slug)
                    try:
                        detail_resp = self._request(
                            method="GET",
                            path="/api/v5/mktplace/nft/collection/detail",
                            params={"slug": found_slug},
                        )
                        dd = detail_resp.get("data", detail_resp)
                        if isinstance(dd, list) and dd:
                            dd = dd[0]
                        if isinstance(dd, dict):
                            log.info("_resolve_project_id: collection/detail via asset slug=%s keys=%s",
                                     found_slug, sorted(dd.keys()))
                            # Check top-level
                            for key in ("project", "projectId", "id", "collectionId"):
                                val = dd.get(key)
                                if val and str(val).isdigit() and str(val) != "0":
                                    log.info("_resolve_project_id: %s → %s (from asset slug %s detail)",
                                             addr_lower[:14], val, found_slug)
                                    self._cache_put(cache_key, int(val))
                                    self._save_project_id_cache()
                                    return int(val)
                            # Check nested: stats, assetContracts
                            for nk in ("stats", "assetContracts"):
                                nv = dd.get(nk)
                                if isinstance(nv, dict):
                                    log.info("_resolve_project_id: detail[%s] keys=%s", nk, sorted(nv.keys()))
                                    for key in ("project", "projectId", "id", "collectionId"):
                                        val = nv.get(key)
                                        if val and str(val).isdigit() and str(val) != "0":
                                            log.info("_resolve_project_id: %s → %s (from detail.%s.%s)",
                                                     addr_lower[:14], val, nk, key)
                                            self._cache_put(cache_key, int(val))
                                            self._save_project_id_cache()
                                            return int(val)
                                elif isinstance(nv, list) and nv:
                                    log.info("_resolve_project_id: detail[%s] is list[%d], first keys=%s",
                                             nk, len(nv),
                                             sorted(nv[0].keys()) if isinstance(nv[0], dict) else type(nv[0]).__name__)
                    except Exception:
                        pass
        except Exception as exc:
            log.info("_resolve_project_id: asset/list failed: %s", exc)

        # ── 6. Scrape HTML collection page — project ID is in embedded JSON ──
        # Works with both slug and raw address: /nft/collection/{chain}/{slug_or_address}
        page_id = slug or addr_lower
        page_url = f"https://web3.okx.com/nft/collection/{chain.lower()}/{page_id}"
        try:
            import urllib.request
            import urllib.error
            log.warning("_resolve_project_id: SLOW: falling back to HTML scrape for %s (took %s to reach this point)",
                       addr_lower[:14], page_url)
            req = urllib.request.Request(page_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            # Look for "project":DIGITS in the embedded JSON
            m = re.search(r'"project"\s*:\s*(\d{2,})', html)
            if m:
                pid = int(m.group(1))
                log.info("_resolve_project_id: HTML scrape %s → project=%d from %s",
                         addr_lower[:14], pid, page_url)
                self._cache_put(cache_key, pid)
                self._save_project_id_cache()
                return pid
            else:
                log.info("_resolve_project_id: HTML scrape %s — no project ID in page (%d chars)",
                         addr_lower[:14], len(html))
        except Exception as exc:
            log.info("_resolve_project_id: HTML scrape failed for %s: %s", page_url, exc)

        log.error("_resolve_project_id: FAILED to resolve %s on %s — "
                   "offer will likely fail. Add project_id to buy_config or ensure "
                   "there are active offers on this collection.",
                   addr_lower[:14], chain)
        return addr_lower  # fallback — will cause 400 error but at least logs will show it

    @staticmethod
    def _find_slug_for_address(collection_address: str) -> str | None:
        """Try to find OKX slug for a collection address from config files."""
        import os
        from pathlib import Path

        addr_lower = collection_address.lower()
        # Check collections_registry.json
        for config_name in ("collections_registry.json", "sniper_config.json"):
            config_path = Path(os.getenv("CONFIG_DIR", "./config")) / config_name
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                # collections_registry has list under "collections"
                items = cfg.get("collections", cfg.get("sniper_targets", []))
                if isinstance(items, list):
                    for item in items:
                        if (item.get("collection_address", "").lower() == addr_lower
                                or item.get("contract_address", "").lower() == addr_lower):
                            slug = item.get("collection_slug") or item.get("slug")
                            if slug:
                                return slug
            except Exception:
                continue
        return None

    def _complete_two_step_offer(
        self, step1_resp: dict[str, Any], private_key: str, chain_id: int, endpoint: str | None,
    ) -> dict[str, Any]:
        """Step 2: extract EIP-712 data from step1 response, sign, submit."""
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        data = step1_resp.get("data", step1_resp)

        # Extract steps
        steps = data.get("steps", [])

        # ── AUTO-APPROVE: if ApprovalCurrency status is "incomplete", approve on-chain ──
        for step in steps:
            action = step.get("action", "") if isinstance(step, dict) else ""
            if "approval" in action.lower():
                for approval_item in step.get("items", []):
                    if approval_item.get("status") == "incomplete":
                        token_addr = approval_item.get("tokenAddress", "")
                        spender = approval_item.get("approvalAddress", "")
                        currency = approval_item.get("currency", "?")
                        if token_addr and spender:
                            log.warning("create_offer: %s approval INCOMPLETE — auto-approving %s for %s",
                                        currency, token_addr[:14], spender[:14])
                            try:
                                self._auto_approve_erc20(
                                    token_address=token_addr,
                                    spender_address=spender,
                                    private_key=private_key,
                                    chain_id=chain_id,
                                )
                            except Exception as exc:
                                log.error("create_offer: auto-approve %s FAILED: %s", currency, exc)
                                raise OKXSubmitError(f"ERC20 approval failed for {currency}: {exc}")

        # Find SignOrders step
        sign_step = None
        for step in steps:
            action = step.get("action", "") if isinstance(step, dict) else ""
            if "sign" in action.lower():
                sign_step = step
                break

        if not sign_step:
            log.error("create_offer step2: no SignOrders step in response. data keys: %s", list(data.keys()))
            raise OKXSubmitError("create_offer: OKX response has no SignOrders step")

        # Extract EIP-712 structured data from SignOrders
        # OKX response: steps[i].items[0] has {data, domain, kind, post, ...}
        sign_items = sign_step.get("items", [])
        sign_item = sign_items[0] if sign_items else sign_step
        log.info("create_offer step2: sign_item keys=%s", sorted(sign_item.keys()))

        eip712_data = sign_item.get("data", sign_step.get("data", {}))
        eip712_domain = sign_item.get("domain", eip712_data.get("domain", {}))
        eip712_types = sign_item.get("types", eip712_data.get("types", {}))

        # The Seaport order fields ARE the message (no separate "message" wrapper)
        # Fields: conduitKey, consideration, counter, endTime, offer, offerer, orderType, salt, etc.
        eip712_message = eip712_data.get("message", eip712_data)
        primary_type = eip712_data.get("primaryType",
                                        sign_item.get("primaryType", "OrderComponents"))

        # Build EIP-712 types if not provided (Seaport 1.6 OrderComponents)
        if not eip712_types:
            eip712_types = {
                "OrderComponents": [
                    {"name": "offerer", "type": "address"},
                    {"name": "zone", "type": "address"},
                    {"name": "offer", "type": "OfferItem[]"},
                    {"name": "consideration", "type": "ConsiderationItem[]"},
                    {"name": "orderType", "type": "uint8"},
                    {"name": "startTime", "type": "uint256"},
                    {"name": "endTime", "type": "uint256"},
                    {"name": "zoneHash", "type": "bytes32"},
                    {"name": "salt", "type": "uint256"},
                    {"name": "conduitKey", "type": "bytes32"},
                    {"name": "counter", "type": "uint256"},
                ],
                "OfferItem": [
                    {"name": "itemType", "type": "uint8"},
                    {"name": "token", "type": "address"},
                    {"name": "identifierOrCriteria", "type": "uint256"},
                    {"name": "startAmount", "type": "uint256"},
                    {"name": "endAmount", "type": "uint256"},
                ],
                "ConsiderationItem": [
                    {"name": "itemType", "type": "uint8"},
                    {"name": "token", "type": "address"},
                    {"name": "identifierOrCriteria", "type": "uint256"},
                    {"name": "startAmount", "type": "uint256"},
                    {"name": "endAmount", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                ],
            }

        # If eip712_message IS the raw order data (no "message" wrapper), use it as-is
        # but strip non-order fields (domain, types, etc.)
        if "offerer" in eip712_message and "primaryType" not in eip712_message:
            pass  # eip712_message is already the clean order data
        elif "offerer" in eip712_message:
            # Has extra keys mixed in — extract only OrderComponents fields
            order_keys = {f["name"] for f in eip712_types.get("OrderComponents", [])}
            eip712_message = {k: v for k, v in eip712_message.items() if k in order_keys}

        structured = {
            "types": eip712_types,
            "domain": eip712_domain,
            "primaryType": primary_type,
            "message": eip712_message,
        }

        log.info("create_offer step2: signing EIP-712 primaryType=%s domain=%s message_keys=%s",
                 primary_type, json.dumps(eip712_domain, default=str)[:200],
                 sorted(eip712_message.keys()) if isinstance(eip712_message, dict) else "?")

        encoded = encode_typed_data(full_message=structured)
        signed = Account.sign_message(encoded, private_key=private_key)
        signature = "0x" + signed.signature.hex()

        # Extract post body template from sign_item (not sign_step)
        post_info = sign_item.get("post", sign_step.get("post", data.get("post", {})))
        post_body = post_info.get("body", {})
        post_endpoint = post_info.get("endpoint", self._SUBMIT_ORDER_PATH)

        # Fill signature into post body
        # OKX expects signature at top level AND/OR in items[].protocolData
        post_body["signature"] = signature

        # Also inject into each item's protocolData (Seaport format)
        items_list = post_body.get("items", [])
        for it in items_list:
            if isinstance(it, dict):
                it["protocolData"] = json.dumps({
                    "parameters": eip712_message,
                    "signature": signature,
                })

        # Some responses split into r, s, v
        if "r" in post_body:
            r = "0x" + signed.r.to_bytes(32, "big").hex() if hasattr(signed, 'r') else ""
            s = "0x" + signed.s.to_bytes(32, "big").hex() if hasattr(signed, 's') else ""
            post_body["r"] = r
            post_body["s"] = s

        # --- Step 2 submission with retry ---
        # After on-chain cancel, OKX backend needs extra time to sync chain state.
        # Use more retries with longer delays if a recent on-chain cancel occurred.
        recent_onchain = (
            hasattr(self, "_last_onchain_cancel_ts")
            and (time.time() - getattr(self, "_last_onchain_cancel_ts", 0)) < 60
        )
        max_retries = 4 if recent_onchain else 2
        if recent_onchain:
            # Wait extra before first attempt to let OKX sync on-chain state
            extra_wait = max(0, 8.0 - (time.time() - self._last_onchain_cancel_ts))
            if extra_wait > 0:
                log.info("create_offer step2: recent on-chain cancel — waiting %.1fs for OKX sync", extra_wait)
                time.sleep(extra_wait)
        last_err_msg = ""
        # Progressive delays: 1s, 3s, 5s, 8s
        _retry_delays = [1.0, 3.0, 5.0, 8.0]
        for attempt in range(1, max_retries + 1):
            # Delay before step2 to give OKX backend time
            delay = _retry_delays[attempt - 1] if attempt <= len(_retry_delays) else 3.0
            time.sleep(delay)

            ts = int(time.time() * 1000)
            path = f"{post_endpoint}?t={ts}" if "?" not in post_endpoint else post_endpoint

            log.info("create_offer step2 [attempt %d/%d]: POST %s body=%s",
                     attempt, max_retries, path, _sanitize_json_for_log(post_body, limit=2000))

            response = self._request(method="POST", path=path, payload=post_body)
            resp_str = _sanitize_json_for_log(response, limit=2000)
            log.info("create_offer step2 ← response: %s", resp_str)

            code = str(response.get("code", ""))
            if code not in ("0", ""):
                raise OKXSubmitError(f"submitOrder failed: code={code} msg={response.get('msg', resp_str[:500])}")

            # Extract offer ID from step2 response.
            # OKX submitOrder returns {"data": {"successOrderIds": [...], "errors": [...]}}
            resp_data = response.get("data", {})
            if isinstance(resp_data, dict):
                success_ids = resp_data.get("successOrderIds", [])
                errors = resp_data.get("errors", [])
                if errors:
                    err_msgs = [e.get("message", str(e)) for e in errors if isinstance(e, dict)]
                    log.warning("create_offer step2 errors: %s", "; ".join(err_msgs))
                if success_ids:
                    offer_id = str(success_ids[0])
                    log.info("create_offer step2 SUCCESS: offer_id=%s (attempt %d)", offer_id, attempt)
                    return {"offer_id": offer_id, "order_id": offer_id, "status": "submitted", "raw": response}
                if errors and not success_ids:
                    last_err_msg = errors[0].get("message", "unknown") if errors else "unknown"
                    # Retry on "no longer valid" — OKX may need time after on-chain cancel
                    if "no longer valid" in last_err_msg.lower() and attempt < max_retries:
                        next_delay = _retry_delays[attempt] if attempt < len(_retry_delays) else 5.0
                        log.warning("create_offer step2: '%s' — retrying in %.0fs (attempt %d/%d)",
                                    last_err_msg, next_delay, attempt, max_retries)
                        continue
                    raise OKXSubmitError(f"submitOrder rejected: {last_err_msg}")

            # Fallback: try older response format
            offer_id = self._extract_scalar(response, keys=("orderId", "offerId", "orderHash", "id"))
            oid = str(offer_id) if offer_id else None
            return {"offer_id": oid, "order_id": oid, "status": "submitted", "raw": response}

        # Should not reach here, but just in case
        raise OKXSubmitError(f"submitOrder failed after {max_retries} attempts: {last_err_msg}")

    def _auto_approve_erc20(
        self, *, token_address: str, spender_address: str,
        private_key: str, chain_id: int,
    ) -> str:
        """Send ERC20 approve(spender, maxUint256) on-chain. Returns tx hash."""
        from web3 import Web3
        from eth_account import Account

        chain_name = {56: "bsc", 1: "eth", 137: "polygon", 42161: "arbitrum"}.get(chain_id, "bsc")
        rpc_url = self._CHAIN_RPCS.get(chain_name, self._CHAIN_RPCS["bsc"])
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        # Minimal ERC20 ABI — only approve()
        erc20_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            }
        ]

        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=erc20_abi,
        )
        account = Account.from_key(private_key)
        max_uint256 = 2**256 - 1

        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        tx = token_contract.functions.approve(
            Web3.to_checksum_address(spender_address),
            max_uint256,
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gasPrice": int(gas_price * 1.1),
            "gas": 60_000,
            "chainId": chain_id,
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex()
        log.info("auto_approve_erc20: %s approve(%s) tx=%s — waiting confirmation...",
                 token_address[:14], spender_address[:14], tx_hex)

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise RuntimeError(f"approve tx reverted: {tx_hex}")

        log.info("auto_approve_erc20: CONFIRMED tx=%s gasUsed=%d", tx_hex, receipt["gasUsed"])
        return tx_hex

    def _create_offer_direct(
        self, *, chain: str, chain_id: int, account: Any,
        private_key: str, collection_address: str, token_id: str | int,
        price_raw: str, currency_address: str, valid_time: int,
    ) -> dict[str, Any]:
        """Fallback: build Seaport order locally, sign, submit directly."""
        from okx_nft_bot.signing.seaport_signer import (
            build_order_payload, build_per_item_offer, get_counter, sign_order,
        )
        rpc_url = self._CHAIN_RPCS.get(chain, self._CHAIN_RPCS["bsc"])
        counter = get_counter(account.address, rpc_url=rpc_url)

        is_collection_offer = not token_id or str(token_id) in ("", "0")
        int_token_id = 0 if is_collection_offer else int(token_id)
        now = int(time.time())
        duration_s = max(valid_time - now, 3600)

        if is_collection_offer:
            parameters = build_order_payload(
                offerer=account.address, collection=collection_address,
                price_wei=int(price_raw), counter=counter, duration_s=duration_s,
                offer_token=currency_address or None,
            )
        else:
            parameters = build_per_item_offer(
                offerer=account.address, collection=collection_address,
                token_id=int_token_id, price_wei=int(price_raw), counter=counter,
                duration_s=duration_s, offer_token=currency_address or None,
            )

        signature = sign_order(parameters, private_key, chain_id=chain_id)

        log.info("create_offer_direct chain=%s coll=%s token=%s → submit_seaport_order",
                 chain, collection_address[:14], token_id or "collection")
        return self.submit_seaport_order(
            chain=chain, wallet_address=account.address,
            parameters=parameters, signature=signature,
        )

    def submit_seaport_order(
        self,
        *,
        chain: str,
        wallet_address: str,
        parameters: dict[str, Any] | Any,
        signature: str,
    ) -> dict[str, Any]:
        """POST signed Seaport order to OKX priapi submitOrder endpoint.

        Endpoint: /priapi/v1/nft/trading/seaport/step/submitOrder
        Payload: OKX expects Seaport AdvancedOrder format inside items[]:
          items: [{parameters: OrderParameters, numerator, denominator, signature, extraData}]
        OrderParameters = OrderComponents minus counter plus totalOriginalConsiderationItems.
        """
        chain_id = self._CHAIN_IDS.get(chain.lower(), 56)
        ts = int(time.time() * 1000)
        path = f"{self._SUBMIT_ORDER_PATH}?t={ts}"

        # Build OrderParameters (Seaport struct) — counter is NOT part of
        # OrderParameters, only of OrderComponents (used for EIP-712 signing).
        order_params: dict[str, Any] = {
            "offerer": parameters["offerer"],
            "zone": parameters["zone"],
            "offer": parameters["offer"],
            "consideration": parameters["consideration"],
            "orderType": int(parameters["orderType"]),
            "startTime": str(parameters["startTime"]),
            "endTime": str(parameters["endTime"]),
            "zoneHash": parameters["zoneHash"],
            "salt": str(parameters["salt"]),
            "conduitKey": parameters["conduitKey"],
            "totalOriginalConsiderationItems": int(
                parameters.get("totalOriginalConsiderationItems", len(parameters["consideration"]))
            ),
        }

        # Wrap as Seaport AdvancedOrder — this is what OKX expects in items[]
        advanced_order: dict[str, Any] = {
            "parameters": order_params,
            "numerator": 1,
            "denominator": 1,
            "signature": signature,
            "extraData": "0x",
        }

        payload: dict[str, Any] = {
            "chain": chain_id,
            "items": [advanced_order],
            "orderData": "",
            "r": "",
            "s": "",
            "signature": signature,
            "walletAddress": wallet_address,
        }

        log.info(
            "submit_seaport_order → POST %s chain=%d wallet=%s payload=%s",
            self._SUBMIT_ORDER_PATH, chain_id, wallet_address[:10],
            _sanitize_json_for_log(payload, limit=4000),
        )

        response = self._request(
            method="POST",
            path=path,
            payload=payload,
        )
        resp_str = _sanitize_json_for_log(response, limit=3000)
        log.info("submit_seaport_order ← response: %s", resp_str)

        code = str(response.get("code", ""))
        if code not in ("0", ""):
            msg = response.get("msg") or response.get("message") or resp_str[:500]
            log.error("submit_seaport_order FAILED code=%s msg=%s", code, msg)
            raise OKXSubmitError(f"OKX submitOrder failed: code={code} msg={msg}")

        offer_id = self._extract_scalar(response, keys=("orderId", "offerId", "orderHash", "id"))
        status = self._extract_scalar(response, keys=("status", "state")) or "submitted"

        log.info("submit_seaport_order ✓ offer_id=%s status=%s", offer_id, status)

        return {
            "offer_id": str(offer_id) if offer_id else None,
            "status": str(status),
            "raw": response,
        }

    def cancel_offer(self, offer_id: str, chain: str = "bsc",
                     order_params: dict | None = None) -> bool:
        """Cancel a specific offer.

        Strategy:
        1. Try OKX API cancel-listing (works for listings, sometimes offers)
        2. If API fails → on-chain Seaport cancel() for this specific order

        order_params: the protocolData.parameters dict from the offer.
        Required for on-chain fallback.  If not provided, on-chain cancel
        is skipped.
        """
        if not self.settings.buyer_wallet_address:
            log.error("cancel_offer: BUYER_WALLET_ADDRESS not set")
            return False

        # ── Attempt 1: OKX API ──
        api_ok = self._cancel_via_api(offer_id, chain)
        if api_ok:
            return True

        # ── Attempt 2: on-chain Seaport cancel ──
        if order_params:
            log.info("cancel_offer %s: API failed, trying on-chain Seaport cancel",
                     offer_id[:14])
            return self._cancel_onchain_seaport(order_params, chain)

        log.warning("cancel_offer %s: API failed and no order_params for on-chain cancel",
                    offer_id[:14])
        return False

    def _cancel_via_api(self, offer_id: str, chain: str) -> bool:
        """Try OKX API cancel-listing endpoint."""
        chain_map = {"eth": "eth", "bsc": "bsc", "polygon": "polygon"}
        chain_name = chain_map.get(chain.lower(), chain.lower())

        payload = {
            "chain": chain_name,
            "walletAddress": self.settings.buyer_wallet_address,
            "orderIds": [str(offer_id)],
        }
        try:
            response = self._request(
                method="POST",
                path="/api/v5/mktplace/nft/markets/cancel-listing",
                payload=payload,
            )
        except Exception as exc:
            log.debug("cancel_via_api %s: request failed: %s", offer_id[:14], exc)
            return False

        resp_code = str(response.get("code", "0"))
        resp_msg = response.get("msg", "")

        # "No longer available" = offer is an OFFER not a LISTING, API can't cancel it
        if "no longer" in resp_msg.lower():
            log.debug("cancel_via_api %s: '%s' — API cannot cancel offers", offer_id[:14], resp_msg)
            return False

        if resp_code != "0":
            log.debug("cancel_via_api %s: code=%s msg=%s", offer_id[:14], resp_code, resp_msg)
            return False

        log.info("cancel_offer %s: SUCCESS via API", offer_id[:14])
        return True

    # Seaport 1.6 addresses per chain
    _SEAPORT_ADDRESSES = {
        "bsc": "0x0000000000000068f116a894984e2db1123eb395",
        "eth": "0x0000000000000068f116a894984e2db1123eb395",
    }

    _SEAPORT_CANCEL_ABI = [
        {
            "inputs": [
                {
                    "components": [
                        {"name": "offerer", "type": "address"},
                        {"name": "zone", "type": "address"},
                        {"name": "offer", "type": "tuple[]", "components": [
                            {"name": "itemType", "type": "uint8"},
                            {"name": "token", "type": "address"},
                            {"name": "identifierOrCriteria", "type": "uint256"},
                            {"name": "startAmount", "type": "uint256"},
                            {"name": "endAmount", "type": "uint256"},
                        ]},
                        {"name": "consideration", "type": "tuple[]", "components": [
                            {"name": "itemType", "type": "uint8"},
                            {"name": "token", "type": "address"},
                            {"name": "identifierOrCriteria", "type": "uint256"},
                            {"name": "startAmount", "type": "uint256"},
                            {"name": "endAmount", "type": "uint256"},
                            {"name": "recipient", "type": "address"},
                        ]},
                        {"name": "orderType", "type": "uint8"},
                        {"name": "startTime", "type": "uint256"},
                        {"name": "endTime", "type": "uint256"},
                        {"name": "zoneHash", "type": "bytes32"},
                        {"name": "salt", "type": "uint256"},
                        {"name": "conduitKey", "type": "bytes32"},
                        {"name": "counter", "type": "uint256"},
                    ],
                    "name": "orders",
                    "type": "tuple[]",
                }
            ],
            "name": "cancel",
            "outputs": [{"name": "cancelled", "type": "bool"}],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [{"name": "offerer", "type": "address"}],
            "name": "getCounter",
            "outputs": [{"name": "counter", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    _RPC_URLS = {
        "bsc": "https://bsc-dataseed.binance.org/",
        "eth": "https://eth.llamarpc.com",
    }

    def _cancel_onchain_seaport(self, order_params: dict, chain: str) -> bool:
        """Cancel a specific Seaport order on-chain via cancel()."""
        try:
            from web3 import Web3
            from eth_account import Account
        except ImportError:
            log.error("_cancel_onchain: web3 or eth_account not installed")
            return False

        private_key = self.settings.buyer_wallet_private_key
        if not private_key:
            log.error("_cancel_onchain: no private key")
            return False

        chain_lower = chain.lower()
        seaport_addr = self._SEAPORT_ADDRESSES.get(chain_lower)
        rpc_url = self._RPC_URLS.get(chain_lower)
        if not seaport_addr or not rpc_url:
            log.error("_cancel_onchain: unsupported chain %s", chain)
            return False

        try:
            account = Account.from_key(private_key)
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not w3.is_connected():
                log.error("_cancel_onchain: cannot connect to %s RPC", chain)
                return False

            seaport = w3.eth.contract(
                address=Web3.to_checksum_address(seaport_addr),
                abi=self._SEAPORT_CANCEL_ABI,
            )

            # Get current counter
            counter = seaport.functions.getCounter(account.address).call()

            # Build OrderComponents from protocolData.parameters
            params = order_params
            order_components = {
                "offerer": Web3.to_checksum_address(params["offerer"]),
                "zone": Web3.to_checksum_address(params.get("zone", "0x" + "0" * 40)),
                "offer": [
                    {
                        "itemType": int(o["itemType"]),
                        "token": Web3.to_checksum_address(o["token"]),
                        "identifierOrCriteria": int(o.get("identifierOrCriteria", 0)),
                        "startAmount": int(o["startAmount"]),
                        "endAmount": int(o["endAmount"]),
                    }
                    for o in params.get("offer", [])
                ],
                "consideration": [
                    {
                        "itemType": int(c["itemType"]),
                        "token": Web3.to_checksum_address(c["token"]),
                        "identifierOrCriteria": int(c.get("identifierOrCriteria", 0)),
                        "startAmount": int(c["startAmount"]),
                        "endAmount": int(c["endAmount"]),
                        "recipient": Web3.to_checksum_address(c["recipient"]),
                    }
                    for c in params.get("consideration", [])
                ],
                "orderType": int(params.get("orderType", 2)),
                "startTime": int(params.get("startTime", 0)),
                "endTime": int(params.get("endTime", 0)),
                "zoneHash": bytes.fromhex(params.get("zoneHash", "0x" + "0" * 64).replace("0x", "")),
                "salt": int(params.get("salt", 0)),
                "conduitKey": bytes.fromhex(params.get("conduitKey", "0x" + "0" * 64).replace("0x", "")),
                "counter": counter,
            }

            # Build transaction
            nonce = w3.eth.get_transaction_count(account.address)
            tx = seaport.functions.cancel([order_components]).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })

            gas_estimate = w3.eth.estimate_gas(tx)
            tx["gas"] = int(gas_estimate * 1.3)

            # Sign and send
            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            log.info("_cancel_onchain: TX sent %s (gas=%d)", tx_hash.hex()[:16], tx["gas"])

            # Wait for receipt
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.get("status") == 1:
                log.info("cancel_offer: SUCCESS on-chain TX=%s gas=%d",
                         tx_hash.hex()[:16], receipt.get("gasUsed", 0))
                # Track last on-chain cancel time — OKX needs time to sync
                self._last_onchain_cancel_ts = time.time()
                return True
            else:
                log.error("_cancel_onchain: TX REVERTED %s", tx_hash.hex()[:16])
                return False

        except Exception as exc:
            log.error("_cancel_onchain: failed: %s", exc)
            return False

    def create_listing(
        self,
        *,
        chain: str,
        wallet_address: str,
        collection_address: str,
        token_id: str | int,
        price_raw: str,
        currency_address: str,
        valid_time: int,
        platform: str = "okx",
        project: str | int | None = None,
    ) -> dict[str, Any]:
        """Two-step listing: step1 (createListing) → sign EIP-712 → submitOrder.

        Mirrors create_offer but for SELL orders (list NFT for sale).
        """
        from eth_account import Account

        private_key = self.settings.buyer_wallet_private_key
        if not private_key:
            raise OKXSubmitError("create_listing: BUYER_WALLET_PRIVATE_KEY not set")

        account = Account.from_key(private_key)
        chain_lower = chain.lower()
        chain_id = self._CHAIN_IDS.get(chain_lower, 56)
        ts = int(time.time() * 1000)

        project_id = project
        if not project_id:
            project_id = self._resolve_project_id(collection_address, chain_lower)

        item: dict[str, Any] = {
            "project": int(project_id) if str(project_id).isdigit() else project_id,
            "tokenId": str(token_id),
            "collectionAddress": collection_address,
            "price": str(price_raw),
            "currencyAddress": currency_address,
            "count": "1",
            "source": 4,
            "validTime": valid_time,
        }

        endpoint = f"{self._CREATE_LISTING_PATH}?t={ts}"

        step1_payload: dict[str, Any] = {
            "chain": chain_id,
            "walletAddress": account.address,
            "items": [item],
        }

        log.info("create_listing: POST %s project=%s token=%s price=%s",
                 endpoint, project_id, token_id, price_raw)

        step1_resp = self._request(method="POST", path=endpoint, payload=step1_payload)

        resp_str = json.dumps(step1_resp, default=str)
        log.info("create_listing ← response (%d chars): %s", len(resp_str), resp_str[:3000])

        code = str(step1_resp.get("code", ""))
        if code not in ("0", ""):
            msg = step1_resp.get("msg") or step1_resp.get("message") or resp_str[:500]
            log.error("create_listing step1 FAILED code=%s msg=%s", code, msg)
            raise OKXSubmitError(f"create_listing step1 failed: code={code} msg={msg}")

        # Step 2: reuse the same two-step flow as create_offer
        # (sign EIP-712 + submitOrder — identical Seaport flow)
        return self._complete_two_step_offer(step1_resp, private_key, chain_id, endpoint)

    def cancel_listing(self, order_id: str) -> bool:
        """Cancel an active listing (sell order)."""
        response = self._request(
            method="POST",
            path="/api/v5/mktplace/nft/markets/listings/cancel",
            payload={"orderId": order_id},
        )
        if str(response.get("code", "0")) != "0":
            # Fallback: try offers cancel endpoint
            response = self._request(
                method="POST",
                path="/api/v5/mktplace/nft/markets/offers/cancel",
                payload={"offerId": order_id},
            )
        if str(response.get("code", "0")) != "0":
            return False
        success = self._extract_scalar(response, keys=("success", "cancelled", "result"))
        if success is None:
            return True
        if isinstance(success, bool):
            return success
        return str(success).lower() not in {"0", "false", "failed"}

    def get_listings(
        self,
        *,
        chain: str,
        collection_address: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v5/mktplace/nft/markets/listings for a collection."""
        return self._request(
            method="GET",
            path="/api/v5/mktplace/nft/markets/listings",
            params={
                "chain": chain,
                "collectionAddress": collection_address,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_wallet_nfts(
        self,
        *,
        chain: str,
        wallet_address: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET wallet's NFT inventory via OKX owner asset-list endpoint."""
        return self._request(
            method="GET",
            path="/api/v5/mktplace/nft/owner/asset-list",
            params={
                "chain": chain,
                "ownerAddress": wallet_address,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_my_offers(self, chain: str = "bsc",
                      collection_address: str = "",
                      *,
                      require_all_endpoints: bool = False) -> list[dict[str, object]]:
        """Fetch our active offers.

        Queries BOTH token-offer AND collection-offer endpoints because
        the bot places collection offers via createCollectionOffer but the
        /offers endpoint only returns token-level offers.  Previously only
        /offers was checked → always 0 results → self-bidding bug.
        """
        if not self.settings.buyer_wallet_address:
            raise RuntimeError("BUYER_WALLET_ADDRESS is required for get_my_offers()")

        wallet = self.settings.buyer_wallet_address
        all_records: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        endpoint_errors: list[str] = []

        # Query both endpoints: token offers + collection offers
        endpoints = [
            "/api/v5/mktplace/nft/markets/offers",
            "/api/v5/mktplace/nft/markets/collection-offers",
        ]
        for path in endpoints:
            try:
                params: dict[str, object] = {
                    "chain": chain.lower(),
                    "status": "active",
                    "limit": self.settings.okx_page_limit,
                }
                # If collection address given, filter by it (more reliable
                # than maker filter which OKX may ignore)
                if collection_address:
                    params["collectionAddress"] = collection_address
                else:
                    params["maker"] = wallet

                response = self._request(method="GET", path=path, params=params)
                resp_code = str(response.get("code", "0"))
                if resp_code not in ("0", ""):
                    log.debug("get_my_offers: %s returned code=%s — skip",
                              path.split("/")[-1], resp_code)
                    continue
                records = self._extract_records(response)
                for rec in records:
                    # When queried by collectionAddress, filter by maker locally
                    if collection_address:
                        rec_maker = (rec.get("maker") or rec.get("makerAddress") or "").lower()
                        if rec_maker != wallet.lower():
                            continue
                    oid = str(rec.get("orderId") or rec.get("orderHash")
                              or rec.get("id") or "")
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        all_records.append(dict(rec))
            except Exception as exc:
                log.warning("get_my_offers: %s FAILED: %s", path.split("/")[-1], exc)
                endpoint_errors.append(f"{path.split('/')[-1]}: {exc}")

        # If ALL endpoints failed, raise so callers know data is unreliable
        if endpoint_errors and not all_records:
            raise RuntimeError(
                f"get_my_offers: all endpoints failed — {'; '.join(endpoint_errors)}"
            )
        # Critical execution flows should not treat partial endpoint failure
        # as authoritative exchange truth.
        if endpoint_errors and require_all_endpoints:
            raise RuntimeError(
                f"get_my_offers: partial endpoint failure — {'; '.join(endpoint_errors)}"
            )
        if endpoint_errors:
            log.warning("get_my_offers: partial failure (%d endpoints failed) — "
                        "results may be incomplete: %s",
                        len(endpoint_errors), "; ".join(endpoint_errors))

        log.info("get_my_offers: chain=%s maker=%s coll=%s → %d records (token+collection)",
                 chain, wallet[:14],
                 collection_address[:14] if collection_address else "all",
                 len(all_records))
        return all_records

    def _request(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._market_client()
        body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        url, request_path = build_url(self.settings.okx_api_base, path, params)

        last_exc: Exception | None = None
        for attempt in range(_MAX_429_RETRIES + 1):
            headers = client._build_headers(method=method.upper(), request_path=request_path, body=body)
            try:
                return client.transport.request_json(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    body=body,
                )
            except HTTPStatusError as exc:
                if exc.status == 429 and attempt < _MAX_429_RETRIES:
                    wait = _429_BACKOFF_BASE * (2 ** attempt)
                    log.warning("OKX 429 rate limit on %s %s — retry %d/%d in %.1fs",
                                method, path, attempt + 1, _MAX_429_RETRIES, wait)
                    time.sleep(wait)
                    last_exc = exc
                    continue
                if exc.status == 429:
                    raise OKXRateLimitError(str(exc)) from exc
                if exc.status in {401, 403}:
                    raise OKXAuthError(str(exc)) from exc
                raise OKXSubmitError(str(exc)) from exc
            except RuntimeError as exc:
                raise OKXNetworkError(str(exc)) from exc

        # Should not reach here, but just in case
        raise OKXRateLimitError(f"Rate limit after {_MAX_429_RETRIES} retries") from last_exc

    def _market_client(self) -> OKXMarketplaceClient:
        if self.market_client is None:
            self.market_client = OKXMarketplaceClient(settings=self.settings)
        return self.market_client

    def _provider(self) -> OKXOffersProvider:
        if self.provider is not None:
            return self.provider
        client = self.market_client or OKXMarketplaceClient(settings=self.settings)
        self.provider = OKXOffersProvider(client=client, settings=self.settings)
        return self.provider

    @staticmethod
    def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            return [data]
        return []

    @classmethod
    def _extract_scalar(cls, payload: dict[str, Any], *, keys: tuple[str, ...]) -> Any | None:
        records = cls._extract_records(payload)
        if records:
            first = records[0]
            for key in keys:
                if key in first and first[key] is not None:
                    return first[key]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in keys:
                if key in data and data[key] is not None:
                    return data[key]
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        return None
