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


def _canonical_address(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 40:
        raise ValueError(f"{label} must be a 20-byte address")
    try:
        int(raw, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
    return "0x" + raw


def _item_type(item: Mapping[str, Any]) -> int | None:
    try:
        return int(item.get("itemType"))
    except (TypeError, ValueError):
        return None


def _submit_item_parameters(advanced: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Extract signed Seaport parameters from an OKX submitOrder item.

    Repository callers/tests historically used ``items[].parameters`` directly,
    while the real two-step OKX flow writes the signed message into
    ``items[].protocolData`` as JSON just before the final POST. Supporting both
    shapes keeps the guard compatible while ensuring the production payload is
    actually inspected.
    """
    direct = advanced.get("parameters")
    if isinstance(direct, Mapping):
        return direct

    if "protocolData" not in advanced:
        return None

    protocol_data = advanced.get("protocolData")
    if isinstance(protocol_data, str):
        import json

        try:
            decoded = json.loads(protocol_data)
        except (TypeError, ValueError) as exc:
            raise ValueError("submit item protocolData is invalid JSON") from exc
    elif isinstance(protocol_data, Mapping):
        decoded = protocol_data
    else:
        raise ValueError("submit item protocolData has invalid type")

    if not isinstance(decoded, Mapping):
        raise ValueError("submit item protocolData must decode to an object")
    parameters = decoded.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("submit item protocolData missing Seaport parameters")
    return parameters


def _buy_erc20_requirements(
    payload: Mapping[str, Any] | None,
) -> tuple[str, dict[str, int]] | None:
    """Return wallet and raw ERC20 requirements for BUY-side Seaport orders.

    A bid/offer puts ERC20 on the Seaport ``offer`` side and receives an NFT in
    ``consideration``. Listings put the NFT on the offer side and are therefore
    deliberately outside this balance gate.
    """
    if payload is None:
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, (list, tuple)):
        return None

    payload_wallet = payload.get("walletAddress")
    wallet: str | None = None
    requirements: dict[str, int] = {}
    buy_order_seen = False

    for advanced in raw_items:
        if not isinstance(advanced, Mapping):
            continue
        parameters = _submit_item_parameters(advanced)
        if parameters is None:
            continue

        consideration = parameters.get("consideration")
        offer = parameters.get("offer")
        if not isinstance(consideration, (list, tuple)):
            continue

        has_nft_consideration = any(
            isinstance(item, Mapping) and _item_type(item) in _NFT_ITEM_TYPES
            for item in consideration
        )
        if not has_nft_consideration:
            continue

        buy_order_seen = True
        if not isinstance(offer, (list, tuple)) or not offer:
            raise ValueError("buy order missing ERC20 offer items")

        offerer = _canonical_address(parameters.get("offerer"), label="offerer")
        order_wallet = _canonical_address(
            payload_wallet or offerer,
            label="walletAddress",
        )
        if offerer != order_wallet:
            raise ValueError("walletAddress does not match Seaport offerer")
        if wallet is None:
            wallet = order_wallet
        elif wallet != order_wallet:
            raise ValueError("submit batch contains multiple offerer wallets")

        for offer_item in offer:
            if not isinstance(offer_item, Mapping) or _item_type(offer_item) != 1:
                raise ValueError("buy order offer side must contain only ERC20 items")
            token = _canonical_address(offer_item.get("token"), label="offer token")
            try:
                start_amount = int(offer_item.get("startAmount"))
                end_amount = int(offer_item.get("endAmount"))
            except (TypeError, ValueError) as exc:
                raise ValueError("buy order ERC20 amount is invalid") from exc
            required = max(start_amount, end_amount)
            if required <= 0:
                raise ValueError("buy order ERC20 amount must be positive")
            requirements[token] = requirements.get(token, 0) + required

    if not buy_order_seen:
        return None
    if wallet is None or not requirements:
        raise ValueError("buy order balance requirements unavailable")
    return wallet, requirements


def _read_erc20_balance_raw(
    client: Any,
    *,
    chain_name: str,
    token: str,
    wallet: str,
) -> int:
    """Read ERC20 balanceOf in raw token units; any uncertainty is an error."""
    import json
    import urllib.request

    if chain_name not in {"bsc", "eth"}:
        raise RuntimeError(f"balance RPC unsupported for chain {chain_name!r}")

    rpc_url = client._primary_rpc(chain_name)
    if not rpc_url:
        raise RuntimeError(f"no RPC configured for chain {chain_name}")

    token_address = _canonical_address(token, label="offer token")
    wallet_address = _canonical_address(wallet, label="walletAddress")
    calldata = "0x70a08231" + wallet_address[2:].zfill(64)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": token_address, "data": calldata},
                "latest",
            ],
            "id": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        decoded = json.loads(response.read())

    if not isinstance(decoded, Mapping):
        raise RuntimeError("balance RPC returned a non-object response")
    if "error" in decoded:
        raise RuntimeError(f"balance RPC error: {decoded['error']}")
    raw_balance = decoded.get("result")
    if not isinstance(raw_balance, str) or not raw_balance.startswith("0x"):
        raise RuntimeError("balance RPC result missing or malformed")
    try:
        balance = int(raw_balance, 16)
    except ValueError as exc:
        raise RuntimeError("balance RPC result is not valid hex") from exc
    if balance < 0:
        raise RuntimeError("balance RPC returned a negative balance")
    return balance


def _read_bsc_active_offer_count(client: Any) -> int:
    """Read the wallet-wide local active-offer count used by the BSC quota guard."""
    from okx_nft_bot.undercutter.state import PositionState

    db_path = getattr(client.settings, "execution_db_path", None)
    if db_path is None:
        raise RuntimeError("execution_db_path unavailable")
    state = PositionState(db_path)
    return state.count_active_offers()


def _bsc_quota_block_reason(client: Any) -> str | None:
    """Fail closed when the BSC OKX active-offer quota cannot be proven safe."""
    import os

    raw_threshold = os.getenv("COUNTERBID_QUOTA_THRESHOLD", "25")
    try:
        threshold = int(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"invalid COUNTERBID_QUOTA_THRESHOLD={raw_threshold!r}"
        ) from exc
    if threshold <= 0:
        raise RuntimeError(
            f"invalid COUNTERBID_QUOTA_THRESHOLD={threshold}"
        )

    active_count = _read_bsc_active_offer_count(client)
    if active_count < 0:
        raise RuntimeError("active offer count is negative")
    if active_count >= threshold:
        return f"quota_guard:{active_count}/{threshold}"
    return None


def install_submit_safety(client_class: type[Any]) -> None:
    """Gate OKX Seaport submitOrder at the final HTTP effect boundary."""
    original_request = client_class._request
    if getattr(original_request, "_r22_protocoldata_guard", False):
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

            # R20/R22: reconstruct the actual signed BUY-side ERC20 requirement
            # from either direct parameters or the protocolData JSON emitted by
            # the real OKX two-step flow. No upstream cache/price conversion is
            # trusted at this final effect boundary.
            try:
                balance_gate = _buy_erc20_requirements(payload)
            except Exception as exc:
                raise OKXSubmitError(
                    f"submitOrder balance gate blocked: {exc}"
                ) from exc

            if balance_gate is not None:
                # R21: preserve the CounterBidder BSC exchange-drift quota at
                # the final effect boundary. The legacy upstream guard allowed
                # submissions when SQLite could not be read; uncertainty now
                # blocks instead. The original guard is BSC-only and counts all
                # locally active offers, so keep those semantics here.
                if chain_name == "bsc":
                    try:
                        quota_block = _bsc_quota_block_reason(self)
                    except Exception as exc:
                        raise OKXSubmitError(
                            f"submitOrder quota gate blocked: {exc}"
                        ) from exc
                    if quota_block:
                        raise OKXSubmitError(
                            f"submitOrder quota gate blocked: {quota_block}"
                        )

                try:
                    wallet, requirements = balance_gate
                    for token_address, required_raw in requirements.items():
                        balance_raw = _read_erc20_balance_raw(
                            self,
                            chain_name=chain_name,
                            token=token_address,
                            wallet=wallet,
                        )
                        if balance_raw < required_raw:
                            raise RuntimeError(
                                "insufficient ERC20 balance "
                                f"token={token_address} required={required_raw} "
                                f"available={balance_raw}"
                            )
                except OKXSubmitError:
                    raise
                except Exception as exc:
                    raise OKXSubmitError(
                        f"submitOrder balance gate blocked: {exc}"
                    ) from exc

        return original_request(
            self,
            method=method,
            path=path,
            params=params,
            payload=payload,
        )

    guarded_request._r16_submit_guard = True
    guarded_request._r20_balance_guard = True
    guarded_request._r21_quota_guard = True
    guarded_request._r22_protocoldata_guard = True
    guarded_complete_two_step_offer._r16_submit_context = True

    client_class._request = guarded_request
    client_class._complete_two_step_offer = guarded_complete_two_step_offer
