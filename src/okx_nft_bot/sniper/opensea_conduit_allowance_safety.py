from __future__ import annotations

import json
import os
from functools import wraps
from typing import Any, Mapping

from okx_nft_bot.clients.opensea import SEAPORT_ADDRESS_ETH
from okx_nft_bot.sniper.opensea_effect_boundary_safety import (
    _canonical_address,
    _mirror_context_active,
    _signed_effect_requirements,
)


_CONDUIT_CONTROLLER_ETH = "0x00000000f9490004c11cef243f5400493c00ad63"
_ZERO_BYTES32 = "0x" + ("00" * 32)
_GET_CONDUIT_SELECTOR = "0x6e9bfd9f"
_ERC20_ALLOWANCE_SELECTOR = "0xdd62ed3e"
_DEFAULT_ETH_RPC_URL = "https://eth.rpc.bloxroute.com/public"


def _canonical_bytes32(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise RuntimeError(f"{label} must be a 32-byte hex value")
    try:
        int(raw, 16)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be hexadecimal") from exc
    return "0x" + raw


def _rpc_result_hex(client: Any, *, to: str, data: str, label: str) -> str:
    rpc_url = os.getenv("ETH_RPC_URL", _DEFAULT_ETH_RPC_URL).strip()
    if not rpc_url:
        raise RuntimeError("ETH_RPC_URL unavailable")

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": _canonical_address(to, label=f"{label} target"), "data": data},
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
        raise RuntimeError(f"{label} RPC returned a non-object response")
    if "error" in decoded:
        raise RuntimeError(f"{label} RPC error: {decoded['error']}")
    result = decoded.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RuntimeError(f"{label} RPC result missing or malformed")
    raw = result[2:]
    if len(raw) % 2:
        raise RuntimeError(f"{label} RPC result has odd-length hex")
    try:
        int(raw or "0", 16)
    except ValueError as exc:
        raise RuntimeError(f"{label} RPC result is not valid hex") from exc
    return raw.lower()


def _resolve_conduit_spender(client: Any, *, conduit_key: Any) -> str:
    key = _canonical_bytes32(conduit_key, label="OpenSea conduitKey")
    if key == _ZERO_BYTES32:
        return _canonical_address(SEAPORT_ADDRESS_ETH, label="Seaport address")

    raw = _rpc_result_hex(
        client,
        to=_CONDUIT_CONTROLLER_ETH,
        data=_GET_CONDUIT_SELECTOR + key[2:],
        label="OpenSea ConduitController.getConduit",
    )
    if len(raw) != 128:
        raise RuntimeError("OpenSea getConduit RPC result must contain address and exists")

    address_word = raw[:64]
    exists_word = raw[64:]
    try:
        exists = int(exists_word, 16)
    except ValueError as exc:
        raise RuntimeError("OpenSea getConduit exists flag is invalid") from exc
    if exists not in {0, 1}:
        raise RuntimeError("OpenSea getConduit exists flag is non-boolean")
    if exists != 1:
        raise RuntimeError(f"OpenSea conduit is not deployed for key={key}")

    conduit = _canonical_address("0x" + address_word[-40:], label="OpenSea conduit")
    if int(conduit[2:], 16) == 0:
        raise RuntimeError("OpenSea getConduit returned zero address")
    return conduit


def _read_erc20_allowance_raw(
    client: Any,
    *,
    token: str,
    owner: str,
    spender: str,
) -> int:
    token_address = _canonical_address(token, label="offer token")
    owner_address = _canonical_address(owner, label="offerer")
    spender_address = _canonical_address(spender, label="allowance spender")
    calldata = (
        _ERC20_ALLOWANCE_SELECTOR
        + owner_address[2:].zfill(64)
        + spender_address[2:].zfill(64)
    )
    raw = _rpc_result_hex(
        client,
        to=token_address,
        data=calldata,
        label="OpenSea ERC20 allowance",
    )
    if len(raw) != 64:
        raise RuntimeError("OpenSea allowance RPC result must be one uint256 word")
    allowance = int(raw, 16)
    if allowance < 0:
        raise RuntimeError("OpenSea allowance RPC returned a negative allowance")
    return allowance


def _assert_fresh_allowance_state(
    client: Any,
    *,
    parameters: Mapping[str, Any] | None,
    chain: str,
) -> None:
    chain_name = str(chain or "").strip().lower()
    if chain_name not in {"eth", "ethereum", "1"}:
        raise RuntimeError(f"OpenSea allowance gate received unsupported chain {chain!r}")
    if not isinstance(parameters, Mapping):
        raise RuntimeError("OpenSea signed parameters unavailable")

    offerer, _, requirements = _signed_effect_requirements(parameters)
    spender = _resolve_conduit_spender(
        client,
        conduit_key=parameters.get("conduitKey"),
    )

    for token, required_raw in requirements.items():
        allowance_raw = _read_erc20_allowance_raw(
            client,
            token=token,
            owner=offerer,
            spender=spender,
        )
        if allowance_raw < required_raw:
            raise RuntimeError(
                "insufficient ERC20 allowance "
                f"token={token} spender={spender} required={required_raw} "
                f"allowance={allowance_raw}"
            )


def install_opensea_conduit_allowance_safety(client_class: type[Any]) -> None:
    """Fail closed when the signed OpenSea mirror cannot source ERC20 approvals."""
    current = client_class._submit_opensea_offer
    if getattr(current, "_r44_opensea_conduit_allowance_guard", False):
        return

    original = current

    @wraps(original)
    def guarded_submit(
        self: Any,
        parameters: Mapping[str, Any],
        signature: str,
        chain: str = "eth",
    ) -> Any:
        if not _mirror_context_active():
            return original(self, parameters, signature, chain)
        try:
            _assert_fresh_allowance_state(
                self,
                parameters=parameters,
                chain=chain,
            )
        except Exception as exc:
            raise RuntimeError(
                f"OpenSea live submit allowance gate blocked: {exc}"
            ) from exc
        return original(self, parameters, signature, chain)

    guarded_submit._r44_opensea_conduit_allowance_guard = True
    client_class._submit_opensea_offer = guarded_submit
