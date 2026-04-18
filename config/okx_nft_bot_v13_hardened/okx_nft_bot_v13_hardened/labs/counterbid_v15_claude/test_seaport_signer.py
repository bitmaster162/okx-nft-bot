"""
test_seaport_signer.py  –  Parasite-Killer v14  minimal test suite
===================================================================
Tests are fully offline (no real RPC, no real signing key).
Run with:  pytest test_seaport_signer.py -v
"""

import hashlib
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account

# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from seaport_signer import (
    CHAIN_ID,
    CONDUIT_KEY,
    SEAPORT_ADDRESS,
    WBNB_ADDRESS,
    ZERO_ADDRESS,
    ZERO_BYTES32,
    ItemType,
    OrderType,
    build_order_payload,
    cancel_order,
    get_counter,
    sign_order,
    submit_order,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ACCOUNT     = Account.from_key(TEST_PRIVATE_KEY)
TEST_OFFERER     = TEST_ACCOUNT.address          # 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266

TEST_COLLECTION  = "0x1234567890123456789012345678901234567890"
TEST_PRICE_BNB   = 0.05
TEST_PRICE_WEI   = int(TEST_PRICE_BNB * 10**18)  # 50000000000000000
TEST_COUNTER     = 7


# ---------------------------------------------------------------------------
# build_order_payload tests
# ---------------------------------------------------------------------------
class TestBuildOrderPayload:
    def test_returns_dict(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert isinstance(p, dict)

    def test_offerer_field(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert p["offerer"] == TEST_OFFERER

    def test_offer_is_wbnb(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert len(p["offer"]) == 1
        offer = p["offer"][0]
        assert offer["token"] == WBNB_ADDRESS
        assert int(offer["itemType"]) == int(ItemType.ERC20)
        assert offer["startAmount"] == str(TEST_PRICE_WEI)
        assert offer["endAmount"]   == str(TEST_PRICE_WEI)

    def test_consideration_is_collection(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert len(p["consideration"]) == 1
        c = p["consideration"][0]
        assert c["token"] == TEST_COLLECTION
        assert int(c["itemType"]) == int(ItemType.ERC721_CRITERIA)
        assert int(c["identifierOrCriteria"]) == 0  # any token id
        assert c["recipient"] == TEST_OFFERER

    def test_order_type_full_open(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert int(p["orderType"]) == int(OrderType.FULL_OPEN)

    def test_counter_embedded(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert int(p["counter"]) == TEST_COUNTER

    def test_conduit_key_default(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert p["conduitKey"] == CONDUIT_KEY

    def test_times_are_valid(self):
        before = int(time.time()) - 2
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        after  = int(time.time()) + 2
        assert before <= int(p["startTime"]) <= after
        assert int(p["endTime"]) > int(p["startTime"])

    def test_salt_is_int_string(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert p["salt"].isdigit()

    def test_salt_custom(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, salt=12345)
        assert int(p["salt"]) == 12345

    def test_custom_duration(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, duration_s=3600)
        diff = int(p["endTime"]) - int(p["startTime"])
        assert 3590 <= diff <= 3610  # allow small clock drift

    def test_zero_price_accepted(self):
        """Zero-price payload is structurally valid (business logic may reject it)."""
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, 0, TEST_COUNTER)
        assert int(p["offer"][0]["startAmount"]) == 0

    def test_total_original_consideration(self):
        p = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER)
        assert p["totalOriginalConsiderationItems"] == len(p["consideration"])


# ---------------------------------------------------------------------------
# sign_order tests
# ---------------------------------------------------------------------------
class TestSignOrder:
    def _payload(self):
        return build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, salt=99)

    def test_returns_hex_string(self):
        sig = sign_order(self._payload(), TEST_PRIVATE_KEY)
        assert isinstance(sig, str)
        assert sig.startswith("0x")

    def test_signature_length(self):
        sig = sign_order(self._payload(), TEST_PRIVATE_KEY)
        # 65 bytes = 130 hex chars + "0x" prefix
        assert len(sig) == 132

    def test_signature_is_deterministic(self):
        """Same payload + same key → same signature."""
        p   = self._payload()
        s1  = sign_order(p, TEST_PRIVATE_KEY)
        s2  = sign_order(p, TEST_PRIVATE_KEY)
        assert s1 == s2

    def test_different_price_different_signature(self):
        p1  = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, TEST_COUNTER, salt=1)
        p2  = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI * 2, TEST_COUNTER, salt=1)
        s1  = sign_order(p1, TEST_PRIVATE_KEY)
        s2  = sign_order(p2, TEST_PRIVATE_KEY)
        assert s1 != s2

    def test_recovers_correct_signer(self):
        """Verify the recovered signer matches the test account."""
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from seaport_signer import EIP712_DOMAIN, EIP712_TYPES, _to_typed_data_message

        p = self._payload()
        sig = sign_order(p, TEST_PRIVATE_KEY)

        structured = {
            "types":       EIP712_TYPES,
            "domain":      EIP712_DOMAIN,
            "primaryType": "OrderComponents",
            "message":     _to_typed_data_message(p),
        }
        encoded   = encode_typed_data(full_message=structured)
        recovered = Account.recover_message(encoded, signature=bytes.fromhex(sig[2:]))
        assert recovered.lower() == TEST_OFFERER.lower()


# ---------------------------------------------------------------------------
# get_counter tests  (mocked RPC)
# ---------------------------------------------------------------------------
class TestGetCounter:
    def _mock_response(self, counter_int: int) -> MagicMock:
        m = MagicMock()
        m.json.return_value = {
            "jsonrpc": "2.0",
            "id":      1,
            "result":  hex(counter_int),
        }
        m.raise_for_status = MagicMock()
        return m

    @patch("seaport_signer.requests.post")
    def test_returns_int(self, mock_post):
        mock_post.return_value = self._mock_response(42)
        result = get_counter(TEST_OFFERER)
        assert result == 42

    @patch("seaport_signer.requests.post")
    def test_zero_counter(self, mock_post):
        mock_post.return_value = self._mock_response(0)
        assert get_counter(TEST_OFFERER) == 0

    @patch("seaport_signer.requests.post")
    def test_large_counter(self, mock_post):
        big = 2**64 - 1
        mock_post.return_value = self._mock_response(big)
        assert get_counter(TEST_OFFERER) == big

    @patch("seaport_signer.requests.post")
    def test_calls_seaport_address(self, mock_post):
        mock_post.return_value = self._mock_response(1)
        get_counter(TEST_OFFERER)
        call_args = mock_post.call_args
        payload   = call_args[1]["json"]
        assert payload["params"][0]["to"] == SEAPORT_ADDRESS

    @patch("seaport_signer.requests.post")
    def test_rpc_error_raises(self, mock_post):
        m = MagicMock()
        m.json.return_value = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "execution reverted"}}
        m.raise_for_status = MagicMock()
        mock_post.return_value = m
        with pytest.raises(RuntimeError, match="RPC error"):
            get_counter(TEST_OFFERER)


# ---------------------------------------------------------------------------
# Stub tests
# ---------------------------------------------------------------------------
class TestStubs:
    def test_submit_order_returns_stub(self):
        result = submit_order({"dummy": True}, dry_run=True)
        assert result["stub"] is True
        assert result["dry_run"] is True

    def test_cancel_order_returns_stub(self):
        result = cancel_order("0xdeadbeef", dry_run=True)
        assert result["stub"] is True
        assert result["dry_run"] is True

    def test_submit_order_dry_run_false_raises(self):
        import seaport_signer as sm
        original = sm.EXECUTION_ENABLED
        sm.EXECUTION_ENABLED = True
        try:
            with pytest.raises(RuntimeError, match="live execution is disabled"):
                submit_order({"dummy": True}, dry_run=False)
        finally:
            sm.EXECUTION_ENABLED = original

    def test_cancel_order_dry_run_false_raises(self):
        import seaport_signer as sm
        original = sm.EXECUTION_ENABLED
        sm.EXECUTION_ENABLED = True
        try:
            with pytest.raises(RuntimeError, match="live execution is disabled"):
                cancel_order("0xdeadbeef", dry_run=False)
        finally:
            sm.EXECUTION_ENABLED = original


# ---------------------------------------------------------------------------
# Integration: build + sign → verify
# ---------------------------------------------------------------------------
class TestIntegration:
    @patch("seaport_signer.requests.post")
    def test_full_flow(self, mock_post):
        """build_order_payload → sign_order → verify recovered address."""
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from seaport_signer import EIP712_DOMAIN, EIP712_TYPES, _to_typed_data_message

        # Mock RPC
        m = MagicMock()
        m.json.return_value = {"result": hex(5)}
        m.raise_for_status = MagicMock()
        mock_post.return_value = m

        counter = get_counter(TEST_OFFERER)
        payload = build_order_payload(TEST_OFFERER, TEST_COLLECTION, TEST_PRICE_WEI, counter, salt=777)
        sig     = sign_order(payload, TEST_PRIVATE_KEY)

        # Verify
        structured = {
            "types":       EIP712_TYPES,
            "domain":      EIP712_DOMAIN,
            "primaryType": "OrderComponents",
            "message":     _to_typed_data_message(payload),
        }
        encoded   = encode_typed_data(full_message=structured)
        recovered = Account.recover_message(encoded, signature=bytes.fromhex(sig[2:]))
        assert recovered.lower() == TEST_OFFERER.lower()
        assert counter == 5
