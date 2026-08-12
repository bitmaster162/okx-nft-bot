from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.killswitch import _cancel_chain, format_killswitch_result


class _State:
    def __init__(self, active=None) -> None:
        self.active = list(active or [])
        self.marked: list[tuple[str, str]] = []
        self.submit_events: list[dict[str, object]] = []

    def get_active_offers(self, *, chain: str):
        return list(self.active)

    def mark_offer_status(self, *, order_hash: str, status: str, **_kwargs):
        self.marked.append((order_hash, status))
        return True

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("exchange lookup failure with successful local fallback must not upsert")

    def record_submit_event(self, **kwargs):
        self.submit_events.append(dict(kwargs))
        return len(self.submit_events)

    def log_action(self, **_kwargs):
        return 1


class _FailLookupAPI:
    def __init__(self, *, cancel_result: bool = True) -> None:
        self.cancel_result = cancel_result
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        raise RuntimeError(f"{chain} exchange inventory unavailable")

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancelled.append(order_hash)
        assert order_params is None
        return self.cancel_result


def test_exchange_lookup_failure_is_degraded_even_with_empty_local_state():
    state = _State()
    api = _FailLookupAPI()

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert api.cancelled == []
    assert result.exchange_lookup_failed is True
    assert result.exchange_lookup_error == "eth exchange inventory unavailable"
    assert result.exchange_seen == 0
    assert result.failed == ()
    assert result.fatal_error is None
    assert result.failure_count == 1

    text = format_killswitch_result(
        SimpleNamespace(
            activated_at="2026-08-12T00:00:00+00:00",
            chains=(result,),
            preflight_error=None,
            total_failed=result.failure_count,
        )
    )
    assert "exchange_lookup_failed=1" in text
    assert "total_failed=1" in text


def test_successful_local_fallback_cancel_still_reports_exchange_discovery_failure():
    active = SimpleNamespace(order_hash="known-local-offer")
    state = _State(active=[active])
    api = _FailLookupAPI(cancel_result=True)

    result = _cancel_chain(state=state, api=api, chain="bsc")

    assert api.cancelled == ["known-local-offer"]
    assert result.exchange_lookup_failed is True
    assert result.live_cancelled == 1
    assert result.failed == ()
    assert result.local_state_lookup_failed is False
    assert result.local_state_persistence_failed is False
    assert result.fatal_error is None
    assert result.failure_count == 1
    assert state.marked == [("known-local-offer", "cancelled")]
    assert state.submit_events[0]["status"] == "killswitch"
    assert "exchange_lookup_failed=1" in str(state.submit_events[0]["reason"])


def test_exchange_lookup_failure_with_no_state_is_counted_once_by_fatal_result():
    api = _FailLookupAPI()

    result = _cancel_chain(
        state=None,
        api=api,
        chain="eth",
        state_unavailable_error="execution DB init unavailable",
    )

    assert result.exchange_lookup_failed is True
    assert result.fatal_error == (
        "exchange lookup failed while local state unavailable: "
        "eth exchange inventory unavailable"
    )
    assert result.failure_count == 1
