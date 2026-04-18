from __future__ import annotations

from unittest.mock import patch

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from okx_nft_bot.config import Settings
from okx_nft_bot.signing.seaport_signer import (
    CONDUIT_KEY,
    EIP712_DOMAIN,
    EIP712_TYPES,
    ItemType,
    OrderType,
    SignedOrder,
    WBNB_ADDRESS,
    _to_typed_data_message,
    build_order_payload,
    cancel_order,
    get_counter,
    preview_counterbid,
    sign_order,
    submit_order,
)

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)
TEST_COLLECTION = "0x1234567890123456789012345678901234567890"
TEST_PRICE_BNB = 0.05
TEST_PRICE_WEI = int(TEST_PRICE_BNB * 10**18)
TEST_COUNTER = 7


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
    )


def test_build_order_payload_shape() -> None:
    payload = build_order_payload(TEST_ACCOUNT.address, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
    assert payload["offerer"] == TEST_ACCOUNT.address
    assert payload["conduitKey"] == CONDUIT_KEY
    assert int(payload["counter"]) == TEST_COUNTER
    assert int(payload["orderType"]) == int(OrderType.FULL_RESTRICTED)
    assert payload["offer"][0]["token"] == WBNB_ADDRESS
    assert int(payload["offer"][0]["itemType"]) == int(ItemType.ERC20)
    assert payload["consideration"][0]["token"] == TEST_COLLECTION
    assert int(payload["consideration"][0]["itemType"]) == int(ItemType.ERC721_CRITERIA)


def test_sign_order_returns_prefixed_signature() -> None:
    payload = build_order_payload(TEST_ACCOUNT.address, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, salt=99)
    signature = sign_order(payload, TEST_PRIVATE_KEY)
    assert signature.startswith("0x")
    assert len(signature) == 132


def test_sign_order_recovers_expected_address() -> None:
    payload = build_order_payload(TEST_ACCOUNT.address, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, salt=777)
    signature = sign_order(payload, TEST_PRIVATE_KEY)
    structured = {
        "types": EIP712_TYPES,
        "domain": EIP712_DOMAIN,
        "primaryType": "OrderComponents",
        "message": _to_typed_data_message(payload),
    }
    encoded = encode_typed_data(full_message=structured)
    recovered = Account.recover_message(encoded, signature=bytes.fromhex(signature[2:]))
    assert recovered.lower() == TEST_ACCOUNT.address.lower()


def test_get_counter_reads_rpc_hex_value() -> None:
    with patch("okx_nft_bot.signing.seaport_signer._rpc_request_json", return_value={"result": hex(42)}):
        assert get_counter(TEST_ACCOUNT.address) == 42


def test_preview_counterbid_builds_payload(tmp_path) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.signing.seaport_signer.get_counter", return_value=5):
        payload = preview_counterbid(
            settings=settings,
            collection=TEST_COLLECTION,
            price_bnb=TEST_PRICE_BNB,
            chain="bsc",
        )
    assert payload["dry_run"] is True
    assert payload["collection"] == TEST_COLLECTION
    assert payload["counter"] == 5
    assert payload["signature"].startswith("0x")


def test_preview_counterbid_rejects_non_bsc(tmp_path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="Only 'bsc'"):
        preview_counterbid(settings=settings, collection=TEST_COLLECTION, price_bnb=TEST_PRICE_BNB, chain="eth")


def test_submit_and_cancel_are_dry_run_stubs() -> None:
    signed = SignedOrder(parameters={"foo": "bar"}, signature="0xabc")
    submit = submit_order(signed, dry_run=True)
    cancel = cancel_order("0xdeadbeef", dry_run=True)
    assert submit["stub"] is True
    assert cancel["stub"] is True


def test_submit_and_cancel_reject_live_mode() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        submit_order(SignedOrder(parameters={}, signature="0xabc"), dry_run=False)
    with pytest.raises(RuntimeError, match="disabled"):
        cancel_order("0xdeadbeef", dry_run=False)
