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
    import okx_nft_bot.execution_governor as governor_module

    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)


def _buy_parameters(*, counter=7, amount=100, offerer=None):
    wallet = offerer or ("0x" + "1" * 40)
    return {
        "offerer": wallet,
        "counter": str(counter),
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


def _sell_parameters(*, counter=7):
    wallet = "0x" + "4" * 40
    return {
        "offerer": wallet,
        "counter": str(counter),
        "offer": [
            {
                "itemType": 2,
                "token": "0x" + "5" * 40,
                "identifierOrCriteria": 9,
                "startAmount": "1",
                "endAmount": "1",
            }
        ],
        "consideration": [
            {
                "itemType": 1,
                "token": "0x" + "6" * 40,
                "identifierOrCriteria": 0,
                "startAmount": "100",
                "endAmount": "100",
                "recipient": wallet,
            }
        ],
    }


def _protocol_payload(*, parameters, chain=56):
    payload = {
        "items": [
            {
                "protocolData": json.dumps(
                    {"parameters": parameters, "signature": "0xsig"}
                )
            }
        ]
    }
    if chain is not None:
        payload["chain"] = chain
    if parameters.get("offerer"):
        payload["walletAddress"] = parameters["offerer"]
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
                payload=_protocol_payload(
                    parameters=_buy_parameters(counter=7),
                    chain=None,
                ),
            )

        def _primary_rpc(self, chain_name):
            return f"https://{chain_name}.example.invalid"

    install_submit_safety(DummyClient)
    return DummyClient


def test_real_client_has_r23_counter_guard_installed():
    assert getattr(OKXAPIClient._request, "_r23_counter_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r22_protocoldata_guard", False) is True


def test_matching_signed_counter_allows_downstream_guards_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    counter_reads = []

    def read_counter(_client, **kwargs):
        counter_reads.append(kwargs)
        return 7

    monkeypatch.setattr(safety, "_read_seaport_counter", read_counter)
    monkeypatch.setattr(safety, "_bsc_quota_block_reason", lambda _client: None)
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_a, **_k: 100)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_protocol_payload(parameters=_buy_parameters(counter=7)),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1
    assert counter_reads == [
        {
            "chain_name": "bsc",
            "offerer": "0x" + "1" * 40,
        }
    ]


def test_stale_signed_counter_blocks_before_quota_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_read_seaport_counter", lambda *_a, **_k: 8)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("quota must not run after stale counter"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run after stale counter"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="stale Seaport counter.*signed=7.*on_chain=8"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(parameters=_buy_parameters(counter=7)),
        )

    assert client.request_calls == []


def test_counter_rpc_failure_blocks_before_quota_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_seaport_counter",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rpc exhausted")),
    )
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("quota must not run after counter RPC failure"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run after counter RPC failure"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder counter gate blocked: rpc exhausted"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(parameters=_buy_parameters(counter=7)),
        )

    assert client.request_calls == []


def test_protocoldata_missing_counter_fails_closed_before_counter_rpc(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    parameters = _buy_parameters(counter=7)
    del parameters["counter"]
    monkeypatch.setattr(
        safety,
        "_read_seaport_counter",
        lambda *_a, **_k: pytest.fail("counter RPC must not run for malformed signed data"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="signed protocolData missing offerer or counter"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(parameters=parameters),
        )

    assert client.request_calls == []


def test_conflicting_batch_counters_fail_closed_before_counter_rpc(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    wallet = "0x" + "1" * 40
    payload = {
        "chain": 56,
        "walletAddress": wallet,
        "items": [
            {
                "protocolData": json.dumps(
                    {"parameters": _buy_parameters(counter=7), "signature": "0x1"}
                )
            },
            {
                "protocolData": json.dumps(
                    {"parameters": _buy_parameters(counter=8), "signature": "0x2"}
                )
            },
        ],
    }
    monkeypatch.setattr(
        safety,
        "_read_seaport_counter",
        lambda *_a, **_k: pytest.fail("counter RPC must not run for conflicting batch"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="conflicting counters"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=payload,
        )

    assert client.request_calls == []


def test_sell_protocoldata_is_also_counter_checked(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_read_seaport_counter", lambda *_a, **_k: 8)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("SELL must not run BUY quota"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("SELL must not run BUY balance"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="stale Seaport counter"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_protocol_payload(parameters=_sell_parameters(counter=7)),
        )

    assert client.request_calls == []


def test_two_step_counter_gate_uses_eth_context(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    observed = []

    def read_counter(_client, **kwargs):
        observed.append((kwargs["chain_name"], kwargs["offerer"]))
        return 7

    monkeypatch.setattr(safety, "_read_seaport_counter", read_counter)
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_a, **_k: 100)
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("ETH two-step must not run BSC quota"),
    )
    Client = _make_client_class()
    client = Client()

    result = client._complete_two_step_offer({}, "private-key", 1, None)

    assert result == {"ok": True}
    assert observed == [("eth", "0x" + "1" * 40)]
    assert len(client.request_calls) == 1
