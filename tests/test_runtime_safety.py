from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient, SEAPORT_ADDRESS_ETH
from okx_nft_bot.sniper.counter_bidder import CounterBidder, RivalOffer, _finite_positive


class _Prices:
    def __init__(self, weth_usd: float = 1.0):
        self.weth_usd = float(weth_usd)

    def to_usd(self, amount: float, currency: str) -> float:
        cur = (currency or "").upper()
        return float(amount) * (self.weth_usd if cur in {"ETH", "WETH"} else 1.0)

    def from_usd(self, amount: float, currency: str) -> float:
        cur = (currency or "").upper()
        divisor = self.weth_usd if cur in {"ETH", "WETH"} else 1.0
        return float(amount) / divisor


def _bare_bidder() -> CounterBidder:
    bidder = CounterBidder.__new__(CounterBidder)
    bidder.prices = _Prices()
    return bidder


def _runtime_bidder(addr: str) -> CounterBidder:
    bidder = _bare_bidder()
    bidder.buy_config = {
        "collections": {
            addr: {
                "max_offer_price": 10.0,
                "currency": "WETH",
            }
        }
    }
    bidder.offer_currencies = ["WETH"]
    bidder.protected_wallets = set()
    bidder._friend_skipped_count = 0
    bidder.max_usd = 1.0
    bidder.max_usd_eth = 1.0
    bidder.nonwl_max_usd = 0.001
    bidder.budget_wl_usd = 1.0
    bidder.budget_nonwl_usd = 1.0
    bidder.max_qty = 10
    bidder.undercut_bps = 50
    bidder.dry_run = True
    bidder._last_placed = {}
    bidder._PLACE_COOLDOWN = 600
    bidder._placed_qty_cache = {}
    bidder.already_winning = 0
    bidder._fetch_best_offer_maker = lambda *_args, **_kwargs: {}
    bidder._get_max_price = lambda *_args, **_kwargs: 10.0
    bidder._antiwash_flag = lambda *_args, **_kwargs: None
    bidder._is_trap = lambda *_args, **_kwargs: 0
    bidder._fetch_floor_price = lambda *_args, **_kwargs: 0.0
    bidder._we_already_best = lambda *_args, **_kwargs: False
    bidder._find_our_offer = lambda *_args, **_kwargs: None
    bidder._cancel_existing_offer = lambda *_args, **_kwargs: True
    return bidder


@pytest.mark.parametrize(
    "value",
    [None, "", "not-a-number", 0, 0.0, -1, float("nan"), float("inf"), -float("inf")],
)
def test_finite_positive_rejects_invalid_values(value):
    assert _finite_positive(value) is False


@pytest.mark.parametrize("value", [1, 0.000001, "2.5"])
def test_finite_positive_accepts_positive_finite_values(value):
    assert _finite_positive(value) is True


def test_balance_cap_disabled_uses_env_cap_without_balance_lookup(monkeypatch):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "0")
    bidder = _bare_bidder()
    bidder._get_currency_address = lambda *_args: pytest.fail("currency lookup must not run")
    bidder._get_balance = lambda *_args: pytest.fail("balance lookup must not run")

    assert bidder._resolve_balance_cap("WETH", "eth", 3.0) == 3.0


def test_balance_cap_fails_closed_on_invalid_ratio(monkeypatch):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "1")
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_RATIO", "0")
    bidder = _bare_bidder()
    bidder._get_currency_address = lambda *_args: pytest.fail("currency lookup must not run")

    assert bidder._resolve_balance_cap("WETH", "eth", 3.0) is bidder.BALANCE_CAP_UNAVAILABLE


@pytest.mark.parametrize("balance", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_balance_cap_fails_closed_on_invalid_balance(monkeypatch, balance):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "1")
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_RATIO", "0.95")
    bidder = _bare_bidder()
    bidder._get_currency_address = lambda *_args: "0x" + "1" * 40
    bidder._get_balance = lambda *_args: balance

    assert bidder._resolve_balance_cap("WETH", "eth", 3.0) is bidder.BALANCE_CAP_UNAVAILABLE


def test_balance_cap_clamps_to_balance_ratio_and_env_ceiling(monkeypatch):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "1")
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_RATIO", "0.95")
    bidder = _bare_bidder()
    bidder._get_currency_address = lambda *_args: "0x" + "1" * 40
    bidder._get_balance = lambda *_args: 5.0
    bidder.prices = _Prices(weth_usd=1.0)

    assert bidder._resolve_balance_cap("WETH", "eth", 10.0) == pytest.approx(4.75)
    assert bidder._resolve_balance_cap("WETH", "eth", 3.0) == pytest.approx(3.0)
    assert bidder._resolve_balance_cap("WETH", "eth", 0.0) == pytest.approx(4.75)


def test_undercut_fails_closed_without_submit_when_balance_unavailable(monkeypatch):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "1")
    addr = ("0x" + "2" * 40).lower()
    bidder = _runtime_bidder(addr)
    bidder.buy_config = {"collections": {}}
    bidder.max_usd_eth = 3.0
    bidder._resolve_balance_cap = lambda *_args, **_kwargs: bidder.BALANCE_CAP_UNAVAILABLE
    bidder._submit_undercut = lambda *_args, **_kwargs: pytest.fail("OKX submit must not run")
    bidder._mirror_to_opensea = lambda *_args, **_kwargs: pytest.fail("OpenSea mirror must not run")

    result = bidder._undercut_collection(addr, [], "eth", is_wl=True)

    assert result.our_offers_placed == 0
    assert result.our_offers_failed == 0
    assert result.our_offers_skipped == 1


def test_collection_cap_cannot_raise_global_cap(monkeypatch):
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_ENABLED", "1")
    addr = ("0x" + "3" * 40).lower()
    bidder = _runtime_bidder(addr)
    bidder._resolve_balance_cap = lambda *_args, **_kwargs: 1.0
    observed = {}

    def capture_alert(*args, **kwargs):
        observed["our_usd"] = args[8]
        observed["our_price"] = args[4]

    bidder._alert_undercut = capture_alert
    offer = RivalOffer(
        collection_address=addr,
        collection_name="cap-clamp",
        chain="eth",
        token_id="",
        price=2.0,
        currency="WETH",
        offer_id="rival",
        source_type="collection_offer",
        maker="0x" + "4" * 40,
    )

    result = bidder._undercut_collection(addr, [offer], "eth", is_wl=True)

    assert result.our_offers_placed == 1
    assert observed["our_usd"] == pytest.approx(1.0)
    assert observed["our_price"] == pytest.approx(1.0)


@pytest.mark.parametrize("okx_success, expected_mirrors", [(False, 0), (True, 1)])
def test_opensea_mirror_runs_only_after_successful_okx_submit(okx_success, expected_mirrors):
    bidder = _bare_bidder()
    mirrors = []
    bidder._submit_eth = lambda *_args, **_kwargs: okx_success
    bidder._mirror_to_opensea = lambda *args, **kwargs: mirrors.append((args, kwargs)) or True

    result = bidder._submit_undercut(
        "0x" + "5" * 40,
        "",
        0.000101,
        "WETH",
        "eth",
        quantity=1,
        duration_hours=720,
    )

    assert result is okx_success
    assert len(mirrors) == expected_mirrors


class _OpenSeaCapture:
    def __init__(self):
        self.calls = []

    def create_opensea_offer(self, **kwargs):
        self.calls.append(kwargs)
        return {"order_id": "test-order"}


def _mirror_bidder() -> tuple[CounterBidder, _OpenSeaCapture]:
    bidder = _bare_bidder()
    capture = _OpenSeaCapture()
    bidder.opensea_enabled = True
    bidder.opensea_client = capture
    bidder._get_currency_address = lambda *_args: "0x" + "6" * 40
    bidder._get_balance = lambda *_args: 10.0
    bidder._record_execution_submit_event = lambda *_args, **_kwargs: None
    return bidder, capture


def test_opensea_tick_rounds_up_before_submit(monkeypatch):
    monkeypatch.setenv("OPENSEA_OFFERS_ENABLED", "1")
    monkeypatch.setenv("OPENSEA_DRY_RUN", "0")
    monkeypatch.setenv("OPENSEA_COOLDOWN_S", "0")
    monkeypatch.setenv("OPENSEA_TICK_WETH", "0.0001")
    monkeypatch.setenv("COUNTERBID_MAX_USD_ETH", "100")
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_RATIO", "0.95")
    bidder, capture = _mirror_bidder()
    bidder.prices = _Prices(weth_usd=3000.0)

    assert bidder._mirror_to_opensea("0x" + "7" * 40, 0.000101, "WETH", 1) is True
    assert len(capture.calls) == 1
    assert capture.calls[0]["price_wei"] == 200_000_000_000_000


def test_opensea_tick_refuses_round_up_above_cap(monkeypatch):
    monkeypatch.setenv("OPENSEA_OFFERS_ENABLED", "1")
    monkeypatch.setenv("OPENSEA_DRY_RUN", "0")
    monkeypatch.setenv("OPENSEA_COOLDOWN_S", "0")
    monkeypatch.setenv("OPENSEA_TICK_WETH", "0.0001")
    monkeypatch.setenv("COUNTERBID_MAX_USD_ETH", "0.5")
    monkeypatch.setenv("COUNTERBID_BALANCE_CAP_RATIO", "0.95")
    bidder, capture = _mirror_bidder()
    bidder.prices = _Prices(weth_usd=3000.0)

    assert bidder._mirror_to_opensea("0x" + "8" * 40, 0.000101, "WETH", 1) is False
    assert capture.calls == []


def test_opensea_order_shape_has_signed_zone_fee_and_matching_count(monkeypatch):
    monkeypatch.delenv("OPENSEA_ZONE", raising=False)
    monkeypatch.delenv("OPENSEA_FEE_BPS", raising=False)
    monkeypatch.delenv("OPENSEA_FEE_RECIPIENT", raising=False)
    client = OpenSeaClient.__new__(OpenSeaClient)
    offerer = "0x" + "9" * 40
    collection = "0x" + "a" * 40
    currency = "0x" + "b" * 40
    price_wei = 10**18

    params = client._build_seaport_offer(
        offerer=offerer,
        collection_address=collection,
        token_id=None,
        price_wei=price_wei,
        currency_address=currency,
        counter=0,
        valid_time=2_000_000_000,
    )

    assert params["zone"] != "0x" + "0" * 40
    assert params["orderType"] == 2
    assert params["totalOriginalConsiderationItems"] == len(params["consideration"]) == 2
    fee = params["consideration"][1]
    assert fee["itemType"] == 1
    assert fee["token"] == currency
    assert int(fee["startAmount"]) == price_wei // 100
    assert fee["startAmount"] == fee["endAmount"]


class _SubmitSettings:
    @property
    def opensea_api_key(self):
        return True

    @property
    def opensea_api_base(self):
        return "https://example.invalid"


class _TransportCapture:
    def __init__(self):
        self.body = None

    def request_json(self, **kwargs):
        self.body = json.loads(kwargs["body"])
        return {"order_hash": "test-hash"}


def test_opensea_submit_body_includes_seaport_protocol_address():
    client = OpenSeaClient.__new__(OpenSeaClient)
    transport = _TransportCapture()
    client.settings = _SubmitSettings()
    client.transport = transport
    params = {
        "offerer": "0x" + "c" * 40,
        "offer": [{"startAmount": "1"}],
    }

    result = client._submit_opensea_offer(params, "test-signature", chain="eth")

    assert result["status"] == "submitted"
    assert transport.body["parameters"] == params
    assert transport.body["protocol_address"] == SEAPORT_ADDRESS_ETH
