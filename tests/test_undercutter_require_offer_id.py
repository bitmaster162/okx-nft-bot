from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.undercutter.engine import UndercutAction, UndercutEngine


class _GovernorStub:
    def effective_dry_run(self):
        return False

    def check_live_submit_allowed(self, **kwargs):
        return None


class _OfferClientStub:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.submit_calls = 0
        self.cancel_calls = 0

    def submit_offer(self, payload):
        self.submit_calls += 1
        return dict(self.response)

    def cancel_offer(self, *args, **kwargs):
        self.cancel_calls += 1
        raise AssertionError("old offer must not be retired when new offer has no exchange id")


class _StateStub:
    def __init__(self) -> None:
        self.active_writes = []
        self.submit_events = []
        self.action_logs = []
        self.status_marks = []

    def upsert_active_offer(self, **kwargs):
        self.active_writes.append(kwargs)

    def record_submit_event(self, **kwargs):
        self.submit_events.append(kwargs)

    def log_action(self, **kwargs):
        self.action_logs.append(kwargs)

    def mark_offer_status(self, **kwargs):
        self.status_marks.append(kwargs)
        return True


def _engine(response: dict[str, object]) -> tuple[UndercutEngine, _OfferClientStub, _StateStub]:
    engine = object.__new__(UndercutEngine)
    client = _OfferClientStub(response)
    state = _StateStub()
    engine.settings = SimpleNamespace(
        buyer_wallet_address=None,
        buyer_wallet_private_key=None,
        execution_chain="bsc",
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    engine.offer_client = client
    engine.state = state
    engine.governor = _GovernorStub()
    return engine, client, state


def test_live_attack_without_offer_id_is_failed_not_submitted(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.to_usd", lambda amount, currency="BNB": 100.0)
    engine, client, state = _engine({"offer_id": None, "status": "submitted"})
    action = UndercutAction(
        action_type="ATTACK",
        collection="0xcollection",
        chain="bsc",
        old_price_bnb=None,
        new_price_bnb=0.25,
        reason="test",
    )

    engine._apply_action(action)

    assert client.submit_calls == 1
    assert action.executed is False
    assert action.error == "no_offer_id_in_response"
    assert state.active_writes == []
    assert len(state.submit_events) == 1
    assert state.submit_events[0]["status"] == "failed"
    assert state.submit_events[0]["reason"] == "no_offer_id_in_response"
    assert len(state.action_logs) == 1
    assert state.action_logs[0]["executed"] is False


def test_live_defense_without_offer_id_keeps_previous_offer(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.to_usd", lambda amount, currency="BNB": 100.0)
    engine, client, state = _engine({"offer_id": "", "status": "submitted"})
    action = UndercutAction(
        action_type="DEFENSE",
        collection="0xcollection",
        chain="bsc",
        old_price_bnb=0.20,
        new_price_bnb=0.25,
        reason="test",
        order_hash="old-live-order",
    )

    engine._apply_action(action)

    assert client.submit_calls == 1
    assert client.cancel_calls == 0
    assert action.executed is False
    assert action.error == "no_offer_id_in_response"
    assert state.active_writes == []
    assert state.status_marks == []
    assert state.submit_events[0]["status"] == "failed"


def test_live_attack_with_exchange_offer_id_remains_success(monkeypatch) -> None:
    monkeypatch.setattr("okx_nft_bot.prices.to_usd", lambda amount, currency="BNB": 100.0)
    engine, client, state = _engine({"offer_id": "order-abc", "status": "submitted"})
    action = UndercutAction(
        action_type="ATTACK",
        collection="0xcollection",
        chain="bsc",
        old_price_bnb=None,
        new_price_bnb=0.25,
        reason="test",
    )

    engine._apply_action(action)

    assert client.submit_calls == 1
    assert action.executed is True
    assert action.error is None
    assert action.order_hash == "order-abc"
    assert len(state.active_writes) == 1
    assert state.active_writes[0]["order_hash"] == "order-abc"
    assert len(state.submit_events) == 1
    assert state.submit_events[0]["status"] == "submitted"
    assert "offer_id=order-abc" in state.submit_events[0]["reason"]
