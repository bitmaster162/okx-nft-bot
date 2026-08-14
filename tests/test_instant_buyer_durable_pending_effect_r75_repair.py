from __future__ import annotations

import threading
from pathlib import Path

from eth_account import Account
from web3 import Web3

from okx_nft_bot.sniper.buyer import OKXInstantBuyer
from okx_nft_bot.undercutter.state import PositionState


class _Governor:
    def __init__(self, db_path: Path) -> None:
        self.state = PositionState(db_path)
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

    def estimate_gas(self, tx: dict) -> int:
        return 100_000

    def send_raw_transaction(self, raw):
        self.send_calls.append(raw)
        return _Hash(f"tx-r75-{len(self.send_calls)}")

    def wait_for_transaction_receipt(self, tx_hash, timeout: int):
        raise TimeoutError("receipt unavailable")


class _FakeW3:
    def __init__(self) -> None:
        self.eth = _FakeEth()


class _FakeOKXClient:
    def __init__(self, *, settings) -> None:
        self.settings = settings

    def buy_listing(self, *, chain: str, wallet_address: str, order_id: str):
        return {
            "contract_address": "market-contract-r75",
            "input": "0x1234",
            "value": "0",
            "approval_needed": False,
        }


class _Signed:
    raw_transaction = b"signed-r75"


def _price_oracle(currency: str) -> float:
    return {
        "BNB": 600.0,
        "WBNB": 600.0,
        "ETH": 3000.0,
        "WETH": 3000.0,
    }.get(currency.upper(), 0.0)


def _buyer(db_path: Path, w3: _FakeW3) -> OKXInstantBuyer:
    buyer = OKXInstantBuyer.__new__(OKXInstantBuyer)
    buyer.enabled = True
    buyer.dry_run = False
    buyer.execution_db_path = db_path
    buyer.buyer_address = "buyer-wallet-r75"
    buyer.buyer_key = object()
    buyer.gas_multiplier = 1.2
    buyer.max_gas_gwei = 50.0
    buyer._failed_orders = set()
    buyer._pending_orders = set()
    buyer._lock = threading.Lock()
    buyer._w3 = {"eth": w3}
    buyer._governor = _Governor(db_path)
    buyer._effective_dry_run = lambda: False
    return buyer


def _patch_dependencies(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", _price_oracle)
    monkeypatch.setattr("okx_nft_bot.config.load_settings", lambda: object())
    monkeypatch.setattr("okx_nft_bot.counterbid.okx_api.OKXAPIClient", _FakeOKXClient)
    monkeypatch.setattr(Web3, "to_checksum_address", staticmethod(lambda value: value))
    monkeypatch.setattr(Account, "sign_transaction", staticmethod(lambda tx, key: _Signed()))


def test_receipt_timeout_blocks_same_order_after_process_restart(monkeypatch, tmp_path) -> None:
    _patch_dependencies(monkeypatch)
    db_path = tmp_path / "execution-r75.sqlite3"
    listing = {"orderId": "order-r75-timeout"}
    collection = "collection-r75"

    first_w3 = _FakeW3()
    first_buyer = _buyer(db_path, first_w3)
    first = first_buyer._execute_buy(
        listing,
        "eth",
        0.01,
        collection_address=collection,
        currency="WETH",
    )

    assert first["success"] is False
    assert first.get("pending") is True
    assert first["error"] == "RECEIPT_TIMEOUT"
    assert len(first_w3.eth.send_calls) == 1

    second_w3 = _FakeW3()
    restarted_buyer = _buyer(db_path, second_w3)
    second = restarted_buyer._execute_buy(
        listing,
        "eth",
        0.01,
        collection_address=collection,
        currency="WETH",
    )

    assert second["success"] is False
    assert second.get("pending") is True
    assert "pending" in str(second.get("error", "")).lower()
    assert len(second_w3.eth.send_calls) == 0
