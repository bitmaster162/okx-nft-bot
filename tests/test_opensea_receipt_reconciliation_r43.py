from __future__ import annotations

from contextlib import contextmanager

import pytest

from okx_nft_bot.clients.http import HTTPStatusError
from okx_nft_bot.clients.opensea import OpenSeaClient, SEAPORT_ADDRESS_ETH
from okx_nft_bot.sniper.counter_bidder import CounterBidder
from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT
from okx_nft_bot.sniper.opensea_receipt_reconciliation import (
    derive_seaport_order_hash,
    install_opensea_receipt_reconciliation,
)


OFFERER = "0x" + "1" * 40
COLLECTION = "0x" + "2" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ZONE = "0x000056f7000000ece9003ca63978907a00ffd100"
CONDUIT = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
ZERO32 = "0x" + "00" * 32


def _parameters(*, salt: int = 123, counter: int = 7) -> dict:
    return {
        "offerer": OFFERER,
        "zone": ZONE,
        "offer": [
            {
                "itemType": 1,
                "token": WETH,
                "identifierOrCriteria": 0,
                "startAmount": "500",
                "endAmount": "500",
            }
        ],
        "consideration": [
            {
                "itemType": 4,
                "token": COLLECTION,
                "identifierOrCriteria": 0,
                "startAmount": "1",
                "endAmount": "1",
                "recipient": OFFERER,
            },
            {
                "itemType": 1,
                "token": WETH,
                "identifierOrCriteria": 0,
                "startAmount": "5",
                "endAmount": "5",
                "recipient": "0x0000a26b00c1f0df003000390027140000faa719",
            },
        ],
        "orderType": 2,
        "startTime": "1700000000",
        "endTime": "1700600000",
        "zoneHash": ZERO32,
        "salt": str(salt),
        "conduitKey": CONDUIT,
        "counter": str(counter),
        "totalOriginalConsiderationItems": 2,
    }


@contextmanager
def _mirror_scope(context=None):
    resolved = context if context is not None else {"bidder": object(), "halted": False}
    token = _MIRROR_CONTEXT.set(resolved)
    try:
        yield resolved
    finally:
        _MIRROR_CONTEXT.reset(token)


class _Settings:
    opensea_api_base = "https://api.opensea.test"
    opensea_api_key = "test-key"


class _Transport:
    def __init__(self, *, post_result=None, post_error=None, get_result=None, get_error=None):
        self.post_result = post_result
        self.post_error = post_error
        self.get_result = get_result
        self.get_error = get_error
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        method = kwargs["method"].upper()
        if method == "POST":
            if self.post_error is not None:
                raise self.post_error
            return self.post_result or {"order_hash": "0xnormal"}
        if method == "GET":
            if self.get_error is not None:
                raise self.get_error
            return self.get_result or {}
        raise AssertionError(f"unexpected method {method}")


def _installed_classes():
    class DummyClient:
        def __init__(self, transport):
            self.settings = _Settings()
            self.transport = transport
            self.fail_before_submit = False
            self.create_result = {"order_id": "0xcreate", "status": "submitted"}

        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            if self.fail_before_submit:
                raise RuntimeError("blocked before POST")
            return self.transport.request_json(
                method="POST",
                url="https://api.opensea.test/api/v2/orders/ethereum/seaport/offers",
                headers={},
                body="{}",
            )

        def create_opensea_offer(self, *args, **kwargs):
            return self.create_result

    class DummyBidder:
        def __init__(self, state=None):
            self.state = state
            self.dry_run = False
            self.original_record_calls = []

        def _get_execution_state(self):
            if isinstance(self.state, Exception):
                raise self.state
            return self.state

        def _record_execution_submit_event(self, **kwargs):
            self.original_record_calls.append(kwargs)

    install_opensea_receipt_reconciliation(DummyBidder, DummyClient)
    return DummyBidder, DummyClient


def test_r43_order_hash_is_stable_bytes32_and_changes_with_signed_fields():
    first = derive_seaport_order_hash(_parameters())
    second = derive_seaport_order_hash(_parameters())
    changed = derive_seaport_order_hash(_parameters(salt=124))

    assert first == second
    assert first.startswith("0x") and len(first) == 66
    assert changed != first


def test_r43_ambiguous_submit_reconciles_exact_order_hash():
    Bidder, Client = _installed_classes()
    params = _parameters()
    expected = derive_seaport_order_hash(params)
    transport = _Transport(
        post_error=RuntimeError("connection reset after send"),
        get_result={"order_hash": expected, "order_status": "active"},
    )
    client = Client(transport)

    with _mirror_scope():
        result = client._submit_opensea_offer(params, "0xsig", "eth")

    assert result["status"] == "submitted"
    assert result["reconciled"] is True
    assert result["order_id"] == expected
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1]["url"].endswith(
        f"/api/v2/orders/chain/ethereum/protocol/{SEAPORT_ADDRESS_ETH}/{expected}"
    )


def test_r43_unresolved_ambiguous_submit_returns_uncertain_hash():
    Bidder, Client = _installed_classes()
    params = _parameters()
    expected = derive_seaport_order_hash(params)
    transport = _Transport(
        post_error=HTTPStatusError(503, "upstream unavailable"),
        get_error=HTTPStatusError(404, "not indexed"),
    )
    client = Client(transport)

    with _mirror_scope():
        result = client._submit_opensea_offer(params, "0xsig", "eth")

    assert result["status"] == "submit_uncertain"
    assert result["receipt_uncertain"] is True
    assert result["order_hash"] == expected
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_r43_deterministic_400_is_not_reconciled():
    Bidder, Client = _installed_classes()
    transport = _Transport(post_error=HTTPStatusError(400, "invalid order"))
    client = Client(transport)

    with _mirror_scope(), pytest.raises(HTTPStatusError) as exc_info:
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert exc_info.value.status == 400
    assert [call["method"] for call in transport.calls] == ["POST"]


def test_r43_failure_before_target_post_is_not_reconciled():
    Bidder, Client = _installed_classes()
    transport = _Transport()
    client = Client(transport)
    client.fail_before_submit = True

    with _mirror_scope(), pytest.raises(RuntimeError, match="blocked before POST"):
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert transport.calls == []


def test_r43_direct_non_mirror_submit_keeps_original_contract():
    Bidder, Client = _installed_classes()
    transport = _Transport(post_result={"order_id": "direct"})
    client = Client(transport)

    result = client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert result == {"order_id": "direct"}
    assert [call["method"] for call in transport.calls] == ["POST"]


def test_r43_create_propagates_uncertain_receipt_into_mirror_context():
    Bidder, Client = _installed_classes()
    client = Client(_Transport())
    expected = derive_seaport_order_hash(_parameters())
    client.create_result = {
        "order_id": expected,
        "order_hash": expected,
        "receipt_uncertain": True,
        "status": "submit_uncertain",
        "raw": {"submit_error": "timeout"},
    }
    context = {"bidder": object(), "halted": False}

    with _mirror_scope(context):
        result = client.create_opensea_offer()
        assert result["receipt_uncertain"] is True
        assert context["receipt_uncertain"] is True
        assert context["order_id"] == expected
        assert context["receipt_detail"] == {"submit_error": "timeout"}


class _State:
    def __init__(self):
        self.force_calls = []
        self.upserts = []
        self.submit_events = []

    def set_force_dry_run(self, enabled, *, reason=None):
        self.force_calls.append((enabled, reason))

    def upsert_active_offer(self, **kwargs):
        self.upserts.append(kwargs)

    def record_submit_event(self, **kwargs):
        self.submit_events.append(kwargs)


def test_r43_uncertain_record_quarantines_exposure_and_halts():
    Bidder, Client = _installed_classes()
    state = _State()
    bidder = Bidder(state)
    order_hash = derive_seaport_order_hash(_parameters())
    context = {
        "bidder": bidder,
        "halted": False,
        "receipt_uncertain": True,
        "order_id": order_hash,
        "price_bnb": 0.00125,
        "price_usd": 0.50,
    }

    with _mirror_scope(context), pytest.raises(RuntimeError, match="exposure quarantined"):
        bidder._record_execution_submit_event(
            chain="eth",
            collection=COLLECTION,
            price_bnb=0.00125,
            status="submitted",
            reason=f"opensea order_id={order_hash}",
        )

    assert bidder.dry_run is True
    assert bidder._r39_opensea_mirror_halted is True
    assert context["halted"] is True
    assert state.force_calls == [(True, "opensea_mirror_receipt_uncertain")]
    assert len(state.upserts) == 1
    quarantined = state.upserts[0]
    assert quarantined["order_hash"] == order_hash
    assert quarantined["status"] == "killswitch_failed"
    assert quarantined["preview_payload"]["marketplace"] == "opensea"
    assert quarantined["preview_payload"]["receipt"] == "uncertain"
    assert len(state.submit_events) == 1
    assert state.submit_events[0]["action_type"] == "LIVE_OPENSEA_MIRROR"
    assert state.submit_events[0]["status"] == "uncertain"
    assert bidder.original_record_calls == []


def test_r43_normal_record_delegates_without_quarantine():
    Bidder, Client = _installed_classes()
    state = _State()
    bidder = Bidder(state)
    context = {
        "bidder": bidder,
        "halted": False,
        "receipt_uncertain": False,
        "order_id": "0xabc",
        "price_bnb": 0.001,
        "price_usd": 0.40,
    }
    kwargs = {
        "chain": "eth",
        "collection": COLLECTION,
        "price_bnb": 0.001,
        "status": "submitted",
        "reason": "opensea order_id=0xabc",
    }

    with _mirror_scope(context):
        bidder._record_execution_submit_event(**kwargs)

    assert bidder.original_record_calls == [kwargs]
    assert state.force_calls == []
    assert state.upserts == []


def test_r43_installer_is_idempotent():
    Bidder, Client = _installed_classes()
    submit = Client._submit_opensea_offer
    create = Client.create_opensea_offer
    record = Bidder._record_execution_submit_event

    install_opensea_receipt_reconciliation(Bidder, Client)

    assert Client._submit_opensea_offer is submit
    assert Client.create_opensea_offer is create
    assert Bidder._record_execution_submit_event is record


def test_real_wrapper_order_preserves_r42_r41_and_r39():
    submit = OpenSeaClient._submit_opensea_offer
    assert getattr(submit, "_r43_opensea_receipt_reconciliation", False)
    assert getattr(submit.__wrapped__, "_r42_opensea_effect_boundary_guard", False)
    assert getattr(submit.__wrapped__.__wrapped__, "_r41_opensea_canonical_submit_route", False)

    create = OpenSeaClient.create_opensea_offer
    assert getattr(create, "_r43_opensea_receipt_context", False)
    assert getattr(create.__wrapped__, "_r39_opensea_mirror_gate", False)

    record = CounterBidder._record_execution_submit_event
    assert getattr(record, "_r43_opensea_uncertain_quarantine", False)
    assert getattr(record.__wrapped__, "_r39_opensea_mirror_accounting", False)
