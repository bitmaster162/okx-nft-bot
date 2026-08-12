from __future__ import annotations

from pathlib import Path

from okx_nft_bot.sniper.buyer import OKXInstantBuyer


class _StateStub:
    def record_submit_event(self, **kwargs):
        raise AssertionError("submit event must not be written in blocked test paths")


class _GovernorSequence:
    def __init__(self, gate_results: list[str | None]) -> None:
        self.gate_results = list(gate_results)
        self.gate_calls: list[dict] = []
        self.allocate_calls: list[tuple[str, str]] = []
        self.state = _StateStub()

    def check_live_submit_allowed(self, **kwargs):
        self.gate_calls.append(kwargs)
        if not self.gate_results:
            raise AssertionError("unexpected extra live-gate call")
        return self.gate_results.pop(0)

    def allocate_nonce(self, wallet: str, chain: str):
        self.allocate_calls.append((wallet, chain))
        return 77


class _FakeEth:
    def __init__(self, *, gas_price: int) -> None:
        self.gas_price = gas_price
        self.estimate_calls: list[dict] = []
        self.send_calls: list[object] = []

    def estimate_gas(self, tx: dict) -> int:
        self.estimate_calls.append(dict(tx))
        return 100_000

    def send_raw_transaction(self, raw):
        self.send_calls.append(raw)
        raise AssertionError("transaction broadcast must not run in blocked test paths")


class _FakeW3:
    def __init__(self, *, gas_price: int) -> None:
        self.eth = _FakeEth(gas_price=gas_price)


class _FakeOKXClient:
    def __init__(self, *, settings) -> None:
        self.settings = settings

    def buy_listing(self, *, chain: str, wallet_address: str, order_id: str):
        return {
            "contract_address": "0x" + "9" * 40,
            "input": "0x1234",
            "value": "0",
            "approval_needed": False,
        }


def _price_oracle(currency: str) -> float:
    return {
        "BNB": 600.0,
        "WBNB": 600.0,
        "ETH": 3000.0,
        "WETH": 3000.0,
    }.get(currency.upper(), 0.0)


def _buyer(governor: _GovernorSequence, w3: _FakeW3) -> OKXInstantBuyer:
    buyer = OKXInstantBuyer.__new__(OKXInstantBuyer)
    buyer.enabled = True
    buyer.dry_run = False
    buyer.execution_db_path = Path("/tmp/nonexistent-execution.sqlite3")
    buyer.buyer_address = "0x" + "1" * 40
    buyer.buyer_key = "0x" + "2" * 64
    buyer.gas_multiplier = 1.2
    buyer.max_gas_gwei = 50.0
    buyer._failed_orders = set()
    buyer._w3 = {"eth": w3}
    buyer._governor = governor
    buyer._effective_dry_run = lambda: False
    return buyer


def _patch_dependencies(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", _price_oracle)
    monkeypatch.setattr("okx_nft_bot.config.load_settings", lambda: object())
    monkeypatch.setattr("okx_nft_bot.counterbid.okx_api.OKXAPIClient", _FakeOKXClient)


def test_gas_cap_abort_does_not_reserve_nonce(monkeypatch) -> None:
    _patch_dependencies(monkeypatch)
    governor = _GovernorSequence([None, None])
    w3 = _FakeW3(gas_price=60 * 10**9)
    buyer = _buyer(governor, w3)

    result = buyer._execute_buy(
        {"orderId": "order-high-gas"},
        "eth",
        0.01,
        collection_address="0x" + "4" * 40,
        currency="WETH",
    )

    assert result["success"] is False
    assert result["error"].startswith("Gas too high:")
    assert governor.allocate_calls == []
    assert len(governor.gate_calls) == 2
    assert w3.eth.estimate_calls == []
    assert w3.eth.send_calls == []


def test_final_gate_flip_blocks_before_nonce_and_broadcast(monkeypatch) -> None:
    _patch_dependencies(monkeypatch)
    governor = _GovernorSequence([None, None, "live arm required"])
    w3 = _FakeW3(gas_price=20 * 10**9)
    buyer = _buyer(governor, w3)

    result = buyer._execute_buy(
        {"orderId": "order-disarmed"},
        "eth",
        0.01,
        collection_address="0x" + "5" * 40,
        currency="WETH",
    )

    assert result == {
        "success": False,
        "error": "LIVE_GATE_BLOCKED:live arm required",
    }
    assert governor.allocate_calls == []
    assert len(governor.gate_calls) == 3
    assert len(w3.eth.estimate_calls) == 1
    assert "nonce" not in w3.eth.estimate_calls[0]
    assert w3.eth.send_calls == []
