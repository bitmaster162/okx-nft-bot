from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import okx_nft_bot.telegram_bot as telegram


class _FakeState:
    instance = None
    trace: list[str] = []

    def __init__(self, _db_path: Path) -> None:
        type(self).instance = self
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []
        self.status_updates: list[tuple[str, str]] = []

    def audit_integrity(self):
        self.trace.append("audit")
        return SimpleNamespace(ok=True)

    def disarm_live(self, **_kwargs):
        self.trace.append("disarm")
        return {"armed": False}

    def set_force_dry_run(self, value: bool, **_kwargs):
        self.trace.append(f"force_dry:{int(value)}")

    def set_runtime_value(self, key: str, _value: object):
        self.trace.append(f"runtime:{key}")

    def get_active_offers(self, *, chain: str):
        self.trace.append(f"active:{chain}")
        return []

    def mark_offer_status(self, *, order_hash: str, status: str):
        self.status_updates.append((order_hash, status))
        return True

    def record_submit_event(self, **kwargs):
        self.submit_events.append(dict(kwargs))

    def log_action(self, **kwargs):
        self.actions.append(dict(kwargs))


class _FakeAPI:
    instance = None
    trace: list[str] = []

    def __init__(self, *, settings) -> None:
        type(self).instance = self
        self.settings = settings
        self.cancel_chains: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        return [{"offerId": f"offer-{chain}", "protocolData": {"parameters": {"salt": "1"}}}]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        self.trace.append(f"cancel:{chain}")
        self.cancel_chains.append(chain)
        return True


def _processor() -> telegram.TelegramCommandProcessor:
    processor = telegram.TelegramCommandProcessor.__new__(telegram.TelegramCommandProcessor)
    processor.settings = SimpleNamespace(execution_db_path=Path("/tmp/execution.sqlite3"))
    return processor


def test_killswitch_fans_out_to_every_supported_execution_chain(monkeypatch) -> None:
    trace: list[str] = []
    _FakeState.trace = trace
    _FakeAPI.trace = trace
    monkeypatch.setattr(telegram, "PositionState", _FakeState)
    monkeypatch.setattr(telegram, "OKXAPIClient", _FakeAPI)
    monkeypatch.setattr(telegram, "SUPPORTED_EXECUTION_CHAINS", ("bsc", "eth"))

    text = _processor()._killswitch_command([])

    assert _FakeAPI.instance is not None
    assert _FakeAPI.instance.cancel_chains == ["bsc", "eth"]
    assert "chain=bsc" in text
    assert "chain=eth" in text
    assert "total_failed=0" in text
    assert trace.index("disarm") < trace.index("lookup:bsc")
    assert trace.index("force_dry:1") < trace.index("lookup:bsc")
    assert [row["chain"] for row in _FakeState.instance.submit_events] == ["bsc", "eth"]


def test_killswitch_continues_other_chains_after_chain_fatal(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "PositionState", _FakeState)
    monkeypatch.setattr(telegram, "OKXAPIClient", _FakeAPI)
    monkeypatch.setattr(telegram, "SUPPORTED_EXECUTION_CHAINS", ("bsc", "eth"))

    calls: list[str] = []

    def _fake_chain(self, *, state, api, chain):
        calls.append(chain)
        if chain == "bsc":
            raise RuntimeError("bsc unavailable")
        return {
            "chain": chain,
            "active_offers_seen": 0,
            "exchange_seen": 0,
            "live_cancelled": 0,
            "local_cancelled": 0,
            "already_gone": 0,
            "failed": [],
            "exchange_lookup_failed": False,
            "exchange_lookup_error": None,
        }

    monkeypatch.setattr(telegram.TelegramCommandProcessor, "_killswitch_chain", _fake_chain)

    text = _processor()._killswitch_command([])

    assert calls == ["bsc", "eth"]
    assert "chain=bsc fatal_error=bsc unavailable" in text
    assert "chain=eth" in text
    assert "total_failed=1" in text


def test_killswitch_rejects_arguments_without_side_effects(monkeypatch) -> None:
    def _unexpected(*_args, **_kwargs):
        raise AssertionError("state must not be constructed")

    monkeypatch.setattr(telegram, "PositionState", _unexpected)

    assert _processor()._killswitch_command(["eth"]) == "Usage: /killswitch"
