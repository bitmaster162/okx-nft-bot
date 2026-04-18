"""
test_counter_bidder.py  –  Parasite-Killer v15
===============================================
Unit tests for counter-bidder logic.

Mocks:
- ConfigManager (SQLite)
- OKXAPIClient (API calls)
- seaport_signer (signing)
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config import ConfigManager, CollectionConfig
from counter_bidder import CounterBidder, PARASITE_WALLETS


class TestConfigManager(unittest.TestCase):
    """Test config.py — collection storage."""

    def setUp(self):
        """Use temp DB for each test."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.config = ConfigManager(self.db_path)

    def tearDown(self):
        """Clean up temp DB."""
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_add_collection(self):
        """Test adding a collection."""
        cfg = self.config.add_collection(
            address="0xABC123",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
        )
        self.assertEqual(cfg.address.lower(), "0xabc123")
        self.assertEqual(cfg.chain, "bsc")
        self.assertEqual(cfg.min_price_bnb, 0.01)
        self.assertEqual(cfg.max_price_bnb, 10.0)
        self.assertEqual(cfg.margin_bnb, 0.001)

    def test_get_collection(self):
        """Test retrieving a collection."""
        self.config.add_collection(
            address="0xDEF456",
            chain="eth",
            min_price_bnb=0.05,
            max_price_bnb=5.0,
            margin_bnb=0.0001,
        )
        cfg = self.config.get_collection("0xDEF456")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.chain, "eth")

    def test_list_collections(self):
        """Test listing collections."""
        self.config.add_collection(
            address="0xAAA",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
        )
        self.config.add_collection(
            address="0xBBB",
            chain="eth",
            min_price_bnb=0.1,
            max_price_bnb=50.0,
            margin_bnb=0.01,
        )

        all_cfgs = self.config.list_collections()
        self.assertEqual(len(all_cfgs), 2)

        bsc_cfgs = self.config.list_collections(chain="bsc")
        self.assertEqual(len(bsc_cfgs), 1)
        self.assertEqual(bsc_cfgs[0].chain, "bsc")

    def test_remove_collection(self):
        """Test removing a collection."""
        self.config.add_collection(
            address="0xXYZ",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
        )
        result = self.config.remove_collection("0xXYZ")
        self.assertTrue(result)

        cfg = self.config.get_collection("0xXYZ")
        self.assertIsNone(cfg)

    def test_enable_disable(self):
        """Test enabling/disabling collections."""
        self.config.add_collection(
            address="0xTEST",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
            enabled=True,
        )

        # Disable
        self.config.disable_collection("0xTEST")
        cfg = self.config.get_collection("0xTEST")
        self.assertFalse(cfg.enabled)

        # Re-enable
        self.config.enable_collection("0xTEST")
        cfg = self.config.get_collection("0xTEST")
        self.assertTrue(cfg.enabled)


class TestCounterBidder(unittest.TestCase):
    """Test counter_bidder.py logic."""

    def setUp(self):
        """Setup mocked bidder."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.config = ConfigManager(self.db_path)

        # Mock OKX API
        self.okx_mock = MagicMock()
        self.okx_mock.fetch_offers.return_value = []

        self.bidder = CounterBidder(
            config_manager=self.config,
            okx_client=self.okx_mock,
            offerer_address="0xOfferer",
            private_key="0x" + "ff" * 32,
        )

        # Add a test collection
        self.config.add_collection(
            address="0xCollection1",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
        )

    def tearDown(self):
        """Cleanup temp DB."""
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_detect_no_parasite(self):
        """Test when no parasite offers exist."""
        offers = [
            {
                "maker": "0xNormalBidder1",
                "price": "0.5",
            },
            {
                "maker": "0xNormalBidder2",
                "price": "0.3",
            },
        ]
        result = self.bidder.detect_parasite_offers(offers)
        self.assertIsNone(result)

    def test_detect_parasite(self):
        """Test detecting a parasite offer."""
        parasite_addr = list(PARASITE_WALLETS)[0]
        offers = [
            {
                "maker": "0xNormal",
                "price": "0.5",
            },
            {
                "maker": parasite_addr,
                "price": "0.7",  # highest
            },
        ]
        result = self.bidder.detect_parasite_offers(offers)
        self.assertIsNotNone(result)
        self.assertEqual(result["maker"].lower(), parasite_addr.lower())

    def test_detect_parasite_returns_highest(self):
        """Test that highest parasite offer is returned."""
        parasite1 = list(PARASITE_WALLETS)[0]
        parasite2 = list(PARASITE_WALLETS)[1] if len(PARASITE_WALLETS) > 1 else "0xParasite2"

        offers = [
            {"maker": parasite1, "price": "0.5"},
            {"maker": parasite2, "price": "0.8"},  # highest parasite
            {"maker": "0xNormal", "price": "1.0"},  # normal but highest
        ]
        result = self.bidder.detect_parasite_offers(offers)
        self.assertEqual(result["price"], "0.8")

    def test_calculate_counter_price_with_parasite(self):
        """Test counter price calculation when parasite exists."""
        cfg = self.config.get_collection("0xCollection1")
        parasite_price = 0.5

        counter_price, reason = self.bidder.calculate_counter_price(cfg, parasite_price)

        # Should be parasite + margin = 0.5 + 0.001 = 0.501
        self.assertAlmostEqual(counter_price, 0.501, places=6)
        self.assertIn("Parasite", reason)

    def test_calculate_counter_price_no_parasite(self):
        """Test counter price calculation when no parasite."""
        cfg = self.config.get_collection("0xCollection1")

        counter_price, reason = self.bidder.calculate_counter_price(cfg, 0)

        # Should default to min_price
        self.assertAlmostEqual(counter_price, cfg.min_price_bnb, places=6)
        self.assertIn("No parasite", reason)

    def test_calculate_counter_price_respects_limits(self):
        """Test that counter price respects min/max limits."""
        # Create collection with tight limits
        self.config.add_collection(
            address="0xTight",
            chain="bsc",
            min_price_bnb=0.1,
            max_price_bnb=0.2,
            margin_bnb=0.05,
        )
        cfg = self.config.get_collection("0xTight")

        # Parasite is very high
        counter_price, reason = self.bidder.calculate_counter_price(cfg, 0.5)

        # Should be clamped to max
        self.assertAlmostEqual(counter_price, 0.2, places=6)
        self.assertIn("clamped to max", reason)

    def test_validate_counter_bid_valid(self):
        """Test validation of a valid bid."""
        cfg = self.config.get_collection("0xCollection1")
        is_valid, error = self.bidder.validate_counter_bid(
            cfg,
            counter_price_bnb=0.501,
            parasite_offer_bnb=0.5,
        )
        self.assertTrue(is_valid)
        self.assertIsNone(error)

    def test_validate_counter_bid_below_min(self):
        """Test that bid below min is invalid."""
        cfg = self.config.get_collection("0xCollection1")
        is_valid, error = self.bidder.validate_counter_bid(
            cfg,
            counter_price_bnb=0.001,
            parasite_offer_bnb=0,
        )
        self.assertFalse(is_valid)
        self.assertIn("min", error.lower())

    def test_validate_counter_bid_above_max(self):
        """Test that bid above max is invalid."""
        cfg = self.config.get_collection("0xCollection1")
        is_valid, error = self.bidder.validate_counter_bid(
            cfg,
            counter_price_bnb=15.0,
            parasite_offer_bnb=0,
        )
        self.assertFalse(is_valid)
        self.assertIn("max", error.lower())

    def test_validate_counter_bid_not_above_parasite(self):
        """Test that bid must be above parasite if parasite exists."""
        cfg = self.config.get_collection("0xCollection1")
        is_valid, error = self.bidder.validate_counter_bid(
            cfg,
            counter_price_bnb=0.4,  # below parasite
            parasite_offer_bnb=0.5,
        )
        self.assertFalse(is_valid)
        self.assertIn("parasite", error.lower())


class TestCounterBidderIntegration(unittest.TestCase):
    """Integration tests for the full counter-bid flow."""

    def setUp(self):
        """Setup for integration tests."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.config = ConfigManager(self.db_path)

        self.okx_mock = MagicMock()
        self.okx_mock.fetch_offers.return_value = []

        self.bidder = CounterBidder(
            config_manager=self.config,
            okx_client=self.okx_mock,
            offerer_address="0xOfferer",
            private_key="0x" + "ff" * 32,
        )

    def tearDown(self):
        """Cleanup."""
        os.close(self.db_fd)
        os.unlink(self.db_path)

    @patch("counter_bidder.get_counter")
    @patch("counter_bidder.build_order_payload")
    @patch("counter_bidder.sign_order")
    def test_process_single_collection_success(self, mock_sign, mock_build, mock_counter):
        """Test successful processing of a single collection."""
        # Setup config
        self.config.add_collection(
            address="0xTest",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
        )

        # Mock API response
        parasite_addr = list(PARASITE_WALLETS)[0]
        self.okx_mock.fetch_offers.return_value = [
            {"maker": parasite_addr, "price": "0.5"},
        ]

        # Mock signing
        mock_counter.return_value = 42
        mock_build.return_value = {"mock": "payload"}
        mock_sign.return_value = "0xsignature"

        # Process
        task = self.bidder.process_single_collection("0xTest")

        # Check
        self.assertTrue(task.valid)
        self.assertAlmostEqual(task.parasite_offer_bnb, 0.5, places=6)
        self.assertAlmostEqual(task.counter_price_bnb, 0.501, places=6)
        self.assertIsNone(task.error)

    def test_process_single_collection_not_in_config(self):
        """Test processing a collection not in config."""
        task = self.bidder.process_single_collection("0xUnknown")
        self.assertFalse(task.valid)
        self.assertIn("not found", task.error.lower())

    def test_process_single_collection_disabled(self):
        """Test processing a disabled collection."""
        self.config.add_collection(
            address="0xDisabled",
            chain="bsc",
            min_price_bnb=0.01,
            max_price_bnb=10.0,
            margin_bnb=0.001,
            enabled=False,
        )
        task = self.bidder.process_single_collection("0xDisabled")
        self.assertFalse(task.valid)
        self.assertIn("disabled", task.error.lower())


if __name__ == "__main__":
    unittest.main()
