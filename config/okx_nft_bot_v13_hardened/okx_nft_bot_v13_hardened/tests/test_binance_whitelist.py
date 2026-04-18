"""Unit tests for providers/binance_whitelist.py and adapters/binance_support_mapper.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from okx_nft_bot.adapters.binance_support_mapper import BinanceSupportMapper
from okx_nft_bot.providers.binance_whitelist import (
    BinanceWhitelistEntry,
    BinanceWhitelistProvider,
    _parse_html_entries,
)


# ─── BinanceWhitelistEntry ────────────────────────────────────────────────────

def test_entry_lowercases_address():
    entry = BinanceWhitelistEntry(
        collection_name="Test",
        contract_address_or_identifier="0xABC123",
        network="eth",
        binance_supported=True,
        binance_list_type="main",
        source_url="https://example.com",
        verified_at="2026-01-01",
    )
    assert entry.contract_address_or_identifier == "0xabc123"


def test_entry_to_dict_round_trip():
    entry = BinanceWhitelistEntry(
        collection_name="Pancake Bunnies",
        contract_address_or_identifier="0xdf7952b35f24acf7fc0487d01c8d5690a60dba07",
        network="bsc",
        binance_supported=True,
        binance_list_type="main",
        source_url="https://www.binance.com/en/support/faq/3bee0ac7437649bc94b0903d1e22609f",
        verified_at="2026-03-09",
    )
    d = entry.to_dict()
    assert d["collection_name"] == "Pancake Bunnies"
    assert d["binance_supported"] is True
    assert d["source_reliability"] == "reference_static"


# ─── BinanceWhitelistProvider (static path) ───────────────────────────────────

def _write_static(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "binance_whitelist.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_provider_loads_static_file(tmp_path: Path):
    entries_raw = [
        {
            "collection_name": "Pancake Bunnies",
            "contract_address_or_identifier": "0xdf7952b35f24acf7fc0487d01c8d5690a60dba07",
            "network": "bsc",
            "binance_supported": True,
            "binance_list_type": "main",
            "source_url": "https://example.com",
            "source_reliability": "reference_static",
            "verified_at": "2026-03-09",
        }
    ]
    static_path = _write_static(tmp_path, entries_raw)
    provider = BinanceWhitelistProvider(static_fallback_path=static_path)
    entries = provider.load_static()
    assert len(entries) == 1
    assert entries[0].collection_name == "Pancake Bunnies"
    assert entries[0].network == "bsc"


def test_provider_returns_empty_on_missing_file(tmp_path: Path):
    provider = BinanceWhitelistProvider(static_fallback_path=tmp_path / "nonexistent.json")
    entries = provider.load_static()
    assert entries == []


def test_provider_returns_empty_on_malformed_json(tmp_path: Path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("not json at all }{", encoding="utf-8")
    provider = BinanceWhitelistProvider(static_fallback_path=bad_path)
    entries = provider.load_static()
    assert entries == []


# ─── HTML parsing ─────────────────────────────────────────────────────────────

def test_parse_html_extracts_table_rows():
    html = """
    <html><body>
    <table>
        <tr><th>Collection</th><th>Contract</th><th>Network</th></tr>
        <tr><td>Pancake Bunnies</td><td>0xDf7952B35f24aCF7fC0487D01c8d5690a60DBa07</td><td>BSC</td></tr>
        <tr><td>Azuki</td><td>0xED5AF388653567Af2F388E6224dc7c4b3241C544</td><td>Ethereum</td></tr>
    </table>
    </body></html>
    """
    entries = _parse_html_entries(html, list_type="main", source_url="https://example.com")
    assert len(entries) == 2
    addrs = {e.contract_address_or_identifier for e in entries}
    assert "0xdf7952b35f24acf7fc0487d01c8d5690a60dba07" in addrs
    assert "0xed5af388653567af2f388e6224dc7c4b3241c544" in addrs
    for e in entries:
        assert e.binance_supported is True
        assert e.binance_list_type == "main"
        assert e.source_reliability == "reference_static"


def test_parse_html_no_addresses_returns_empty():
    html = "<html><body><p>No contracts here</p></body></html>"
    entries = _parse_html_entries(html, list_type="main", source_url="https://example.com")
    assert entries == []


def test_parse_html_deduplicates_same_address():
    html = """
    <table>
        <tr><td>Collection A</td><td>0xDf7952B35f24aCF7fC0487D01c8d5690a60DBa07</td></tr>
        <tr><td>Collection B</td><td>0xDf7952B35f24aCF7fC0487D01c8d5690a60DBa07</td></tr>
    </table>
    """
    entries = _parse_html_entries(html, list_type="main", source_url="https://example.com")
    assert len(entries) == 1


# ─── BinanceSupportMapper ─────────────────────────────────────────────────────

def _make_entry(name: str, address: str, list_type: str = "main", network: str = "eth") -> BinanceWhitelistEntry:
    return BinanceWhitelistEntry(
        collection_name=name,
        contract_address_or_identifier=address,
        network=network,
        binance_supported=True,
        binance_list_type=list_type,
        source_url="https://example.com",
        verified_at="2026-03-09",
    )


def test_mapper_lookup_by_address():
    entries = [_make_entry("Pancake Bunnies", "0xdf7952b35f24acf7fc0487d01c8d5690a60dba07", network="bsc")]
    mapper = BinanceSupportMapper(entries)
    meta = mapper.lookup(address="0xdf7952b35f24acf7fc0487d01c8d5690a60dba07")
    assert meta.binance_supported is True
    assert meta.binance_list_type == "main"
    assert meta.support_confidence == "confirmed"


def test_mapper_lookup_case_insensitive():
    entries = [_make_entry("BAYC", "0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D")]
    mapper = BinanceSupportMapper(entries)
    meta = mapper.lookup(address="0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d")
    assert meta.binance_supported is True


def test_mapper_lookup_by_name_exact():
    entries = [_make_entry("Azuki", "0xed5af388653567af2f388e6224dc7c4b3241c544")]
    mapper = BinanceSupportMapper(entries)
    meta = mapper.lookup(name="Azuki")
    assert meta.binance_supported is True


def test_mapper_lookup_by_name_substring():
    entries = [_make_entry("Pancake Bunnies", "0xdf7952b35f24acf7fc0487d01c8d5690a60dba07")]
    mapper = BinanceSupportMapper(entries)
    meta = mapper.lookup(name="pancake")
    assert meta.binance_supported is True


def test_mapper_unknown_returns_false():
    mapper = BinanceSupportMapper([])
    meta = mapper.lookup(address="0xdeadbeef")
    assert meta.binance_supported is False
    assert meta.support_confidence == "unknown"
    assert meta.binance_list_type is None


def test_mapper_lookup_dict_has_all_required_keys():
    entries = [_make_entry("BAYC", "0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d", list_type="aggregator")]
    mapper = BinanceSupportMapper(entries)
    d = mapper.lookup_dict(address="0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d")
    required = {"binance_supported", "binance_list_type", "support_confidence", "support_source_url", "freshness", "confidence"}
    assert required.issubset(d.keys())


def test_mapper_empty_entries_never_raises():
    mapper = BinanceSupportMapper([])
    meta = mapper.lookup(address=None, name=None)
    assert meta.binance_supported is False
