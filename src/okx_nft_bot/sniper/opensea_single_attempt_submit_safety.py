from __future__ import annotations

from copy import copy
from functools import wraps
from typing import Any, Mapping
from urllib.parse import urlsplit

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.sniper.opensea_effect_boundary_safety import _mirror_context_active


_TARGET_SUBMIT_SUFFIX = "/v2/orders/ethereum/seaport/offers"


def _is_opensea_create_offer_post(kwargs: Mapping[str, Any]) -> bool:
    method = str(kwargs.get("method") or "").upper()
    url = str(kwargs.get("url") or "")
    try:
        path = urlsplit(url).path.rstrip("/").lower()
    except ValueError:
        return False
    return method == "POST" and path.endswith(_TARGET_SUBMIT_SUFFIX)


def _stdlib_single_attempt(transport: StdlibHttpTransport, **kwargs: Any) -> dict[str, Any]:
    """Reuse the live session/limiter but cap this one request to one attempt."""
    isolated = copy(transport)
    isolated.max_retries = 1
    return isolated.request_json(**kwargs)


def _request_target_once(transport: Any, **kwargs: Any) -> dict[str, Any]:
    """Issue one production transport attempt while preserving R43 observation.

    R43 wraps the client transport in a small observer before calling the inner
    submit chain. For the production Stdlib transport we bypass its retry loop
    by cloning only the transport object and setting max_retries=1. The clone
    shares the already-created curl session and rate limiter, so no new runtime
    connection pool or limiter is introduced.

    Custom/injected transports keep their prior contract. They are invoked once
    by this adapter, but R45 does not assume or rewrite their internal policy.
    """
    if isinstance(transport, StdlibHttpTransport):
        return _stdlib_single_attempt(transport, **kwargs)

    nested = getattr(transport, "_wrapped", None)
    if nested is not None and hasattr(transport, "submit_attempted"):
        # This is the R43 submit observer. Mark the target boundary before the
        # effect, then descend to the wrapped production transport so R43 can
        # reconcile a lost/ambiguous receipt after the single attempt fails.
        try:
            transport.submit_attempted = True
        except Exception as exc:
            raise RuntimeError(
                "OpenSea single-attempt guard could not arm receipt observation"
            ) from exc
        return _request_target_once(nested, **kwargs)

    return transport.request_json(**kwargs)


class _SingleAttemptOpenSeaTransport:
    """Narrow transport adapter: only OpenSea create-offer POST loses retries."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def request_json(self, **kwargs: Any) -> dict[str, Any]:
        if _is_opensea_create_offer_post(kwargs):
            return _request_target_once(self._wrapped, **kwargs)
        return self._wrapped.request_json(**kwargs)


def install_opensea_single_attempt_submit_safety(client_class: type[Any]) -> None:
    """Prevent blind HTTP retries of effectful CounterBidder OpenSea submits."""
    current = client_class._submit_opensea_offer
    if getattr(current, "_r45_opensea_single_attempt_submit", False):
        return

    original = current

    @wraps(original)
    def guarded_submit(
        self: Any,
        parameters: Mapping[str, Any],
        signature: str,
        chain: str = "eth",
    ) -> Any:
        # Preserve the direct OpenSea client contract. R45 hardens the production
        # CounterBidder mirror path whose ambiguity is already reconciled by R43.
        if not _mirror_context_active():
            return original(self, parameters, signature, chain)

        guarded_client = copy(self)
        guarded_client.transport = _SingleAttemptOpenSeaTransport(self.transport)
        return original(guarded_client, parameters, signature, chain)

    guarded_submit._r45_opensea_single_attempt_submit = True
    client_class._submit_opensea_offer = guarded_submit
