from __future__ import annotations

from pathlib import Path

import pytest

from okx_nft_bot.clients.http import HTTPStatusError, StdlibHttpTransport
from okx_nft_bot.clients.opensea import SEAPORT_ADDRESS_ETH
from okx_nft_bot.clients.opensea_killswitch import OpenSeaKillSwitchClient
from okx_nft_bot.config import Settings
from okx_nft_bot.killswitch import _cancel_chain
from okx_nft_bot.undercutter.state import PositionState


ORDER_HASH = "0x" + "a" * 64
OKX_HASH = "okx-order-1"
COLLECTION = "0x" + "3" * 40


def _settings(tmp_path: Path, *, api_key: str | None = "api-key") -> Settings:
    settings = Settings(app_env="test", db_path=tmp_path / "main.sqlite3")
    settings.opensea_api_base = "https://api.opensea.test"
    settings.opensea_api_key = api_key
    settings.opensea_request_timeout = 1
    settings.opensea_max_retries = 4
    settings.opensea_rate_limit_per_sec = 1000.0
    return settings


class _Transport:
    def __init__(self, *, readback=None, post_error=None, get_error=None):
        self.readback = readback if readback is not None else {"status": "CANCELLED"}
        self.post_error = post_error
        self.get_error = get_error
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        method = kwargs["method"].upper()
        if method == "POST":
            if self.post_error is not None:
                raise self.post_error
            return {}
        if method == "GET":
            if self.get_error is not None:
                raise self.get_error
            return self.readback
        raise AssertionError(f"unexpected method {method}")


@pytest.mark.parametrize(
    "readback",
    [
        {"status": "CANCELLED"},
        {"order_status": "canceled"},
        {"canceled": True},
        {"order": {"cancelled": True}},
        {"offer": {"status": "cancelled"}},
    ],
)
def test_r46_exact_order_readback_confirms_cancel(tmp_path, readback):
    transport = _Transport(readback=readback)
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    assert client.cancel_offer(ORDER_HASH, chain="eth") is True
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]

    post = transport.calls[0]
    assert post["url"].endswith(
        f"/api/v2/orders/chain/ethereum/protocol/{SEAPORT_ADDRESS_ETH}/{ORDER_HASH}/cancel"
    )
    assert post["headers"]["X-API-KEY"] == "api-key"
    assert post["headers"]["Authorization"] == "Bearer wallet-jwt"
    assert post["body"] == "{}"

    readback_call = transport.calls[1]
    assert readback_call["url"].endswith(
        f"/api/v2/orders/chain/ethereum/protocol/{SEAPORT_ADDRESS_ETH}/{ORDER_HASH}"
    )
    assert readback_call["headers"] == {
        "Accept": "application/json",
        "X-API-KEY": "api-key",
    }


def test_r46_unconfirmed_readback_fails_closed(tmp_path):
    transport = _Transport(readback={"status": "ACTIVE", "canceled": False})
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    with pytest.raises(RuntimeError, match="post-condition not confirmed"):
        client.cancel_offer(ORDER_HASH)

    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_r46_missing_wallet_jwt_blocks_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSEA_WALLET_JWT", raising=False)
    transport = _Transport()
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
    )

    with pytest.raises(RuntimeError, match="OPENSEA_WALLET_JWT"):
        client.cancel_offer(ORDER_HASH)

    assert transport.calls == []


def test_r46_missing_api_key_blocks_before_network(tmp_path):
    transport = _Transport()
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path, api_key=None),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    with pytest.raises(RuntimeError, match="OPENSEA_API_KEY"):
        client.cancel_offer(ORDER_HASH)

    assert transport.calls == []


def test_r46_rejects_non_seaport_hash_before_network(tmp_path):
    transport = _Transport()
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    with pytest.raises(ValueError, match="32-byte"):
        client.cancel_offer("os-order-legacy")

    assert transport.calls == []


def test_r46_rejects_non_ethereum_cancel_before_network(tmp_path):
    transport = _Transport()
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    with pytest.raises(ValueError, match="only supports Ethereum"):
        client.cancel_offer(ORDER_HASH, chain="bsc")

    assert transport.calls == []


class _NoWait:
    def wait(self):
        return None


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self.ok = 200 <= status_code < 300


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected repeated HTTP request")
        return self.responses.pop(0)


def test_r46_production_cancel_post_is_single_attempt_on_503(tmp_path):
    transport = StdlibHttpTransport(timeout=1, max_retries=4, rate_limit_per_sec=1000.0)
    session = _Session([_Response(503, "upstream unavailable"), _Response(200, "late retry")])
    transport._session = session
    transport._rate_limiter = _NoWait()
    client = OpenSeaKillSwitchClient(
        settings=_settings(tmp_path),
        transport=transport,
        wallet_jwt="wallet-jwt",
    )

    with pytest.raises(HTTPStatusError) as exc_info:
        client.cancel_offer(ORDER_HASH)

    assert exc_info.value.status == 503
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "POST"


class _OKX:
    def __init__(self, *, rows=None, lookup_error=None):
        self.rows = list(rows or [])
        self.lookup_error = lookup_error
        self.cancel_calls = []

    def get_my_offers(self, *, chain, require_all_endpoints):
        assert require_all_endpoints is True
        if self.lookup_error is not None:
            raise self.lookup_error
        return list(self.rows)

    def cancel_offer(self, order_hash, *, chain, order_params=None):
        self.cancel_calls.append((order_hash, chain, order_params))
        return True


class _OpenSea:
    def __init__(self, *, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def cancel_offer(self, order_hash, *, chain="eth"):
        self.calls.append((order_hash, chain))
        if self.error is not None:
            raise self.error
        return self.result


def _state(tmp_path):
    return PositionState(tmp_path / "execution.sqlite3")


def _add_opensea(state, *, order_hash=ORDER_HASH):
    state.upsert_active_offer(
        order_hash=order_hash,
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.25,
        status="active",
        preview_payload={
            "marketplace": "opensea",
            "source": "counter_bidder_mirror",
        },
    )


def test_r46_killswitch_routes_opensea_only_to_opensea_adapter(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    okx = _OKX()
    opensea = _OpenSea()

    result = _cancel_chain(
        state=state,
        api=okx,
        opensea_api=opensea,
        chain="eth",
    )

    assert okx.cancel_calls == []
    assert opensea.calls == [(ORDER_HASH, "eth")]
    assert result.active_offers_seen == 1
    assert result.live_cancelled == 1
    assert result.failed == ()
    assert result.failure_count == 0
    assert state.get_active_offers(chain="eth") == []
    assert state.get_killswitch_failed_offers(chain="eth") == []


def test_r46_opensea_cancel_failure_stays_quarantined(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    okx = _OKX()
    opensea = _OpenSea(error=RuntimeError("wallet token denied"))

    result = _cancel_chain(
        state=state,
        api=okx,
        opensea_api=opensea,
        chain="eth",
    )

    assert okx.cancel_calls == []
    assert result.live_cancelled == 0
    assert len(result.failed) == 1
    assert result.failed[0].startswith(f"{ORDER_HASH}:opensea_cancel_failed:")
    failed = state.get_killswitch_failed_offers(chain="eth")
    assert [offer.order_hash for offer in failed] == [ORDER_HASH]
    assert failed[0].preview_payload["marketplace"] == "opensea"


def test_r46_missing_adapter_preserves_r40_fail_closed_behavior(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    okx = _OKX()

    result = _cancel_chain(state=state, api=okx, chain="eth")

    assert okx.cancel_calls == []
    assert result.failed == (f"{ORDER_HASH}:opensea_cancel_unavailable",)
    assert [offer.order_hash for offer in state.get_killswitch_failed_offers(chain="eth")] == [ORDER_HASH]


def test_r46_mixed_marketplaces_never_cross_cancel_adapters(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    state.upsert_active_offer(
        order_hash=OKX_HASH,
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.1,
        status="active",
    )
    okx = _OKX(
        rows=[
            {
                "offerId": OKX_HASH,
                "collectionAddress": COLLECTION,
            }
        ]
    )
    opensea = _OpenSea()

    result = _cancel_chain(
        state=state,
        api=okx,
        opensea_api=opensea,
        chain="eth",
    )

    assert opensea.calls == [(ORDER_HASH, "eth")]
    assert [call[0] for call in okx.cancel_calls] == [OKX_HASH]
    assert result.live_cancelled == 2
    assert result.failed == ()
    assert state.get_active_offers(chain="eth") == []
    assert state.get_killswitch_failed_offers(chain="eth") == []


def test_r46_opensea_cancel_still_runs_when_okx_lookup_fails(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    okx = _OKX(lookup_error=RuntimeError("OKX inventory unavailable"))
    opensea = _OpenSea()

    result = _cancel_chain(
        state=state,
        api=okx,
        opensea_api=opensea,
        chain="eth",
    )

    assert opensea.calls == [(ORDER_HASH, "eth")]
    assert result.live_cancelled == 1
    assert result.exchange_lookup_failed is True
    assert result.failed == ()
    assert state.get_killswitch_failed_offers(chain="eth") == []
