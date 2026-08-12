from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.mass_offer.unwind import MassOfferUnwindController


class _GovernorStub:
    def __init__(self, effective: bool) -> None:
        self.effective = effective
        self.calls: list[bool] = []

    def effective_dry_run(self, requested: bool) -> bool:
        self.calls.append(bool(requested))
        return self.effective


class _EngineStub:
    def __init__(self, effective: bool) -> None:
        self.governor = _GovernorStub(effective)
        self.cancel_calls: list[tuple[str, tuple[str, ...]]] = []

    def cancel_selected(self, *, chain: str, order_hashes: list[str]) -> dict[str, object]:
        self.cancel_calls.append((chain, tuple(order_hashes)))
        return {
            "selected_seen": len(order_hashes),
            "cancelled": len(order_hashes),
            "failed": [],
        }


def _controller(*, effective: bool) -> tuple[MassOfferUnwindController, _EngineStub]:
    controller = object.__new__(MassOfferUnwindController)
    engine = _EngineStub(effective)
    controller.engine = engine
    controller._persist_runtime_summary = lambda *args, **kwargs: None
    return controller, engine


def _report(*, selected: bool = True) -> SimpleNamespace:
    candidates = []
    if selected:
        candidates.append(
            SimpleNamespace(
                order_hash="offer-1",
                selected=True,
                price_bnb=0.25,
            )
        )
    return SimpleNamespace(
        candidates=candidates,
        wallet="0xabc",
        chain="bsc",
    )


def test_requested_live_forced_dry_run_becomes_simulation() -> None:
    controller, engine = _controller(effective=True)

    result = controller.execute_report(_report(), dry_run=False)

    assert result.requested_dry_run is False
    assert result.effective_dry_run is True
    assert result.selected_count == 1
    assert result.attempted_count == 1
    assert result.simulated_count == 1
    assert result.cancelled_count == 0
    assert result.failed_count == 0
    assert engine.cancel_calls == []
    assert engine.governor.calls == [False]


def test_no_selection_still_reports_effective_forced_dry_run() -> None:
    controller, engine = _controller(effective=True)

    result = controller.execute_report(_report(selected=False), dry_run=False)

    assert result.requested_dry_run is False
    assert result.effective_dry_run is True
    assert result.selected_count == 0
    assert result.simulated_count == 0
    assert result.cancelled_count == 0
    assert engine.cancel_calls == []
    assert engine.governor.calls == [False]


def test_explicit_dry_run_remains_simulation() -> None:
    controller, engine = _controller(effective=True)

    result = controller.execute_report(_report(), dry_run=True)

    assert result.requested_dry_run is True
    assert result.effective_dry_run is True
    assert result.simulated_count == 1
    assert result.cancelled_count == 0
    assert engine.cancel_calls == []
    assert engine.governor.calls == [True]


def test_genuine_live_mode_still_calls_selected_cancel() -> None:
    controller, engine = _controller(effective=False)

    result = controller.execute_report(_report(), dry_run=False)

    assert result.requested_dry_run is False
    assert result.effective_dry_run is False
    assert result.selected_count == 1
    assert result.attempted_count == 1
    assert result.simulated_count == 0
    assert result.cancelled_count == 1
    assert result.failed_count == 0
    assert engine.cancel_calls == [("bsc", ("offer-1",))]
    assert engine.governor.calls == [False]
