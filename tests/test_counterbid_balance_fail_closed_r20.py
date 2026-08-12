from __future__ import annotations

import pytest

from okx_nft_bot.counterbid.submit_safety import install_submit_safety


class _Governor:
    blocked = None

    def __init__(self, *, settings, api_client=None, **_kwargs):
        self.settings = settings
        self.api_client = api_client

    def check_live_submit_allowed(self, **_kwargs):
        return self.__class__.blocked


def _install_governor(monkeypatch, blocked=None):
    import okx_nft_bot.execution_governor as governor_module

    _Governor.blocked = blocked
    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)


def _buy_payload(*, chain=56, wallet=None, offerer=None, amount=100):
    wallet = wallet or ("0x" + "1" * 40)
    offerer = offerer or wallet
    token = "0x" + "2" * 40
    payload = {
        "items": [
            {
                "parameters": {
                    "offerer": offerer,
                    "offer": [
                        {
                            "itemType": 1,
                            "token": token,
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
            }
        ],
        "walletAddress": wallet,
    }
    if chain is not None:
        payload["chain"] = chain
    return payload


def _sell_payload(*, chain=56):
    wallet = "0x" + "4" * 40
    return {
        "chain": chain,
        "walletAddress": wallet,
        "items": [
            {
                "parameters": {
                    "offerer": wallet,
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
            }
        ],
    }


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
                path=self._SUBMIT_ORDER_PATH,
                payload=_buy_payload(chain=None),
            )

        def _primary_rpc(self, chain_name):
            return f"https://{chain_name}.example.invalid"

    install_submit_safety(DummyClient)
    return DummyClient


def test_buy_submit_balance_read_failure_blocks_before_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rpc unavailable")),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder balance gate blocked: rpc unavailable"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(),
        )

    assert client.request_calls == []


def test_buy_submit_insufficient_balance_blocks_before_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: 99,
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="insufficient ERC20 balance"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(amount=100),
        )

    assert client.request_calls == []


def test_buy_submit_exact_balance_reaches_http_once(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
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
        payload=_buy_payload(amount=100),
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


def test_buy_submit_wallet_mismatch_fails_closed_before_balance_read(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: pytest.fail("balance read must not run"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="walletAddress does not match Seaport offerer"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(
                wallet="0x" + "1" * 40,
                offerer="0x" + "7" * 40,
            ),
        )

    assert client.request_calls == []


def test_sell_submit_does_not_use_buy_balance_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: pytest.fail("sell order must not read buy-side balance"),
    )
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_sell_payload(),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1


def test_two_step_buy_submit_uses_context_chain_for_balance_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    observed = []

    def read_balance(_client, **kwargs):
        observed.append(kwargs["chain_name"])
        return 100

    monkeypatch.setattr(safety, "_read_erc20_balance_raw", read_balance)
    Client = _make_client_class()
    client = Client()

    result = client._complete_two_step_offer({}, "private-key", 1, None)

    assert result == {"ok": True}
    assert observed == ["eth"]
    assert len(client.request_calls) == 1
