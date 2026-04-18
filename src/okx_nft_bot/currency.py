from __future__ import annotations

from typing import Final

BSC_USDT_ADDRESS: Final[str] = "0x55d398326f99059ff775485246999027b3197955"
BSC_BUSD_ADDRESS: Final[str] = "0xe9e7cea3dedca5984780bafc599bd69add087d56"

_SYMBOL_ALIASES: Final[dict[str, str]] = {
    "BUSD": "USDT",
}

_ADDRESS_ALIASES: Final[dict[str, str]] = {
    BSC_BUSD_ADDRESS: BSC_USDT_ADDRESS,
}

_ADDRESS_TO_SYMBOL: Final[dict[str, str]] = {
    BSC_USDT_ADDRESS: "USDT",
    BSC_BUSD_ADDRESS: "USDT",
}


def canonical_currency(value: str | None, *, chain: str | None = None) -> str | None:
    del chain  # reserved for future chain-specific aliases
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.lower().startswith("0x"):
        if len(text) == 42:
            lowered = text.lower()
            return _ADDRESS_ALIASES.get(lowered, lowered)
        return text

    return _SYMBOL_ALIASES.get(text.upper(), text.upper())


def canonical_currency_symbol(
    value: str | None,
    *,
    chain: str | None = None,
    default: str | None = None,
) -> str | None:
    canonical = canonical_currency(value, chain=chain)
    if canonical is None:
        return default

    if canonical.startswith("0x"):
        return _ADDRESS_TO_SYMBOL.get(canonical.lower(), default or canonical)

    return canonical
