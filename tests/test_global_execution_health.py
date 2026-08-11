from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import okx_nft_bot.ops as ops
from okx_nft_bot.execution_governor import ExecutionGovernor


class _Integrity:
    def to_dict(self):
        return {"issue_count": 0, "ok": True}


class _ReconcileState:
    def __init__(self) -> None:
        self.runtime: dict[str, object] = {}

    def audit_integrity(self):
        return _Integrity()

    def get_active_offers(self, *, chain: str):
        return []

    def set_runtime_value(self, key: str, value: object):
        self.runtime[key] = value


class _ReconcileAPI:
    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert chain == "eth"
        assert require_all_endpoints is True
        return []


class _SnapshotState:
    runtime: dict[str, object] = {}
    active: dict[str, list[object]] = {}
    failed: dict[str, list[object]] = {}

    def __init__(self, _db_path: Path) -> None:
        pass

    def audit_integrity(self):
        return _Integrity()

    def get_runtime_state(self):
        return dict(type(self).runtime)

    def get_active_offers(self, *, chain: str):
        return list(type(self).active.get(chain, []))

    def get_killswitch_failed_offers(self, *, chain: str):
        return list(type(self).failed.get(chain, []))

    def get_fill_summary(self, *, chain: str):
        return {"chain": chain, "confirmed_fill_count": 0}

    def effective_dry_run(self, configured: bool):
        return configured

    def is_force_dry_run(self):
        return True

    def get_live_arm_state(self, *, now=None):
        return {"armed": False, "expires_at": None}


class _HealthStore:
    def get_state(self, _namespace: str, _key: str):
        return None

    def set_state(self, _namespace: str, _key: str, _value):
        raise AssertionError("health read should not mutate empty alert state")


def _settings(tmp_path: Path):
    db_path = tmp_path / "main.sqlite3"
    execution_db_path = tmp_path / "execution.sqlite3"
    db_path.touch()
    execution_db_path.touch()
    return SimpleNamespace(
        db_path=db_path,
        execution_db_path=execution_db_path,
        metrics_path=tmp_path / "runtime_metrics.json",
        execution_chain="bsc",
        dry_run=True,
        execution_reconcile_max_staleness_seconds=300,
    )


def test_reconcile_persists_chain_scoped_timestamp(tmp_path: Path) -> None:
    state = _ReconcileState()
    settings = SimpleNamespace(execution_chain="bsc", execution_db_path=tmp_path / "execution.sqlite3")
    governor = ExecutionGovernor(settings=settings, state=state, api_client=_ReconcileAPI())

    result = governor.reconcile_active_offers(chain="eth")

    assert result.chain == "eth"
    assert state.runtime["last_reconcile_chain"] == "eth"
    assert state.runtime["last_reconcile_at"] == result.completed_at
    assert state.runtime["last_reconcile_at_eth"] == result.completed_at


def test_snapshot_aggregates_non_selected_chain_killswitch_failure(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _SnapshotState.runtime = {
        "last_reconcile_at_bsc": now,
        "last_reconcile_at_eth": now,
    }
    _SnapshotState.active = {"bsc": [], "eth": []}
    _SnapshotState.failed = {"bsc": [], "eth": [SimpleNamespace(order_hash="eth-zombie")]}
    monkeypatch.setattr(ops, "PositionState", _SnapshotState)

    snapshot = ops._build_execution_snapshot(_settings(tmp_path))

    assert snapshot["chain"] == "bsc"
    assert snapshot["killswitch_failed_count"] == 1
    assert snapshot["chains"]["bsc"]["killswitch_failed_count"] == 0
    assert snapshot["chains"]["eth"]["killswitch_failed_count"] == 1


def test_healthcheck_blocks_on_non_selected_chain_killswitch_failure(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _SnapshotState.runtime = {
        "last_reconcile_at_bsc": now,
        "last_reconcile_at_eth": now,
    }
    _SnapshotState.active = {"bsc": [], "eth": []}
    _SnapshotState.failed = {"bsc": [], "eth": [SimpleNamespace(order_hash="eth-zombie")]}
    monkeypatch.setattr(ops, "PositionState", _SnapshotState)

    result = ops.run_healthcheck(_settings(tmp_path), _HealthStore(), require_fresh_metrics=False)

    assert result.healthy is False
    assert result.reason == "execution_killswitch_failed"
    assert result.payload["execution"]["chains"]["eth"]["killswitch_failed_count"] == 1


def test_healthcheck_uses_reconcile_age_of_chain_with_live_offers(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _SnapshotState.runtime = {
        "last_reconcile_at_bsc": now.isoformat(),
        "last_reconcile_at_eth": (now - timedelta(hours=1)).isoformat(),
    }
    _SnapshotState.active = {
        "bsc": [],
        "eth": [SimpleNamespace(order_hash="0xlive")],
    }
    _SnapshotState.failed = {"bsc": [], "eth": []}
    monkeypatch.setattr(ops, "PositionState", _SnapshotState)

    result = ops.run_healthcheck(_settings(tmp_path), _HealthStore(), require_fresh_metrics=False)

    assert result.healthy is False
    assert result.reason == "execution_reconcile_stale"
    assert result.payload["execution"]["chains"]["bsc"]["live_active_offer_count"] == 0
    assert result.payload["execution"]["chains"]["eth"]["live_active_offer_count"] == 1
