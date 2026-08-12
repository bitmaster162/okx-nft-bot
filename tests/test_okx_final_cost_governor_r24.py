from __future__ import annotations

import json

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.counterbid.submit_safety import (
    _buy_price_bnb_equiv,
    install_submit_safety,
)


WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


class _Governor:
    calls = []
    block_priced = None

    def __init__(self, *, settings, api_client=None, **_kwargs):
        self.settings = settings
        self.api_client = api_client

    def check_live_submit_allowed(self, **kwargs):
        self.__class__.calls.append(kwargs)
        if float(kwargs.get("price_bnb") or 0.0) > 0:
            return self.__class__.block_priced
        return None


def _install_governor(monkeypatch, *, block_priced=None):
    import okx_nft_bot.counterbid.submit_safety as safety
    import okx_nft_bot.execution_governor as governor_module

    _Governor.calls = []
    _Governor.block_priced = block_priced
    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)
    monkeypatch.setattr(safety, "_read_seaport_counter", lambda *_a, **_k: 7)


def _buy_parameters(*, token=WBNB, amount=10**18, counter=7):
    wallet = "0x" + "1" * 40
    return {
        "offerer": wallet,
        "counter": str(counter),
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
                "token": WBNB,
                "identifierOrCriteria": 0,
                "startAmount": str(10**18),
                "endAmount": str(10**18),
                "recipient": wallet,
            }
        ],
    }


def _payload(*, parameters, chain=56):
    wallet = parameters["offerer"]
    return {
        "chain": chain,
        "walletAddress": wallet,
        "items": [
            {
                "protocolData": json.dumps(
                    {"parameters": parameters, "signature": "0xsig"}
                )
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


def _prices(monkeypatch, mapping):
    import okx_nft_bot.prices as prices

    monkeypatch.setattr(prices, "get_usd_price", lambda symbol: mapping.get(symbol, 0.0))


def test_real_client_has_r24_priced_governor_guard_installed():
    assert getattr(OKXAPIClient._request, "_r24_priced_governor_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r23_counter_guard", False) is True


def test_wbnb_raw_amount_normalizes_to_bnb_and_usd(monkeypatch):
    _prices(monkeypatch, {"BNB": 600.0, "WBNB": 600.0})

    price_bnb, price_usd = _buy_price_bnb_equiv(
        chain_name="bsc",
        requirements={WBNB: 10**18},
    )

    assert price_bnb == pytest.approx(1.0)
    assert price_usd == pytest.approx(600.0)


def test_weth_raw_amount_normalizes_through_usd_to_bnb(monkeypatch):
    _prices(monkeypatch, {"BNB": 600.0, "WETH": 3000.0})

    price_bnb, price_usd = _buy_price_bnb_equiv(
        chain_name="eth",
        requirements={WETH: 10**17},
    )

    assert price_bnb == pytest.approx(0.5)
    assert price_usd == pytest.approx(300.0)


def test_bsc_usdt_uses_chain_specific_18_decimals(monkeypatch):
    _prices(monkeypatch, {"BNB": 500.0, "USDT": 1.0})

    price_bnb, price_usd = _buy_price_bnb_equiv(
        chain_name="bsc",
        requirements={BSC_USDT: 100 * 10**18},
    )

    assert price_bnb == pytest.approx(0.2)
    assert price_usd == pytest.approx(100.0)


def test_unknown_buy_token_fails_closed(monkeypatch):
    _prices(monkeypatch, {"BNB": 600.0})

    with pytest.raises(RuntimeError, match="unknown ERC20 metadata"):
        _buy_price_bnb_equiv(
            chain_name="bsc",
            requirements={"0x" + "9" * 40: 10**18},
        )


def test_unavailable_bnb_price_fails_closed(monkeypatch):
    _prices(monkeypatch, {"WBNB": 600.0})

    with pytest.raises(RuntimeError, match="BNB/USD price unavailable"):
        _buy_price_bnb_equiv(
            chain_name="bsc",
            requirements={WBNB: 10**18},
        )


def test_buy_runs_zero_cost_then_real_cost_governor_before_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(safety, "_buy_price_bnb_equiv", lambda **_kwargs: (0.25, 150.0))
    monkeypatch.setattr(safety, "_bsc_quota_block_reason", lambda _client: None)
    monkeypatch.setattr(safety, "_read_erc20_balance_raw", lambda *_a, **_k: 10**18)
    Client = _make_client_class()
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_payload(parameters=_buy_parameters()),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1
    assert len(_Governor.calls) == 2
    assert _Governor.calls[0]["price_bnb"] == 0.0
    assert "price_usd" not in _Governor.calls[0]
    assert _Governor.calls[1]["price_bnb"] == pytest.approx(0.25)
    assert _Governor.calls[1]["price_usd"] == pytest.approx(150.0)


def test_priced_governor_daily_cap_block_prevents_quota_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(
        monkeypatch,
        block_priced="daily BNB-equivalent cap hit: synthetic",
    )
    monkeypatch.setattr(safety, "_buy_price_bnb_equiv", lambda **_kwargs: (0.25, 150.0))
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("quota must not run after priced governor block"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run after priced governor block"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder priced governor blocked: daily BNB-equivalent cap hit"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_payload(parameters=_buy_parameters()),
        )

    assert client.request_calls == []
    assert len(_Governor.calls) == 2
    assert _Governor.calls[1]["price_bnb"] == pytest.approx(0.25)


def test_price_normalization_failure_blocks_before_quota_balance_and_http(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("oracle unavailable")),
    )
    monkeypatch.setattr(
        safety,
        "_bsc_quota_block_reason",
        lambda _client: pytest.fail("quota must not run after price uncertainty"),
    )
    monkeypatch.setattr(
        safety,
        "_read_erc20_balance_raw",
        lambda *_a, **_k: pytest.fail("balance must not run after price uncertainty"),
    )
    Client = _make_client_class()
    client = Client()

    with pytest.raises(Exception, match="submitOrder price gate blocked: oracle unavailable"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload=_payload(parameters=_buy_parameters()),
        )

    assert client.request_calls == []
    assert len(_Governor.calls) == 1


def test_sell_submit_has_only_zero_cost_governor_pass(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as safety

    _install_governor(monkeypatch)
    monkeypatch.setattr(
        safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: pytest.fail("SELL must not normalize BUY exposure"),
    )
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

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload=_payload(parameters=_sell_parameters()),
    )

    assert result == {"ok": True}
    assert len(client.request_calls) == 1
    assert len(_Governor.calls) == 1
    assert _Governor.calls[0]["price_bnb"] == 0.0
