from __future__ import annotations

import math
from contextvars import ContextVar
from functools import wraps
from typing import Any


_MIRROR_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "counter_bidder_opensea_mirror_context",
    default=None,
)


def _strict_mirror_price(*, price_wei: Any, currency_address: Any) -> tuple[float, float]:
    try:
        raw_amount = int(price_wei)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea mirror price_wei is invalid") from exc
    if raw_amount <= 0:
        raise RuntimeError("OpenSea mirror price_wei must be positive")

    token = str(currency_address or "").strip().lower()
    if not token:
        raise RuntimeError("OpenSea mirror currency address unavailable")

    from okx_nft_bot.counterbid.submit_safety import _buy_price_bnb_equiv

    return _buy_price_bnb_equiv(
        chain_name="eth",
        requirements={token: raw_amount},
    )


def _incremental_limit_block_reason(
    bidder: Any,
    *,
    price_bnb: float,
) -> str | None:
    """Check the second mirror effect against global live-submit budgets.

    The mirror is deliberately paired immediately after the OKX offer, so the
    shared cooldown is not re-applied here. Hourly count and daily BNB-equivalent
    exposure are still additive because the OpenSea mirror is a second durable
    live order and is recorded as a second submitted event.
    """
    if not math.isfinite(price_bnb) or price_bnb <= 0:
        raise RuntimeError("OpenSea mirror BNB-equivalent price is invalid")

    governor = bidder._get_execution_governor()
    if governor is None:
        raise RuntimeError("OpenSea mirror execution governor unavailable")
    snapshot = governor.get_rate_limit_snapshot(chain="eth")
    settings = governor.settings

    try:
        hourly_count = int(snapshot["hourly_count"])
        hourly_limit = int(settings.max_live_offers_per_hour)
        daily_bnb = float(snapshot["daily_bnb"])
        daily_limit = float(settings.max_bnb_per_day)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenSea mirror rate-limit state is invalid") from exc

    if hourly_count < 0 or hourly_limit <= 0:
        raise RuntimeError("OpenSea mirror hourly limit state is invalid")
    if not math.isfinite(daily_bnb) or daily_bnb < 0:
        raise RuntimeError("OpenSea mirror daily spend state is invalid")
    if not math.isfinite(daily_limit) or daily_limit <= 0:
        raise RuntimeError("OpenSea mirror daily spend limit is invalid")

    if hourly_count >= hourly_limit:
        return f"rate limit hit: {hourly_count}/{hourly_limit} live submits in the last hour"
    if daily_bnb + price_bnb > daily_limit:
        return (
            f"daily BNB-equivalent cap hit: spent {daily_bnb:.4f} + "
            f"mirror {price_bnb:.6f} = {daily_bnb + price_bnb:.6f} BNB "
            f"> cap {daily_limit:.4f} BNB"
        )
    return None


def _force_safe_after_submit_log_failure(
    context: dict[str, Any],
    *,
    state: Any | None,
) -> None:
    bidder = context.get("bidder")
    context["halted"] = True
    if bidder is None:
        return

    try:
        bidder.dry_run = True
    except Exception:
        pass
    try:
        bidder._r39_opensea_mirror_halted = True
    except Exception:
        pass

    resolved_state = state
    if resolved_state is None:
        try:
            resolved_state = bidder._get_execution_state()
        except Exception:
            resolved_state = None
    if resolved_state is not None:
        try:
            resolved_state.set_force_dry_run(
                True,
                reason="opensea_mirror_submit_log_failure",
            )
        except Exception:
            pass


def _durable_mirror_order_id(result: Any) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("OpenSea mirror submit result is not an object")
    raw = result.get("offer_id") or result.get("order_id") or result.get("order_hash")
    order_id = str(raw or "").strip()
    if not order_id:
        raise RuntimeError("OpenSea mirror durable order id unavailable")
    return order_id


def install_opensea_mirror_safety(
    bidder_class: type[Any],
    opensea_client_class: type[Any],
) -> None:
    """Price-gate, track, and strictly account CounterBidder's OpenSea mirror."""
    current_mirror = bidder_class._mirror_to_opensea
    current_create = opensea_client_class.create_opensea_offer
    current_record = bidder_class._record_execution_submit_event

    mirror_installed = bool(getattr(current_mirror, "_r39_opensea_mirror_context", False))
    create_installed = bool(getattr(current_create, "_r39_opensea_mirror_gate", False))
    record_installed = bool(getattr(current_record, "_r39_opensea_mirror_accounting", False))
    if mirror_installed and create_installed and record_installed:
        return
    if any((mirror_installed, create_installed, record_installed)):
        raise RuntimeError("partial R39 OpenSea mirror safety installation detected")

    original_mirror = current_mirror
    original_create = current_create
    original_record = current_record

    @wraps(original_mirror)
    def guarded_mirror(self: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(self, "_r39_opensea_mirror_halted", False):
            raise RuntimeError("OpenSea mirror halted after submit accounting failure")
        context: dict[str, Any] = {
            "bidder": self,
            "price_bnb": None,
            "price_usd": None,
            "order_id": None,
            "halted": False,
        }
        token = _MIRROR_CONTEXT.set(context)
        try:
            return original_mirror(self, *args, **kwargs)
        finally:
            _MIRROR_CONTEXT.reset(token)

    @wraps(original_create)
    def guarded_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        context = _MIRROR_CONTEXT.get()
        if context is None:
            return original_create(self, *args, **kwargs)
        if context.get("halted"):
            raise RuntimeError("OpenSea mirror halted after submit accounting failure")

        chain = str(kwargs.get("chain") or "").strip().lower()
        if chain not in {"eth", "ethereum", "1"}:
            raise RuntimeError(f"OpenSea mirror safety received unexpected chain {chain!r}")

        price_bnb, price_usd = _strict_mirror_price(
            price_wei=kwargs.get("price_wei"),
            currency_address=kwargs.get("currency_address"),
        )
        bidder = context.get("bidder")
        if bidder is None:
            raise RuntimeError("OpenSea mirror bidder context unavailable")
        block_reason = _incremental_limit_block_reason(
            bidder,
            price_bnb=price_bnb,
        )
        if block_reason:
            raise RuntimeError(f"OpenSea mirror incremental gate blocked: {block_reason}")

        context["price_bnb"] = price_bnb
        context["price_usd"] = price_usd
        result = original_create(self, *args, **kwargs)
        context["order_id"] = _durable_mirror_order_id(result)
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
        context = _MIRROR_CONTEXT.get()
        is_opensea = context is not None and str(reason or "").lower().startswith("opensea")
        if not is_opensea:
            return original_record(
                self,
                chain=chain,
                collection=collection,
                price_bnb=price_bnb,
                status=status,
                reason=reason,
            )

        normalized_bnb = context.get("price_bnb")
        normalized_usd = context.get("price_usd")
        if normalized_bnb is not None:
            price_bnb = float(normalized_bnb)

        if status != "submitted":
            return original_record(
                self,
                chain=chain,
                collection=collection,
                price_bnb=price_bnb,
                status=status,
                reason=reason,
            )

        order_id = str(context.get("order_id") or "").strip()
        if normalized_bnb is None or normalized_usd is None or not order_id:
            raise RuntimeError("OpenSea mirror durable effect missing normalized inventory/accounting data")

        state = None
        try:
            state = self._get_execution_state()
            # R40: OpenSea is a distinct marketplace effect. Persist its durable
            # order id before the submit-ledger row so the emergency kill switch
            # can see unresolved exposure even if the later ledger write fails.
            state.upsert_active_offer(
                order_hash=order_id,
                collection=collection,
                chain=chain,
                price_bnb=float(normalized_bnb),
                status="active",
                preview_payload={
                    "marketplace": "opensea",
                    "source": "counter_bidder_mirror",
                },
            )
            state.record_submit_event(
                engine="counter_bidder",
                action_type="LIVE_OPENSEA_MIRROR",
                collection=collection,
                chain=chain,
                price_bnb=float(normalized_bnb),
                status="submitted",
                reason=f"{reason};price_usd={float(normalized_usd):.8f}",
            )
        except Exception as exc:
            _force_safe_after_submit_log_failure(
                context,
                state=state,
            )
            raise RuntimeError(
                "OpenSea mirror post-submit accounting failed after durable effect: "
                f"{exc}"
            ) from exc

    guarded_mirror._r39_opensea_mirror_context = True
    guarded_create._r39_opensea_mirror_gate = True
    guarded_record._r39_opensea_mirror_accounting = True
    guarded_record._r40_opensea_inventory_tracking = True
    bidder_class._mirror_to_opensea = guarded_mirror
    opensea_client_class.create_opensea_offer = guarded_create
    bidder_class._record_execution_submit_event = guarded_record
