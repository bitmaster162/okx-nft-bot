from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import json
import logging
from typing import Any, Mapping

from eth_account.messages import encode_typed_data

from okx_nft_bot.clients.http import HTTPStatusError
from okx_nft_bot.signing.seaport_signer import EIP712_TYPES, _to_typed_data_message


log = logging.getLogger("counterbid.okx_receipt_reconciliation")

_RECONCILE_CHAIN: ContextVar[str | None] = ContextVar(
    "okx_submit_reconcile_chain",
    default=None,
)
_RECONCILE_PARAMETERS: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "okx_submit_reconcile_parameters",
    default=None,
)

_CHAIN_BY_ID = {
    1: "eth",
    56: "bsc",
    137: "polygon",
    42161: "arbitrum",
}
_PLACEHOLDER_IDS = frozenset({"", "?", "pending", "none", "null"})
_NFT_ITEM_TYPES = frozenset({2, 3, 4, 5})


def _chain_name(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"eth", "bsc", "polygon", "arbitrum"}:
            return normalized
        if normalized.isdigit():
            return _CHAIN_BY_ID.get(int(normalized))
        return None
    try:
        return _CHAIN_BY_ID.get(int(value))
    except (TypeError, ValueError):
        return None


def _durable_id(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if candidate.lower() in _PLACEHOLDER_IDS:
        return None
    return candidate


def _payload_order_parameters(
    payload: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Extract full Seaport OrderComponents from an OKX submit payload.

    Two-step OKX submissions carry the signed components in
    ``items[].protocolData.parameters``. Direct ``submit_seaport_order`` payloads
    contain ``items[].parameters`` without the Seaport counter, so those use the
    explicit context captured by the public method wrapper instead.
    """
    if payload is not None:
        raw_items = payload.get("items")
        if isinstance(raw_items, (list, tuple)):
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                protocol_data = item.get("protocolData")
                if isinstance(protocol_data, str):
                    try:
                        protocol_data = json.loads(protocol_data)
                    except (TypeError, ValueError):
                        protocol_data = None
                if isinstance(protocol_data, Mapping):
                    parameters = protocol_data.get("parameters")
                    if isinstance(parameters, Mapping) and parameters.get("counter") is not None:
                        return parameters

                parameters = item.get("parameters")
                if isinstance(parameters, Mapping) and parameters.get("counter") is not None:
                    return parameters

    contextual = _RECONCILE_PARAMETERS.get()
    return contextual if isinstance(contextual, Mapping) else None


def _offer_collection(parameters: Mapping[str, Any]) -> str | None:
    """Return the NFT collection only for a BUY-side Seaport offer shape."""
    offer = parameters.get("offer")
    consideration = parameters.get("consideration")
    if not isinstance(offer, (list, tuple)) or not offer:
        return None
    if not isinstance(consideration, (list, tuple)) or not consideration:
        return None

    # An OKX bid/offer spends ERC20 and receives an NFT. Listings are the
    # inverse shape and must never be reconciled through the active-offer API.
    for item in offer:
        if not isinstance(item, Mapping):
            return None
        try:
            if int(item.get("itemType")) != 1:
                return None
        except (TypeError, ValueError):
            return None

    nft_tokens: set[str] = set()
    for item in consideration:
        if not isinstance(item, Mapping):
            continue
        try:
            item_type = int(item.get("itemType"))
        except (TypeError, ValueError):
            continue
        if item_type not in _NFT_ITEM_TYPES:
            continue
        token = str(item.get("token") or "").strip().lower()
        if token:
            nft_tokens.add(token)

    if len(nft_tokens) != 1:
        return None
    return next(iter(nft_tokens))


def _seaport_order_hash(parameters: Mapping[str, Any]) -> str:
    """Derive Seaport ``getOrderHash(OrderComponents)`` from signed parameters.

    EIP-712 encodes the OrderComponents struct hash as the SignableMessage body;
    the domain separator is deliberately excluded from Seaport's order hash.
    A repository regression vector pins this helper to the orderHash published
    in OKX's official Query Offer API example.
    """
    message_types = {
        key: value
        for key, value in EIP712_TYPES.items()
        if key != "EIP712Domain"
    }
    message = _to_typed_data_message(dict(parameters))
    encoded = encode_typed_data(
        domain_data={},
        message_types=message_types,
        message_data=message,
    )
    return "0x" + bytes(encoded.body).hex()


def _is_ambiguous_submit_error(exc: Exception) -> bool:
    from okx_nft_bot.counterbid.okx_api import (
        OKXNetworkError,
        OKXRateLimitError,
        OKXSubmitError,
    )

    if isinstance(exc, (OKXRateLimitError, OKXNetworkError)):
        return True
    if not isinstance(exc, OKXSubmitError):
        return False

    cause = exc.__cause__
    if isinstance(cause, HTTPStatusError) and cause.status >= 500:
        return True

    return str(exc).startswith(
        "submitOrder receipt gate blocked: success response missing durable order id"
    )


def _reconcile_submit(
    client: Any,
    *,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    parameters = _payload_order_parameters(payload)
    if parameters is None:
        return None

    collection = _offer_collection(parameters)
    if collection is None:
        return None

    chain = _RECONCILE_CHAIN.get()
    if chain is None and payload is not None:
        chain = _chain_name(payload.get("chain"))
    if chain is None:
        return None

    offerer = str(parameters.get("offerer") or "").strip().lower()
    if not offerer:
        return None

    try:
        expected_hash = _seaport_order_hash(parameters).lower()
    except Exception as exc:
        log.warning("submitOrder reconciliation hash derivation failed: %s", exc)
        return None

    try:
        records = client.get_my_offers(
            chain=chain,
            collection_address=collection,
            require_all_endpoints=True,
        )
    except Exception as exc:
        log.warning(
            "submitOrder reconciliation inventory read failed hash=%s: %s",
            expected_hash,
            exc,
        )
        return None

    for record in records:
        if not isinstance(record, Mapping):
            continue
        record_hash = str(record.get("orderHash") or "").strip().lower()
        if record_hash != expected_hash:
            continue

        maker = str(record.get("maker") or record.get("makerAddress") or "").strip().lower()
        if not maker or maker != offerer:
            continue

        status = str(record.get("status") or "active").strip().lower()
        if status != "active":
            continue

        order_id = _durable_id(record.get("orderId"))
        if order_id is None:
            log.warning(
                "submitOrder reconciliation found exact hash without durable orderId: %s",
                expected_hash,
            )
            return None

        log.warning(
            "submitOrder receipt reconciled by exact active orderHash=%s orderId=%s",
            expected_hash,
            order_id,
        )
        return {
            "code": "0",
            "msg": "",
            "data": {
                "successOrderIds": [order_id],
                "errors": [],
                "reconciledOrderHash": expected_hash,
            },
            "r53_reconciled": True,
        }

    return None


def install_receipt_reconciliation(client_class: type[Any]) -> None:
    """Reconcile ambiguous OKX offer submits without repeating the POST effect."""
    current_request = client_class._request
    current_complete = client_class._complete_two_step_offer
    current_direct = client_class.submit_seaport_order

    request_installed = bool(
        getattr(current_request, "_r53_receipt_reconcile_guard", False)
    )
    complete_installed = bool(
        getattr(current_complete, "_r53_receipt_reconcile_chain", False)
    )
    direct_installed = bool(
        getattr(current_direct, "_r53_receipt_reconcile_parameters", False)
    )
    if request_installed and complete_installed and direct_installed:
        return
    if request_installed or complete_installed or direct_installed:
        raise RuntimeError("partial R53 receipt reconciliation installation detected")

    original_request = current_request
    original_complete = current_complete
    original_direct = current_direct

    @wraps(original_complete)
    def guarded_complete_two_step_offer(
        self: Any,
        step1_resp: dict[str, Any],
        private_key: str,
        chain_id: int,
        endpoint: str | None,
    ) -> dict[str, Any]:
        token = _RECONCILE_CHAIN.set(_chain_name(chain_id))
        try:
            return original_complete(
                self,
                step1_resp,
                private_key,
                chain_id,
                endpoint,
            )
        finally:
            _RECONCILE_CHAIN.reset(token)

    @wraps(original_direct)
    def guarded_submit_seaport_order(
        self: Any,
        *,
        chain: str,
        wallet_address: str,
        parameters: dict[str, Any] | Any,
        signature: str,
    ) -> dict[str, Any]:
        chain_token = _RECONCILE_CHAIN.set(_chain_name(chain))
        params_token = _RECONCILE_PARAMETERS.set(
            parameters if isinstance(parameters, Mapping) else None
        )
        try:
            return original_direct(
                self,
                chain=chain,
                wallet_address=wallet_address,
                parameters=parameters,
                signature=signature,
            )
        finally:
            _RECONCILE_PARAMETERS.reset(params_token)
            _RECONCILE_CHAIN.reset(chain_token)

    @wraps(original_request)
    def guarded_request(
        self: Any,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        canonical_path = str(path or "").split("?", 1)[0]
        is_submit = (
            str(method or "").upper() == "POST"
            and canonical_path == self._SUBMIT_ORDER_PATH
        )
        if not is_submit:
            return original_request(
                self,
                method=method,
                path=path,
                params=params,
                payload=payload,
            )

        try:
            return original_request(
                self,
                method=method,
                path=path,
                params=params,
                payload=payload,
            )
        except Exception as exc:
            if not _is_ambiguous_submit_error(exc):
                raise
            reconciled = _reconcile_submit(self, payload=payload)
            if reconciled is not None:
                return reconciled
            raise

    guarded_request._r53_receipt_reconcile_guard = True
    guarded_complete_two_step_offer._r53_receipt_reconcile_chain = True
    guarded_submit_seaport_order._r53_receipt_reconcile_parameters = True

    client_class._request = guarded_request
    client_class._complete_two_step_offer = guarded_complete_two_step_offer
    client_class.submit_seaport_order = guarded_submit_seaport_order
