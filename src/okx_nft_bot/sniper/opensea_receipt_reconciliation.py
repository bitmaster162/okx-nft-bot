from __future__ import annotations

from copy import copy
from functools import wraps
from typing import Any, Mapping
from urllib.parse import urlsplit

from eth_account.messages import encode_typed_data

from okx_nft_bot.clients.http import HTTPStatusError, build_url
from okx_nft_bot.clients.opensea import EIP712_TYPES, SEAPORT_ADDRESS_ETH


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_AMBIGUOUS_HTTP_STATUSES = {408, 409, 425, 429}


def _mirror_context() -> dict[str, Any] | None:
    from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT

    return _MIRROR_CONTEXT.get()


def _typed_message(parameters: Mapping[str, Any]) -> dict[str, Any]:
    def offer_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "itemType": int(item["itemType"]),
            "token": item["token"],
            "identifierOrCriteria": int(item["identifierOrCriteria"]),
            "startAmount": int(item["startAmount"]),
            "endAmount": int(item["endAmount"]),
        }

    def consideration_item(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "itemType": int(item["itemType"]),
            "token": item["token"],
            "identifierOrCriteria": int(item["identifierOrCriteria"]),
            "startAmount": int(item["startAmount"]),
            "endAmount": int(item["endAmount"]),
            "recipient": item["recipient"],
        }

    offer = parameters.get("offer")
    consideration = parameters.get("consideration")
    if not isinstance(offer, (list, tuple)) or not offer:
        raise RuntimeError("OpenSea order hash unavailable: offer items missing")
    if not isinstance(consideration, (list, tuple)) or not consideration:
        raise RuntimeError("OpenSea order hash unavailable: consideration items missing")

    return {
        "offerer": parameters["offerer"],
        "zone": parameters["zone"],
        "offer": [offer_item(item) for item in offer],
        "consideration": [consideration_item(item) for item in consideration],
        "orderType": int(parameters["orderType"]),
        "startTime": int(parameters["startTime"]),
        "endTime": int(parameters["endTime"]),
        "zoneHash": bytes.fromhex(str(parameters["zoneHash"]).replace("0x", "")),
        "salt": int(parameters["salt"]),
        "conduitKey": bytes.fromhex(str(parameters["conduitKey"]).replace("0x", "")),
        "counter": int(parameters["counter"]),
    }


def derive_seaport_order_hash(parameters: Mapping[str, Any]) -> str:
    """Derive Seaport's EIP-712 OrderComponents struct hash.

    Seaport's ``getOrderHash(OrderComponents)`` returns the EIP-712 struct hash
    that is then combined with the domain separator for signature verification.
    ``eth-account`` exposes that same message struct hash as ``SignableMessage.body``.
    """
    structured = {
        "types": EIP712_TYPES,
        "domain": {
            "name": "Seaport",
            "version": "1.6",
            "chainId": 1,
            "verifyingContract": SEAPORT_ADDRESS_ETH,
        },
        "primaryType": "OrderComponents",
        "message": _typed_message(parameters),
    }
    encoded = encode_typed_data(full_message=structured)
    body = bytes(encoded.body)
    if len(body) != 32:
        raise RuntimeError("OpenSea order hash derivation returned non-bytes32 body")
    return "0x" + body.hex()


class _SubmitObservationTransport:
    """Observe whether the actual OpenSea create-offer POST was attempted."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.submit_attempted = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def request_json(self, **kwargs: Any) -> dict[str, Any]:
        method = str(kwargs.get("method") or "").upper()
        url = str(kwargs.get("url") or "")
        try:
            path = urlsplit(url).path.rstrip("/").lower()
        except ValueError:
            path = ""
        if method == "POST" and path.endswith("/v2/orders/ethereum/seaport/offers"):
            self.submit_attempted = True
        return self._wrapped.request_json(**kwargs)


def _http_status_from_exception(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPStatusError):
            return int(current.status)
        current = current.__cause__ or current.__context__
    return None


def _ambiguous_submit_failure(exc: BaseException) -> bool:
    status = _http_status_from_exception(exc)
    if status is None:
        # Transport failure, response decode failure, or a successful HTTP
        # response rejected only because its durable receipt was incomplete.
        return True
    return status in _AMBIGUOUS_HTTP_STATUSES or status >= 500


def _get_order_url(client: Any, order_hash: str) -> str:
    base = str(getattr(client.settings, "opensea_api_base", "") or "").strip()
    if not base:
        raise RuntimeError("OpenSea API base unavailable for reconciliation")
    path = (
        "/api/v2/orders/chain/ethereum/protocol/"
        f"{SEAPORT_ADDRESS_ETH}/{order_hash}"
    )
    url, _ = build_url(base, path, {})
    return url


def _reconcile_order(client: Any, order_hash: str) -> dict[str, Any]:
    api_key = str(getattr(client.settings, "opensea_api_key", "") or "").strip()
    if not api_key:
        raise RuntimeError("OpenSea API key unavailable for reconciliation")

    response = client.transport.request_json(
        method="GET",
        url=_get_order_url(client, order_hash),
        headers={
            "Accept": "application/json",
            "X-API-KEY": api_key,
            "User-Agent": _USER_AGENT,
        },
    )
    if not isinstance(response, Mapping):
        raise RuntimeError("OpenSea reconciliation response is not an object")
    returned_hash = str(
        response.get("order_hash")
        or response.get("orderHash")
        or response.get("hash")
        or ""
    ).strip().lower()
    if returned_hash != order_hash.lower():
        raise RuntimeError(
            "OpenSea reconciliation order hash mismatch: "
            f"expected={order_hash} returned={returned_hash or '<missing>'}"
        )
    return dict(response)


def _uncertain_result(
    *,
    order_hash: str,
    submit_error: BaseException,
    reconciliation_error: BaseException,
) -> dict[str, Any]:
    return {
        "offer_id": order_hash,
        "order_id": order_hash,
        "order_hash": order_hash,
        "status": "submit_uncertain",
        "receipt_uncertain": True,
        "raw": {
            "order_hash": order_hash,
            "submit_error": str(submit_error)[:300],
            "reconciliation_error": str(reconciliation_error)[:300],
        },
    }


def _force_safe_uncertain(context: dict[str, Any], bidder: Any, state: Any | None) -> None:
    context["halted"] = True
    try:
        bidder.dry_run = True
    except Exception:
        pass
    try:
        bidder._r39_opensea_mirror_halted = True
    except Exception:
        pass
    if state is not None:
        try:
            state.set_force_dry_run(True, reason="opensea_mirror_receipt_uncertain")
        except Exception:
            pass


def install_opensea_receipt_reconciliation(
    bidder_class: type[Any],
    client_class: type[Any],
) -> None:
    """Reconcile or quarantine ambiguous CounterBidder OpenSea submit receipts."""
    current_submit = client_class._submit_opensea_offer
    current_create = client_class.create_opensea_offer
    current_record = bidder_class._record_execution_submit_event

    submit_installed = bool(
        getattr(current_submit, "_r43_opensea_receipt_reconciliation", False)
    )
    create_installed = bool(
        getattr(current_create, "_r43_opensea_receipt_context", False)
    )
    record_installed = bool(
        getattr(current_record, "_r43_opensea_uncertain_quarantine", False)
    )
    if submit_installed and create_installed and record_installed:
        return
    if any((submit_installed, create_installed, record_installed)):
        raise RuntimeError("partial R43 OpenSea receipt reconciliation installation detected")

    original_submit = current_submit
    original_create = current_create
    original_record = current_record

    @wraps(original_submit)
    def guarded_submit(
        self: Any,
        parameters: Mapping[str, Any],
        signature: str,
        chain: str = "eth",
    ) -> Any:
        if _mirror_context() is None:
            return original_submit(self, parameters, signature, chain)

        try:
            order_hash = derive_seaport_order_hash(parameters)
        except Exception as exc:
            raise RuntimeError(
                f"OpenSea receipt reconciliation hash derivation blocked: {exc}"
            ) from exc

        observed_client = copy(self)
        observer = _SubmitObservationTransport(self.transport)
        observed_client.transport = observer

        try:
            return original_submit(observed_client, parameters, signature, chain)
        except Exception as submit_exc:
            if not observer.submit_attempted or not _ambiguous_submit_failure(submit_exc):
                raise

            try:
                reconciled = _reconcile_order(observed_client, order_hash)
            except Exception as reconcile_exc:
                return _uncertain_result(
                    order_hash=order_hash,
                    submit_error=submit_exc,
                    reconciliation_error=reconcile_exc,
                )

            return {
                "offer_id": order_hash,
                "order_id": order_hash,
                "order_hash": order_hash,
                "status": "submitted",
                "reconciled": True,
                "raw": reconciled,
            }

    @wraps(original_create)
    def guarded_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_create(self, *args, **kwargs)
        context = _mirror_context()
        if (
            context is not None
            and isinstance(result, Mapping)
            and bool(result.get("receipt_uncertain"))
        ):
            order_hash = str(
                result.get("order_hash")
                or result.get("order_id")
                or result.get("offer_id")
                or ""
            ).strip()
            if not order_hash:
                raise RuntimeError("OpenSea uncertain receipt missing deterministic order hash")
            context["receipt_uncertain"] = True
            context["order_id"] = order_hash
            context["receipt_detail"] = dict(result.get("raw") or {})
        return result

    @wraps(original_record)
    def guarded_record(
        self: Any,
        *,
        chain: str,
        collection: str,
        price_bnb: float | None,
        status: str,
        reason: str,
    ) -> None:
        context = _mirror_context()
        is_opensea = context is not None and str(reason or "").lower().startswith("opensea")
        uncertain = bool(context and context.get("receipt_uncertain"))
        if not (is_opensea and uncertain and status == "submitted"):
            return original_record(
                self,
                chain=chain,
                collection=collection,
                price_bnb=price_bnb,
                status=status,
                reason=reason,
            )

        order_hash = str(context.get("order_id") or "").strip()
        normalized_bnb = context.get("price_bnb")
        normalized_usd = context.get("price_usd")
        if not order_hash or normalized_bnb is None:
            _force_safe_uncertain(context, self, None)
            raise RuntimeError(
                "OpenSea uncertain receipt missing durable quarantine data"
            )

        state = None
        try:
            state = self._get_execution_state()
        except Exception as exc:
            _force_safe_uncertain(context, self, None)
            raise RuntimeError(
                "OpenSea uncertain receipt could not access execution state: "
                f"{exc}"
            ) from exc

        _force_safe_uncertain(context, self, state)
        try:
            state.upsert_active_offer(
                order_hash=order_hash,
                collection=collection,
                chain=chain,
                price_bnb=float(normalized_bnb),
                status="killswitch_failed",
                preview_payload={
                    "marketplace": "opensea",
                    "source": "counter_bidder_mirror",
                    "receipt": "uncertain",
                },
            )
            state.record_submit_event(
                engine="counter_bidder",
                action_type="LIVE_OPENSEA_MIRROR",
                collection=collection,
                chain=chain,
                price_bnb=float(normalized_bnb),
                status="uncertain",
                reason=(
                    f"opensea submit receipt uncertain order_hash={order_hash};"
                    f"price_usd={float(normalized_usd):.8f}"
                    if normalized_usd is not None
                    else f"opensea submit receipt uncertain order_hash={order_hash}"
                ),
            )
        except Exception as exc:
            raise RuntimeError(
                "OpenSea uncertain receipt quarantine persistence failed after possible effect: "
                f"{exc}"
            ) from exc

        raise RuntimeError(
            "OpenSea mirror submit receipt uncertain; exposure quarantined "
            f"order_hash={order_hash}"
        )

    guarded_submit._r43_opensea_receipt_reconciliation = True
    guarded_create._r43_opensea_receipt_context = True
    guarded_record._r43_opensea_uncertain_quarantine = True
    client_class._submit_opensea_offer = guarded_submit
    client_class.create_opensea_offer = guarded_create
    bidder_class._record_execution_submit_event = guarded_record
