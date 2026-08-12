from __future__ import annotations

import json

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.counterbid.submit_safety import install_submit_safety


class _Governor:
    def __init__(self, *, settings, api_client=None, **_kwargs):
        self.settings = settings
        self.api_client = api_client

    def check_live_submit_allowed(self, **_kwargs):
        return None


def _install_governor(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety
    import okx_nft_bot.execution_governor as governor_module

    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)
    monkeypatch.setattr(safety, "_read_seaport_counter", lambda *_a, **_k: 7)
    monkeypatch.setattr(safety, "_buy_price_bnb_equiv", lambda **_kwargs: (0.001, 1.0))


def _parameters(*, amount=100):
    wallet = "0x" + "1" * 40
    return {
        "offerer": wallet,
        "counter": "7",
        "offer": [
            {
                "itemType": 1,
                "token": "0x" + "2" * 40,
                "identifierOrCriteria": 0,
                "startAmount": str(amount),
                "endAmount": str(amount),
            }
        ],
        "consideration": [
            {
                "itemType": 2,
                "token": "0x" + "3" * 40,
                "identifierOrCriteria": 7,
                "startAmount": "1",
                "endAmount": "1",
                "recipient": wallet,
            }
        ],
    }


def _protocol_payload(*, chain=56, amount=100, protocol_data=None):
    wallet = "0x" + "1" * 40
    if protocol_data is None:
        protocol_data = json.dumps(
            {"parameters": _parameters(amount=amount), "signature": "0xsig"}
        )
    payload = {
        "walletAddress": wallet,
        "items": [{"protocolData": protocol_data}],
    }
    if chain is not None:
        payload["chain"] = chain
    return payload


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
                payload=_protocol_payload(chain=None),
            )

        def _primary_rpc(self, chain_name):
            return f"https://{chain_name}.example.invalid"

    install_submit_safety(DummyClient)
    return DummyClient


def test_real_client_has_r22_protocoldata_guard_installed():
    assert getattr(OKXAPIClient._request, "_r22_protocoldata_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r20_balance_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r21_quota_guard", False) is True


def test_protocoldata_json_buy_runs_balance_gate_before_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_bsc_quota_block_reason", lambda _client: None)
    observed = []

    def read_balance(_client, **kwargs):
        observed.append(kwargs)
        return 100

    monkeypatch.setattr(safety, "_read_erc20_balance_raw", read_balance)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_protocol_payload(amount=100),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1
    assert observed == [
        {
            "chain_name": "bsc",
            "token": "0x" + "2" * 40,
            "wallet": "0x" + "1" * 40,
        }
    ]


def test_protocoldata_json_buy_balance_failure_blocks_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_bsc_quota_block_reason", lambda _client: None)
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_a, **_k: 99)
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="insufficient ERC20 balance"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(amount=100),
        )

    assert client.request_calls == []


def test_protocoldata_json_buy_activates_bsc_quota_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: "quota_guard:25/25",
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run after quota block"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder quota gate blocked: quota_guard:25/25"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(),
        )

    assert client.request_calls == []


def test_malformed_protocoldata_fails_closed_before_quota_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("quota must not run for malformed signed data"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run for malformed signed data"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="protocolData is invalid JSON"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(protocol_data="{not-json"),
        )

    assert client.request_calls == []


def test_protocoldata_mapping_shape_is_supported(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_bsc_quota_block_reason", lambda _client: None)
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_a, **_k: 100)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_protocol_payload(
            protocol_data={"parameters": _parameters(), "signature": "0xsig"}
        ),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1


def test_two_step_protocoldata_uses_context_chain_for_r20_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    observed = []

    def read_balance(_client, **kwargs):
        observed.append(kwargs["chain_name"])
        return 100

    monkeypatch.setattr(safety, "_read_erc20_balance_raw", read_balance)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("ETH two-step must not run BSC quota"),
    )
    Client = _make_client_class()
    client = Client()

    result = client._complete_two_step_offer({}, "private-key", 1, None)

    assert result == {"ok": True}
    assert observed == ["eth"]
    assert len(client.request_calls) == 1
