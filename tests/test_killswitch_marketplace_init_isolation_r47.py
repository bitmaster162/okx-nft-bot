from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import okx_nft_bot.killswitch as killswitch


ORDER_HASH = "0x" + "a" * 64
OKX_HASH = "okx-order-1"
COLLECTION = "0x" + "3" * 40


class _BrokenAPI:
    def __init__(self, *, settings):
        assert settings.dry_run is True
        raise RuntimeError("OKX client init unavailable")


class _OpenSea:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def cancel_offer(self, order_hash: str, *, chain: str = "eth") -> bool:
        self.calls.append((order_hash, chain))
        return True


class _State:
    def __init__(self, offers) -> None:
        self.offers = list(offers)
        self.statuses: dict[str, str] = {}
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def disarm_live(self, **_kwargs):
        return {"armed": False}

    def set_force_dry_run(self, _value: bool, **_kwargs):
        return None

    def set_runtime_value(self, _key: str, _value: object):
        return None

    def audit_integrity(self):
        return None

    def get_active_offers(self, *, chain: str):
        assert chain == "eth"
        return list(self.offers)

    def mark_offer_status(self, *, order_hash: str, status: str):
        self.statuses[order_hash] = status
        return True

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("R47 test does not expect exchange-only upsert")

    def record_submit_event(self, **kwargs):
        self.submit_events.append(dict(kwargs))
        return len(self.submit_events)

    def log_action(self, **kwargs):
        self.actions.append(dict(kwargs))
        return len(self.actions)


def _settings(tmp_path: Path):
    return SimpleNamespace(
        execution_db_path=tmp_path / "execution.sqlite3",
        dry_run=False,
    )


def _offer(order_hash: str, marketplace: str):
    payload = {} if marketplace == "okx" else {"marketplace": marketplace}
    return SimpleNamespace(
        order_hash=order_hash,
        preview_payload=payload,
    )


def test_r47_okx_constructor_failure_does_not_suppress_opensea_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(killswitch, "OKXAPIClient", _BrokenAPI)
    settings = _settings(tmp_path)
    state = _State([_offer(ORDER_HASH, "opensea")])
    opensea = _OpenSea()

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        state=state,
        opensea_api=opensea,
        chains=("eth",),
    )

    item = result.chains[0]
    assert settings.dry_run is True
    assert opensea.calls == [(ORDER_HASH, "eth")]
    assert item.active_offers_seen == 1
    assert item.live_cancelled == 1
    assert item.failed == ()
    assert item.exchange_lookup_failed is True
    assert item.exchange_lookup_error == "api_init: OKX client init unavailable"
    assert item.fatal_error == "api_init: OKX client init unavailable"
    assert item.failure_count == 1
    assert result.total_failed == 1
    assert state.statuses == {ORDER_HASH: "cancelled"}

    assert len(state.submit_events) == 1
    assert "live_cancelled=1" in str(state.submit_events[0]["reason"])
    assert "fatal=1" in str(state.submit_events[0]["reason"])
    assert state.actions[0]["payload"]["fatal_error"] == "api_init: OKX client init unavailable"

    formatted = killswitch.format_killswitch_result(result)
    assert "live_cancelled=1" in formatted
    assert "fatal_error=api_init: OKX client init unavailable" in formatted


def test_r47_okx_zombie_stays_failed_while_opensea_is_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(killswitch, "OKXAPIClient", _BrokenAPI)
    settings = _settings(tmp_path)
    state = _State(
        [
            _offer(ORDER_HASH, "opensea"),
            _offer(OKX_HASH, "okx"),
        ]
    )
    opensea = _OpenSea()

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        state=state,
        opensea_api=opensea,
        chains=("eth",),
    )

    item = result.chains[0]
    assert opensea.calls == [(ORDER_HASH, "eth")]
    assert item.live_cancelled == 1
    assert item.fatal_error == "api_init: OKX client init unavailable"
    assert item.failed == (f"{OKX_HASH}:api_init: OKX client init unavailable",)
    assert item.failure_count == 2
    assert result.total_failed == 2
    assert state.statuses[ORDER_HASH] == "cancelled"
    assert state.statuses[OKX_HASH] == "killswitch_failed"


def test_r47_pure_r37_fatal_format_remains_compact(monkeypatch, tmp_path):
    monkeypatch.setattr(killswitch, "OKXAPIClient", _BrokenAPI)
    settings = _settings(tmp_path)
    state = _State([])

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        state=state,
        opensea_api=_OpenSea(),
        chains=("eth",),
    )

    assert killswitch.format_killswitch_result(result).splitlines()[-2] == (
        "chain=eth fatal_error=api_init: OKX client init unavailable"
    )
