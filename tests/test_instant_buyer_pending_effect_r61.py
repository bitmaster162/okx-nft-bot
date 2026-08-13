from __future__ import annotations

import threading
from pathlib import Path

from eth_account import Account
from web3 import Web3

from okx_nft_bot.sniper.buyer import OKXInstantBuyer


class _StateStub:
    def __init__(self) -> None:
        self.submit_events: list[dict] = []

    def record_submit_event(self, **kwargs) -> None:
        self.submit_events.append(dict(kwargs))

    def set_force_dry_run(self, *args, **kwargs) -> None:
        raise AssertionError("force dry-run should not be needed in this regression")


class _Governor:
    def __init__(self) -> None:
        self.state = _StateStub()
        self.gate_calls: list[dict] = []
        self.allocate_calls: list[tuple[str, str]] = []
        self._nonce = 70

    def check_live_submit_allowed(self, **kwargs):
        self.gate_calls.append(dict(kwargs))
        return None

    def allocate_nonce(self, wallet: str, chain: str) -> int:
        self.allocate_calls.append((wallet, chain))
        self._nonce += 1
        return self._nonce


class _Hash:
    def __init__(self, value: str) -> None:
        self.value = value

    def hex(self) -> str:
        return self.value


class _FakeEth:
    def __init__(self) -> None:
        self.gas_price = 20 * 10**9
        self.send_calls: list[object] = []
        self.wait_calls: list[object] = []

    def estimate_gas(self, tx: dict) -> int:
        return 100_000

    def send_raw_transaction(self, raw):
        self.send_calls.append(raw)
        return _Hash(f"0x{len(self.send_calls):064x}")

    def wait_for_transaction_receipt(self, tx_hash, timeout: int):
        self.wait_calls.append(tx_hash)
        raise TimeoutError("receipt unavailable")


class _FakeW3:
    def __init__(self) -> None:
        self.eth = _FakeEth()


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


class _Signed:
    raw_transaction = b"signed-r61"


def _price_oracle(currency: str) -> float:
    return {
        "BNB": 600.0,
        "WBNB": 600.0,
        "ETH": 3000.0,
        "WETH": 3000.0,
    }.get(currency.upper(), 0.0)


def _buyer(governor: _Governor, w3: _FakeW3) -> OKXInstantBuyer:
    buyer = OKXInstantBuyer.__new__(OKXInstantBuyer)
    buyer.enabled = True
    buyer.dry_run = False
    buyer.execution_db_path = Path("/tmp/nonexistent-execution-r61.sqlite3")
    buyer.buyer_address = "0x" + "1" * 40
    buyer.buyer_key = "0x" + "2" * 64
    buyer.gas_multiplier = 1.2
    buyer.max_gas_gwei = 50.0
    buyer._failed_orders = set()
    buyer._pending_orders = set()
    buyer._lock = threading.Lock()
    buyer._w3 = {"eth": w3}
    buyer._governor = governor
    buyer._effective_dry_run = lambda: False
    return buyer


def _patch_dependencies(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", _price_oracle)
    monkeypatch.setattr("okx_nft_bot.config.load_settings", lambda: object())
    monkeypatch.setattr("okx_nft_bot.counterbid.okx_api.OKXAPIClient", _FakeOKXClient)
    monkeypatch.setattr(Web3, "to_checksum_address", staticmethod(lambda value: value))
    monkeypatch.setattr(Account, "sign_transaction", staticmethod(lambda tx, key: _Signed()))


def test_receipt_timeout_latches_order_and_blocks_duplicate_broadcast(monkeypatch) -> None:
    _patch_dependencies(monkeypatch)
    governor = _Governor()
    w3 = _FakeW3()
    buyer = _buyer(governor, w3)
    listing = {"orderId": "order-r61-timeout"}
    collection = "0x" + "4" * 40

    first = buyer._execute_buy(
        listing,
        "eth",
        0.01,
        collection_address=collection,
        currency="WETH",
    )
    second = buyer._execute_buy(
        listing,
        "eth",
        0.01,
        collection_address=collection,
        currency="WETH",
    )

    assert first["success"] is False
    assert first.get("pending") is True
    assert first["error"] == "RECEIPT_TIMEOUT"
    assert len(w3.eth.send_calls) == 1
    assert "pending" in str(second.get("error", "")).lower()
    assert second["success"] is False
    assert "order-r61-timeout" in buyer._pending_orders
    assert len(governor.state.submit_events) == 1
