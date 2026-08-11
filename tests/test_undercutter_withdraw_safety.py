from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.undercutter.engine import UndercutAction, UndercutEngine


class _Governor:
    def __init__(self, *, dry_run: bool, armed: bool, expires_at: str | None = None) -> None:
        self.dry_run = dry_run
        self.armed = armed
        self.expires_at = expires_at

    def effective_dry_run(self):
        return self.dry_run

    def get_live_arm_state(self, *, now=None):
        return {
            "armed": self.armed,
            "expires_at": self.expires_at,
        }


class _State:
    def __init__(self) -> None:
        self.marks: list[tuple[str, str]] = []
        self.events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def mark_offer_status(self, *, order_hash: str, status: str):
        self.marks.append((order_hash, status))
        return True

    def record_submit_event(self, **kwargs):
        self.events.append(dict(kwargs))

    def log_action(self, **kwargs):
        self.actions.append(dict(kwargs))


class _OfferClient:
    def __init__(self, *, cancel_result: bool = True) -> None:
        self.cancel_result = cancel_result
        self.cancel_calls: list[tuple[str, str, object]] = []

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancel_calls.append((order_hash, chain, order_params))
        return self.cancel_result


def _engine(*, dry_run: bool, armed: bool, cancel_result: bool = True) -> tuple[UndercutEngine, _State, _OfferClient]:
    engine = UndercutEngine.__new__(UndercutEngine)
    state = _State()
    client = _OfferClient(cancel_result=cancel_result)
    engine.settings = SimpleNamespace(execution_chain="bsc")
    engine.state = state
    engine.offer_client = client
    engine.governor = _Governor(dry_run=dry_run, armed=armed)
    engine._fetch_order_params = lambda _order_hash, _chain: {"test": True}
    engine._send_live_notification = lambda **_kwargs: True
    return engine, state, client


def _withdraw(order_hash: str) -> UndercutAction:
    return UndercutAction(
        action_type="WITHDRAW",
        collection="0xcollection",
        chain="bsc",
        old_price_bnb=0.1,
        new_price_bnb=None,
        reason="test withdraw",
        order_hash=order_hash,
    )


def test_forced_dry_withdraw_keeps_real_live_offer_active() -> None:
    engine, state, client = _engine(dry_run=True, armed=True)
    action = _withdraw("live-order-1")

    engine._apply_action(action)

    assert action.executed is False
    assert action.error == "dry_run_enabled"
    assert client.cancel_calls == []
    assert state.marks == []
    assert state.events[-1]["action_type"] == "LIVE_WITHDRAW_BLOCKED"
    assert state.events[-1]["status"] == "blocked"


def test_disarmed_live_withdraw_does_not_cancel_exchange_or_local_state() -> None:
    engine, state, client = _engine(dry_run=False, armed=False)
    action = _withdraw("live-order-2")

    engine._apply_action(action)

    assert action.executed is False
    assert action.error == "live arm required"
    assert client.cancel_calls == []
    assert state.marks == []
    assert state.events[-1]["status"] == "blocked"


def test_confirmed_live_withdraw_cancels_exchange_then_local_state() -> None:
    engine, state, client = _engine(dry_run=False, armed=True)
    action = _withdraw("live-order-3")

    engine._apply_action(action)

    assert action.executed is True
    assert client.cancel_calls == [("live-order-3", "bsc", {"test": True})]
    assert state.marks == [("live-order-3", "cancelled")]
    assert state.events[-1]["action_type"] == "LIVE_WITHDRAW"
    assert state.events[-1]["status"] == "cancelled"
    assert "local_marked=1" in str(state.events[-1]["reason"])


def test_dryrun_withdraw_only_mutates_synthetic_local_offer() -> None:
    engine, state, client = _engine(dry_run=True, armed=False)
    action = _withdraw("dryrun-preview-1")

    engine._apply_action(action)

    assert action.executed is True
    assert client.cancel_calls == []
    assert state.marks == [("dryrun-preview-1", "cancelled")]
    assert state.events == []


def test_dry_run_defense_does_not_retire_real_live_offer() -> None:
    engine, state, client = _engine(dry_run=True, armed=False)

    engine._retire_previous_offer_for_defense(
        action_type="DEFENSE",
        previous_order_hash="live-order-4",
        effective_dry_run=True,
        chain="bsc",
    )

    assert client.cancel_calls == []
    assert state.marks == []


def test_dry_run_defense_can_retire_synthetic_offer() -> None:
    engine, state, client = _engine(dry_run=True, armed=False)

    engine._retire_previous_offer_for_defense(
        action_type="DEFENSE",
        previous_order_hash="dryrun-preview-2",
        effective_dry_run=True,
        chain="bsc",
    )

    assert client.cancel_calls == []
    assert state.marks == [("dryrun-preview-2", "retired")]
