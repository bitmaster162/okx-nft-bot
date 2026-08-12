from __future__ import annotations

from functools import wraps
from typing import Any


class _ExplicitItemTokenZero:
    """Internal compatibility value for the legacy MassOfferEngine classifier.

    The legacy ``place_single_offer`` implementation treats both falsey values
    and the literal string ``"0"`` as collection scope before converting item
    token identifiers to ``int``.  R48/R49 established the opposite invariant:
    token #0 is a real item, while only explicit absence/collection aliases are
    collection scope.

    This value is deliberately truthy and stringifies to ``"00"`` so the
    legacy classifier takes its item branch, but ``int(value)`` is exactly zero.
    It never crosses the marketplace boundary: the original method normalizes
    it to the ordinary integer ``0`` before calling ``OKXAPIClient.create_offer``.
    """

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "00"

    def __int__(self) -> int:
        return 0


def _is_explicit_token_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return isinstance(value, str) and value == "0"


def install_mass_offer_token_scope_safety(engine_class: type[Any]) -> None:
    """Preserve token #0 as item scope at MassOfferEngine's public boundary.

    This is intentionally a narrow compatibility layer.  All non-zero and
    collection-scope inputs execute the pre-R54 method unchanged.  Literal
    integer/string zero is adapted only long enough to cross the legacy scope
    classifier, after which the original method converts it back to integer 0.
    """
    current = engine_class.place_single_offer
    if getattr(current, "_r54_mass_offer_token_zero_scope", False):
        return

    original = current

    @wraps(original)
    def guarded_place_single_offer(self: Any, *args: Any, **kwargs: Any) -> Any:
        if args:
            # place_single_offer is keyword-only in the supported API. Preserve
            # the original TypeError contract instead of guessing positional
            # argument meaning.
            return original(self, *args, **kwargs)

        token_id = kwargs.get("token_id")
        if not _is_explicit_token_zero(token_id):
            return original(self, **kwargs)

        adapted = dict(kwargs)
        adapted["token_id"] = _ExplicitItemTokenZero()
        return original(self, **adapted)

    guarded_place_single_offer._r54_mass_offer_token_zero_scope = True
    engine_class.place_single_offer = guarded_place_single_offer
