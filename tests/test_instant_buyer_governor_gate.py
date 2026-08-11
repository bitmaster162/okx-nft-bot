from __future__ import annotations

from pathlib import Path

import pytest

from okx_nft_bot.sniper.buyer import OKXInstantBuyer


class _StateCapture:
    def __init__(self, *, fail_record: bool = False):
        self.fail_record = fail_record
        self.events: list[dict] = []
        self.force_calls: list[tuple[bool, str | None]] = []

    def record_submit_event(self, **kwargs):
        if self.fail_record:
            raise RuntimeError("audit write failed")
        self.events.append(kwargs)

    def set_force_dry_run(self, enabled: bool, *, reason: str | None = None):
        self.force_calls.append((enabled, reason))


class _GovernorCapture:
    def __init__(self, blocked: str | None = None, *, state: _StateCapture | None = None):
        self.blocked = blocked
        self.state = state or _StateCapture()
        self.gate_calls: list[dict] = []
        self.allocate_calls: list[tuple[str, str]] = []

    def check_live_submit_allowed(self, **kwargs):
        self.gate_calls.append(kwargs)
        return self.blocked

    def allocate_nonce(self, wallet: str, chain: str):
        self.allocate_calls.append((wallet, chain))
        pytest.fail("nonce allocation must not run when live gate blocks")


def _bare_buyer(governor: _GovernorCapture) -> OKXInstantBuyer:
    buyer = OKXInstantBuyer.__new__(OKXInstantBuyer)
    buyer.enabled = True
    buyer.dry_run = False
    buyer.execution_db_path = Path("/tmp/nonexistent-execution.sqlite3")
    buyer.buyer_address = "0x" + "1" * 40
    buyer.buyer_key = "0x" + "2" * 64
    buyer.gas_multiplier = 1.2
    buyer.max_gas_gwei = 50.0
    buyer._failed_orders = set()
    buyer._w3 = {}
    buyer._governor = governor
    buyer._effective_dry_run = lambda: False
    return buyer


def _price_oracle(currency: str) -> float:
    return {
        "BNB": 600.0,
        "WBNB": 600.0,
        "ETH": 3000.0,
        "WETH": 3000.0,
        "USDT": 1.0,
    }.get(currency.upper(), 0.0)


def test_live_gate_normalizes_eth_buy_to_bnb_equivalent(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", _price_oracle)
    governor = _GovernorCapture(blocked="live arm required")
    buyer = _bare_buyer(governor)

    blocked, price_bnb, price_usd = buyer._live_buy_gate(
        collection_address="0x" + "3" * 40,
        chain="eth",
        price=0.01,
        currency="WETH",
    )

    assert blocked == "live arm required"
    assert price_usd == pytest.approx(30.0)
    assert price_bnb == pytest.approx(0.05)
    assert governor.gate_calls == [
        {
            "action_type": "LIVE_BUY",
            "collection": "0x" + "3" * 40,
            "chain": "eth",
            "price_bnb": pytest.approx(0.05),
            "price_usd": pytest.approx(30.0),
        }
    ]


def test_direct_execute_buy_cannot_bypass_live_arm(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", _price_oracle)
    governor = _GovernorCapture(blocked="live arm required")
    buyer = _bare_buyer(governor)

    result = buyer._execute_buy(
        {"orderId": "order-1"},
        "eth",
        0.01,
        collection_address="0x" + "4" * 40,
        currency="WETH",
    )

    assert result == {
        "success": False,
        "error": "LIVE_GATE_BLOCKED:live arm required",
    }
    assert governor.allocate_calls == []


def test_direct_execute_buy_respects_dry_run_before_governor(monkeypatch):
    governor = _GovernorCapture(blocked=None)
    buyer = _bare_buyer(governor)
    buyer._effective_dry_run = lambda: True

    def unexpected_price_lookup(_currency: str) -> float:
        pytest.fail("price lookup must not run while dry-run is active")

    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", unexpected_price_lookup)

    result = buyer._execute_buy(
        {"orderId": "order-2"},
        "bsc",
        0.001,
        collection_address="0x" + "5" * 40,
        currency="WBNB",
    )

    assert result == {
        "success": False,
        "error": "LIVE_GATE_BLOCKED:dry_run_enabled",
    }
    assert governor.gate_calls == []
    assert governor.allocate_calls == []


def test_live_gate_fails_closed_when_price_oracle_unavailable(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.prices.get_usd_price", lambda _currency: 0.0)
    governor = _GovernorCapture(blocked=None)
    buyer = _bare_buyer(governor)

    blocked, price_bnb, price_usd = buyer._live_buy_gate(
        collection_address="0x" + "6" * 40,
        chain="bsc",
        price=0.001,
        currency="WBNB",
    )

    assert blocked == "price_oracle_unavailable"
    assert price_bnb is None
    assert price_usd is None
    assert governor.gate_calls == []


def test_submitted_buy_is_recorded_for_shared_rate_limits():
    state = _StateCapture()
    governor = _GovernorCapture(state=state)

    OKXInstantBuyer._record_live_buy_submit(
        governor,
        collection_address="0x" + "7" * 40,
        chain="eth",
        price_bnb_equiv=0.05,
        price_usd=30.0,
        tx_hash="0xabc",
    )

    assert state.events == [
        {
            "engine": "instant_buyer",
            "action_type": "LIVE_BUY",
            "collection": "0x" + "7" * 40,
            "chain": "eth",
            "price_bnb": pytest.approx(0.05),
            "status": "submitted",
            "reason": "tx_hash=0xabc;price_usd=30.000000",
        }
    ]
    assert state.force_calls == []


def test_submit_audit_failure_forces_dry_run():
    state = _StateCapture(fail_record=True)
    governor = _GovernorCapture(state=state)

    OKXInstantBuyer._record_live_buy_submit(
        governor,
        collection_address="0x" + "8" * 40,
        chain="bsc",
        price_bnb_equiv=0.001,
        price_usd=0.6,
        tx_hash="0xdef",
    )

    assert state.force_calls == [
        (True, "instant_buyer_submit_log_failure")
    ]
