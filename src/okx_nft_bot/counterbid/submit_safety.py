from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Mapping

_SUBMIT_CHAIN: ContextVar[str | None] = ContextVar(
    "okx_submit_guard_chain",
    default=None,
)

_CHAIN_BY_ID = {
    1: "eth",
    56: "bsc",
    137: "polygon",
    42161: "arbitrum",
}


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


def install_submit_safety(client_class: type[Any]) -> None:
    """Gate OKX Seaport submitOrder at the final HTTP effect boundary."""
    original_request = client_class._request
    if getattr(original_request, "_r16_submit_guard", False):
        return

    original_complete = client_class._complete_two_step_offer

    def guarded_complete_two_step_offer(
        self: Any,
        step1_resp: dict[str, Any],
        private_key: str,
        chain_id: int,
        endpoint: str | None,
    ) -> dict[str, Any]:
        # The step2 POST body is supplied by OKX and is not guaranteed to carry
        # a chain field. Keep chain context local to this call so concurrent
        # two-step flows cannot overwrite one another.
        token = _SUBMIT_CHAIN.set(_chain_name(chain_id))
        try:
            return original_complete(
                self,
                step1_resp,
                private_key,
                chain_id,
                endpoint,
            )
        finally:
            _SUBMIT_CHAIN.reset(token)

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

        if is_submit:
            payload_chain = payload.get("chain") if payload is not None else None
            chain_name = _chain_name(payload_chain) or _SUBMIT_CHAIN.get()
            if chain_name is None:
                from okx_nft_bot.counterbid.okx_api import OKXSubmitError

                raise OKXSubmitError(
                    "submitOrder live gate blocked: chain context unavailable"
                )

            from okx_nft_bot.counterbid.okx_api import OKXSubmitError
            from okx_nft_bot.execution_governor import ExecutionGovernor

            governor = ExecutionGovernor(
                settings=self.settings,
                api_client=self,
            )
            blocked = governor.check_live_submit_allowed(
                action_type="LIVE_OKX_SUBMIT_ORDER",
                collection="okx_submit_order",
                chain=chain_name,
                price_bnb=0.0,
            )
            if blocked:
                raise OKXSubmitError(
                    f"submitOrder live gate blocked: {blocked}"
                )

        return original_request(
            self,
            method=method,
            path=path,
            params=params,
            payload=payload,
        )

    guarded_request._r16_submit_guard = True
    guarded_complete_two_step_offer._r16_submit_context = True

    client_class._request = guarded_request
    client_class._complete_two_step_offer = guarded_complete_two_step_offer
