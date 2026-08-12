from __future__ import annotations

import pytest

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


def _buy_payload(*, chain=56, amount=100):
    wallet = "0x" + "1" * 40
    return {
        "chain": chain,
        "walletAddress": wallet,
        "items": [
            {
                "parameters": {
                    "offerer": wallet,
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
            }
        ],
    }


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
            _ = step1_resp, private_key, chain_id, endpoint
            return {"unused": True}

        def _primary_rpc(self, chain_name):
            return f"https://{chain_name}.example.invalid"

    install_submit_safety(DummyClient)
    return DummyClient


def test_bsc_buy_quota_db_error_blocks_before_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_bsc_active_offer_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sqlite unavailable")),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: pytest.fail("balance read must not run after quota uncertainty"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder quota gate blocked: sqlite unavailable"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(),
        )

    assert client.request_calls == []


def test_bsc_buy_at_quota_threshold_blocks_before_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setenv("COUNTERBID_QUOTA_THRESHOLD", "25")
    monkeypatch.setattr(safety, "_read_bsc_active_offer_count", lambda *_args: 25)
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: pytest.fail("balance read must not run at quota"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder quota gate blocked: quota_guard:25/25"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(),
        )

    assert client.request_calls == []


def test_bsc_buy_below_quota_reaches_balance_then_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setenv("COUNTERBID_QUOTA_THRESHOLD", "25")
    monkeypatch.setattr(safety, "_read_bsc_active_offer_count", lambda *_args: 24)
    balance_calls = []

    def read_balance(_client, **kwargs):
        balance_calls.append(kwargs)
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
    assert len(balance_calls) == 1


def test_bsc_buy_invalid_quota_threshold_fails_closed_before_db(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setenv("COUNTERBID_QUOTA_THRESHOLD", "not-an-int")
    monkeypatch.setattr(
        safety,
        "_read_bsc_active_offer_count",
        lambda *_args: pytest.fail("DB count must not run for invalid threshold"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="invalid COUNTERBID_QUOTA_THRESHOLD"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_buy_payload(),
        )

    assert client.request_calls == []


def test_eth_buy_does_not_use_bsc_quota_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_bsc_active_offer_count",
        lambda *_args: pytest.fail("ETH buy must not read BSC quota"),
    )
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_args, **_kwargs: 100)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_buy_payload(chain=1),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1


def test_bsc_sell_does_not_use_buy_quota_or_balance_gate(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_read_bsc_active_offer_count",
        lambda *_args: pytest.fail("SELL submit must not read BUY quota"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_args, **_kwargs: pytest.fail("SELL submit must not read BUY balance"),
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
