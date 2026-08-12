from __future__ import annotations

import json
import os
from functools import wraps
from typing import Any, Mapping


_DEFAULT_ETH_RPC_URL = "https://eth.rpc.bloxroute.com/public"


def _canonical_address(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 40:
        raise RuntimeError(f"{label} must be a 20-byte address")
    try:
        int(raw, 16)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be hexadecimal") from exc
    return "0x" + raw


def _signed_effect_requirements(
    parameters: Mapping[str, Any] | None,
) -> tuple[str, int, dict[str, int]]:
    """Extract offerer, signed counter, and raw ERC20 exposure from an offer."""
    if not isinstance(parameters, Mapping):
        raise RuntimeError("OpenSea signed parameters unavailable")

    offerer = _canonical_address(parameters.get("offerer"), label="offerer")
    try:
        signed_counter = int(parameters.get("counter"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea signed counter is invalid") from exc
    if signed_counter < 0:
        raise RuntimeError("OpenSea signed counter must be non-negative")

    offer = parameters.get("offer")
    if not isinstance(offer, (list, tuple)) or not offer:
        raise RuntimeError("OpenSea signed offer items unavailable")

    requirements: dict[str, int] = {}
    for item in offer:
        if not isinstance(item, Mapping):
            raise RuntimeError("OpenSea signed offer item is invalid")
        try:
            item_type = int(item.get("itemType"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenSea signed offer itemType is invalid") from exc
        if item_type != 1:
            raise RuntimeError("OpenSea BUY offer side must contain only ERC20 items")

        token = _canonical_address(item.get("token"), label="offer token")
        try:
            start_amount = int(item.get("startAmount"))
            end_amount = int(item.get("endAmount"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenSea signed ERC20 amount is invalid") from exc
        required = max(start_amount, end_amount)
        if required <= 0:
            raise RuntimeError("OpenSea signed ERC20 amount must be positive")
        requirements[token] = requirements.get(token, 0) + required

    if not requirements:
        raise RuntimeError("OpenSea signed ERC20 requirements unavailable")
    return offerer, signed_counter, requirements


def _read_erc20_balance_raw(
    client: Any,
    *,
    token: str,
    wallet: str,
) -> int:
    """Read Ethereum ERC20 balanceOf in raw token units; uncertainty is failure."""
    token_address = _canonical_address(token, label="offer token")
    wallet_address = _canonical_address(wallet, label="offerer")
    calldata = "0x70a08231" + wallet_address[2:].zfill(64)
    rpc_url = os.getenv("ETH_RPC_URL", _DEFAULT_ETH_RPC_URL).strip()
    if not rpc_url:
        raise RuntimeError("ETH_RPC_URL unavailable")

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": token_address, "data": calldata},
            "latest",
        ],
        "id": 1,
    }
    decoded = client.transport.request_json(
        method="POST",
        url=rpc_url,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body=json.dumps(payload),
    )
    if not isinstance(decoded, Mapping):
        raise RuntimeError("OpenSea balance RPC returned a non-object response")
    if "error" in decoded:
        raise RuntimeError(f"OpenSea balance RPC error: {decoded['error']}")
    raw_balance = decoded.get("result")
    if not isinstance(raw_balance, str) or not raw_balance.startswith("0x"):
        raise RuntimeError("OpenSea balance RPC result missing or malformed")
    try:
        balance = int(raw_balance, 16)
    except ValueError as exc:
        raise RuntimeError("OpenSea balance RPC result is not valid hex") from exc
    if balance < 0:
        raise RuntimeError("OpenSea balance RPC returned a negative balance")
    return balance


def _assert_fresh_effect_state(
    client: Any,
    *,
    parameters: Mapping[str, Any] | None,
    chain: str,
) -> None:
    chain_name = str(chain or "").strip().lower()
    if chain_name not in {"eth", "ethereum", "1"}:
        raise RuntimeError(f"OpenSea effect gate received unsupported chain {chain!r}")

    offerer, signed_counter, requirements = _signed_effect_requirements(parameters)
    current_counter = int(client.get_seaport_counter(offerer, chain_name))
    if current_counter != signed_counter:
        raise RuntimeError(
            "stale Seaport counter "
            f"offerer={offerer} signed={signed_counter} on_chain={current_counter}"
        )

    for token, required_raw in requirements.items():
        balance_raw = _read_erc20_balance_raw(
            client,
            token=token,
            wallet=offerer,
        )
        if balance_raw < required_raw:
            raise RuntimeError(
                "insufficient ERC20 balance "
                f"token={token} required={required_raw} available={balance_raw}"
            )


def _mirror_context_active() -> bool:
    """Limit R42 to the CounterBidder OpenSea mirror path it hardens."""
    from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT

    return _MIRROR_CONTEXT.get() is not None


def install_opensea_effect_boundary_safety(client_class: type[Any]) -> None:
    """Fail closed on stale counter or balance at the OpenSea mirror boundary."""
    current = client_class._submit_opensea_offer
    if getattr(current, "_r42_opensea_effect_boundary_guard", False):
        return

    original = current

    @wraps(original)
    def guarded_submit(
        self: Any,
        parameters: Mapping[str, Any],
        signature: str,
        chain: str = "eth",
    ) -> Any:
        # Existing direct client helpers/tests are outside the R42 scope. The
        # production CounterBidder mirror owns a ContextVar across build/sign/POST,
        # allowing this final boundary to be strict without changing unrelated
        # OpenSea client call contracts.
        if not _mirror_context_active():
            return original(self, parameters, signature, chain)
        try:
            _assert_fresh_effect_state(
                self,
                parameters=parameters,
                chain=chain,
            )
        except Exception as exc:
            raise RuntimeError(
                f"OpenSea live submit effect gate blocked: {exc}"
            ) from exc
        return original(self, parameters, signature, chain)

    guarded_submit._r42_opensea_effect_boundary_guard = True
    client_class._submit_opensea_offer = guarded_submit
