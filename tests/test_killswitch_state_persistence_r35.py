from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.killswitch import _cancel_chain, format_killswitch_result


class _API:
    def __init__(self, *, cancel_result: bool = False, malformed: bool = False) -> None:
        self.cancel_result = cancel_result
        self.malformed = malformed
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        if self.malformed:
            return [{"collectionAddress": "0x" + "4" * 40, "protocolData": {}}]
        return [
            {
                "offerId": f"offer-{chain}",
                "collectionAddress": "0x" + "4" * 40,
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancelled.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return self.cancel_result


class _AuditBrokenMixin:
    def record_submit_event(self, **_kwargs):
        raise RuntimeError("audit persistence unavailable")

    def log_action(self, **_kwargs):
        raise RuntimeError("audit persistence unavailable")


def test_lookup_and_zombie_upsert_failure_preserve_exchange_failure_counts():
    class _State(_AuditBrokenMixin):
        def get_active_offers(self, *, chain: str):
            raise RuntimeError(f"{chain} state read unavailable")

        def upsert_active_offer(self, **_kwargs):
            raise RuntimeError("zombie persistence unavailable")

    api = _API(cancel_result=False)
    result = _cancel_chain(state=_State(), api=api, chain="eth")

    assert api.cancelled == ["eth"]
    assert result.exchange_seen == 1
    assert result.live_cancelled == 0
    assert result.failed == ("offer-eth:cancel_failed",)
    assert result.local_state_lookup_failed is True
    assert result.local_state_lookup_error == "eth state read unavailable"
    assert result.local_state_persistence_failed is True
    assert "upsert_killswitch_failed[offer-eth]: zombie persistence unavailable" in (
        result.local_state_persistence_error or ""
    )
    assert result.fatal_error is None
    # lookup+persistence are one degraded local-state failure, plus cancel failure.
    assert result.failure_count == 2


def test_zombie_upsert_failure_after_clean_lookup_does_not_zero_result():
    class _State(_AuditBrokenMixin):
        def get_active_offers(self, *, chain: str):
            return []

        def upsert_active_offer(self, **_kwargs):
            raise RuntimeError("sqlite write unavailable")

    api = _API(cancel_result=False)
    result = _cancel_chain(state=_State(), api=api, chain="bsc")

    assert api.cancelled == ["bsc"]
    assert result.exchange_seen == 1
    assert result.failed == ("offer-bsc:cancel_failed",)
    assert result.local_state_lookup_failed is False
    assert result.local_state_persistence_failed is True
    assert result.fatal_error is None
    assert result.failure_count == 2


def test_successful_exchange_cancel_survives_local_mark_failure():
    active = SimpleNamespace(order_hash="offer-eth")

    class _State(_AuditBrokenMixin):
        def get_active_offers(self, *, chain: str):
            return [active]

        def mark_offer_status(self, **_kwargs):
            raise RuntimeError("mark unavailable")

        def upsert_active_offer(self, **_kwargs):
            raise AssertionError("known local offer must not be upserted")

    api = _API(cancel_result=True)
    result = _cancel_chain(state=_State(), api=api, chain="eth")

    assert api.cancelled == ["eth"]
    assert result.exchange_seen == 1
    assert result.live_cancelled == 1
    assert result.failed == ()
    assert result.local_state_persistence_failed is True
    assert result.local_state_persistence_error == (
        "mark_cancelled[offer-eth]: mark unavailable"
    )
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
    assert "live_cancelled=1" in text
    assert "state_persist_failed=1" in text
    assert "total_failed=1" in text


def test_malformed_exchange_quarantine_write_failure_stays_explicit():
    class _State(_AuditBrokenMixin):
        def get_active_offers(self, *, chain: str):
            return []

        def upsert_active_offer(self, **_kwargs):
            raise RuntimeError("quarantine DB unavailable")

    api = _API(malformed=True)
    result = _cancel_chain(state=_State(), api=api, chain="eth")

    assert api.cancelled == []
    assert result.exchange_seen == 1
    assert len(result.failed) == 1
    assert result.failed[0].startswith("exchange_unidentified_")
    assert result.failed[0].endswith(":missing_order_id")
    assert result.local_state_persistence_failed is True
    assert "upsert_unidentified[exchange_unidentified_" in (
        result.local_state_persistence_error or ""
    )
    assert result.fatal_error is None
    assert result.failure_count == 2
