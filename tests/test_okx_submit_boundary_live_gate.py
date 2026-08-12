from __future__ import annotations

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.counterbid.submit_safety import install_submit_safety


class _Governor:
    blocked = "live arm required"
    instances = []

    def __init__(self, *, settings, api_client=None, **_kwargs):
        self.settings = settings
        self.api_client = api_client
        self.gate_calls = []
        self.__class__.instances.append(self)

    def check_live_submit_allowed(self, **kwargs):
        self.gate_calls.append(kwargs)
        return self.__class__.blocked


def _install_governor(monkeypatch, blocked="live arm required"):
    import okx_nft_bot.execution_governor as governor_module

    _Governor.blocked = blocked
    _Governor.instances.clear()
    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)


def _make_client_class():
    class DummyClient:
        _SUBMIT_ORDER_PATH = "/priapi/v1/nft/trading/seaport/step/submitOrder"

        def __init__(self):
            self.settings = object()
            self.request_calls = []

        def _request(self, *, method, path, params=None, payload=None):
            self.request_calls.append(
                {
                    "method": method,
                    "path": path,
                    "params": params,
                    "payload": payload,
                }
            )
            return {"ok": True}

        def _complete_two_step_offer(
            self,
            step1_resp,
            private_key,
            chain_id,
            endpoint,
        ):
            _ = step1_resp, private_key, endpoint
            return self._request(
                method="POST",
                path=self._SUBMIT_ORDER_PATH + "?t=123",
                payload={"items": [{"order": "signed"}]},
            )

    install_submit_safety(DummyClient)
    return DummyClient


def test_counterbid_package_installs_submit_boundary_guard():
    assert getattr(OKXAPIClient._request, "_r16_submit_guard", False) is True
    assert getattr(
        OKXAPIClient._complete_two_step_offer,
        "_r16_submit_context",
        False,
    ) is True

    # R15 approval hardening must remain installed after the R16 installer.
    assert OKXAPIClient._auto_approve_erc20.__module__ == (
        "okx_nft_bot.counterbid.approval_safety"
    )
    assert OKXAPIClient._auto_approve_nft.__module__ == (
        "okx_nft_bot.counterbid.approval_safety"
    )


def test_direct_submit_order_blocks_at_http_boundary(monkeypatch):
    _install_governor(monkeypatch)
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder live gate blocked: live arm required"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH + "?t=999",
            payload={"chain": 56, "items": []},
        )

    assert client.request_calls == []
    assert len(_Governor.instances) == 1
    assert _Governor.instances[0].api_client is client
    assert _Governor.instances[0].gate_calls == [
        {
            "action_type": "LIVE_OKX_SUBMIT_ORDER",
            "collection": "okx_submit_order",
            "chain": "bsc",
            "price_bnb": 0.0,
        }
    ]


def test_two_step_submit_uses_context_chain_and_blocks(monkeypatch):
    _install_governor(monkeypatch)
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder live gate blocked: live arm required"):
        client._complete_two_step_offer({}, "private-key", 1, None)

    assert client.request_calls == []
    assert len(_Governor.instances) == 1
    assert _Governor.instances[0].gate_calls[0]["chain"] == "eth"


def test_allowed_submit_reaches_original_request_once(monkeypatch):
    _install_governor(monkeypatch, blocked=None)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"chain": "bsc", "items": []},
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1
    assert len(_Governor.instances) == 1
    assert _Governor.instances[0].gate_calls[0]["chain"] == "bsc"


def test_cancel_endpoint_is_not_subject_to_submit_gate(monkeypatch):
    import okx_nft_bot.execution_governor as governor_module

    class UnexpectedGovernor:
        def __init__(self, **_kwargs):
            pytest.fail("cancel endpoint must not instantiate submit governor")

    monkeypatch.setattr(governor_module, "ExecutionGovernor", UnexpectedGovernor)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path="/api/v5/mktplace/nft/markets/cancel-listing",
        payload={"orderIds": ["order-1"]},
    )

    assert result == {"ok": True}
    assert client.request_calls[0]["path"].endswith("cancel-listing")


def test_submit_order_without_chain_context_fails_closed(monkeypatch):
    import okx_nft_bot.execution_governor as governor_module

    class UnexpectedGovernor:
        def __init__(self, **_kwargs):
            pytest.fail("unknown chain must fail before constructing governor")

    monkeypatch.setattr(governor_module, "ExecutionGovernor", UnexpectedGovernor)
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="chain context unavailable"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload={"items": []},
        )

    assert client.request_calls == []
