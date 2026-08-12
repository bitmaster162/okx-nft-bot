from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Mapping


_BLASTER_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "offer_blaster_eth_accounting_context",
    default=None,
)
_PLACEHOLDER_RECEIPTS = frozenset({"", "?", "pending", "none", "null"})


def _durable_offer_id(result: Any) -> str:
    if not isinstance(result, Mapping):
        raise RuntimeError("OKX submit result is not an object")
    raw = result.get("offer_id") or result.get("order_id") or result.get("order_hash")
    offer_id = str(raw or "").strip()
    if offer_id.lower() in _PLACEHOLDER_RECEIPTS:
        raise RuntimeError("durable OKX offer receipt unavailable")
    return offer_id


def _eth_buy_requirements(payload: Mapping[str, Any]) -> dict[str, int]:
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise RuntimeError("OfferBlaster submit payload missing Seaport parameters")
    offer = parameters.get("offer")
    if not isinstance(offer, (list, tuple)) or not offer:
        raise RuntimeError("OfferBlaster submit payload missing ERC20 offer items")

    requirements: dict[str, int] = {}
    for item in offer:
        if not isinstance(item, Mapping):
            raise RuntimeError("OfferBlaster ERC20 offer item is malformed")
        try:
            item_type = int(item.get("itemType"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OfferBlaster ERC20 itemType is invalid") from exc
        if item_type != 1:
            raise RuntimeError("OfferBlaster ETH offer side must contain only ERC20 items")
        token = str(item.get("token") or "").strip().lower()
        if not token:
            raise RuntimeError("OfferBlaster ERC20 token address unavailable")
        try:
            start_amount = int(item.get("startAmount"))
            end_amount = int(item.get("endAmount"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OfferBlaster ERC20 amount is invalid") from exc
        required = max(start_amount, end_amount)
        if required <= 0:
            raise RuntimeError("OfferBlaster ERC20 amount must be positive")
        requirements[token] = requirements.get(token, 0) + required
    return requirements


def _collection_from_payload(payload: Mapping[str, Any]) -> str:
    direct = str(payload.get("collection") or "").strip().lower()
    if direct:
        return direct
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise RuntimeError("OfferBlaster collection unavailable")
    consideration = parameters.get("consideration")
    if not isinstance(consideration, (list, tuple)):
        raise RuntimeError("OfferBlaster collection unavailable")
    for item in consideration:
        if not isinstance(item, Mapping):
            continue
        try:
            item_type = int(item.get("itemType"))
        except (TypeError, ValueError):
            continue
        if item_type not in {2, 3, 4, 5}:
            continue
        token = str(item.get("token") or "").strip().lower()
        if token:
            return token
    raise RuntimeError("OfferBlaster collection unavailable")


def _execution_db_path(context: Mapping[str, Any], client: Any) -> Path:
    blaster = context.get("blaster")
    raw = getattr(blaster, "execution_db_path", None)
    if raw is None:
        raw = getattr(getattr(client, "settings", None), "execution_db_path", None)
    if raw is None:
        raise RuntimeError("execution_db_path unavailable for OfferBlaster accounting")
    return Path(raw)


def _force_safe_after_accounting_failure(
    context: dict[str, Any],
    client: Any,
    *,
    state: Any | None,
) -> None:
    context["halted"] = True
    settings = getattr(client, "settings", None)
    if settings is not None and hasattr(settings, "dry_run"):
        try:
            settings.dry_run = True
        except Exception:
            pass

    resolved_state = state
    if resolved_state is None:
        try:
            from okx_nft_bot.undercutter.state import PositionState

            resolved_state = PositionState(_execution_db_path(context, client))
        except Exception:
            resolved_state = None
    if resolved_state is not None:
        try:
            resolved_state.set_force_dry_run(
                True,
                reason="offer_blaster_submit_log_failure",
            )
        except Exception:
            pass


def install_offer_blaster_accounting(
    blaster_class: type[Any],
    okx_client_class: type[Any],
) -> None:
    """Track every durable ETH OfferBlaster submit at its caller boundary.

    Other OKX callers already own their execution_submit_log and active-offer
    state. ContextVar scoping keeps this patch specific to OfferBlaster._blast_eth
    so those paths are not double-counted.
    """
    current_blast = blaster_class._blast_eth
    current_submit = okx_client_class.submit_offer
    blast_installed = bool(getattr(current_blast, "_r27_active_offer_tracking", False))
    submit_installed = bool(getattr(current_submit, "_r27_active_offer_tracking", False))
    if blast_installed and submit_installed:
        return
    if blast_installed != submit_installed:
        raise RuntimeError("partial R27 OfferBlaster state installation detected")

    original_blast = current_blast
    original_submit = current_submit

    @wraps(original_blast)
    def guarded_blast_eth(self: Any, *args: Any, **kwargs: Any) -> Any:
        context: dict[str, Any] = {"blaster": self, "halted": False}
        token = _BLASTER_CONTEXT.set(context)
        try:
            return original_blast(self, *args, **kwargs)
        finally:
            _BLASTER_CONTEXT.reset(token)

    @wraps(original_submit)
    def guarded_submit_offer(self: Any, payload: Mapping[str, Any]) -> Any:
        context = _BLASTER_CONTEXT.get()
        if context is None:
            return original_submit(self, payload)
        if context.get("halted"):
            raise RuntimeError("OfferBlaster live submits halted after accounting failure")

        chain = str(payload.get("chain") or "").strip().lower()
        if chain not in {"eth", "ethereum"}:
            raise RuntimeError(
                f"OfferBlaster accounting context received unexpected chain {chain!r}"
            )

        result = original_submit(self, payload)
        state = None
        offer_id = "unknown"
        try:
            offer_id = _durable_offer_id(result)
            requirements = _eth_buy_requirements(payload)
            from okx_nft_bot.counterbid.submit_safety import _buy_price_bnb_equiv

            price_bnb, price_usd = _buy_price_bnb_equiv(
                chain_name="eth",
                requirements=requirements,
            )
            collection = _collection_from_payload(payload)
            from okx_nft_bot.undercutter.state import PositionState

            state = PositionState(_execution_db_path(context, self))
            # R27: the kill switch falls back to local active_offers when the
            # exchange lookup is unavailable. Persist the durable ETH offer
            # before the submit-ledger row so a later ledger-write failure still
            # leaves the live offer visible to that degraded emergency path.
            state.upsert_active_offer(
                order_hash=offer_id,
                collection=collection,
                chain="eth",
                price_bnb=price_bnb,
                status="active",
            )
            state.record_submit_event(
                engine="offer_blaster",
                action_type="LIVE_OFFER_BLAST",
                collection=collection,
                chain="eth",
                price_bnb=price_bnb,
                status="submitted",
                reason=(
                    f"offer_id={offer_id};token_id={payload.get('token_id')};"
                    f"price_usd={price_usd:.8f}"
                ),
            )
        except Exception as exc:
            _force_safe_after_accounting_failure(
                context,
                self,
                state=state,
            )
            raise RuntimeError(
                "OfferBlaster post-submit accounting failed after durable effect "
                f"offer_id={offer_id}: {exc}"
            ) from exc
        return result

    guarded_blast_eth._r26_accounting_context = True
    guarded_submit_offer._r26_accounting_guard = True
    guarded_blast_eth._r27_active_offer_tracking = True
    guarded_submit_offer._r27_active_offer_tracking = True
    blaster_class._blast_eth = guarded_blast_eth
    okx_client_class.submit_offer = guarded_submit_offer
