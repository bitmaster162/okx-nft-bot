from __future__ import annotations

from pathlib import Path

from okx_nft_bot.killswitch import activate_multichain_killswitch


class _Settings:
    def __init__(self, trace: list[str], db_path: Path) -> None:
        self.trace = trace
        self.execution_db_path = db_path
        self._dry_run = False

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = bool(value)
        self.trace.append(f"settings_dry:{int(self._dry_run)}")


class _State:
    def __init__(
        self,
        trace: list[str],
        *,
        fail_disarm: bool = False,
        fail_force: bool = False,
        fail_runtime_keys: set[str] | None = None,
        fail_audit: bool = False,
    ) -> None:
        self.trace = trace
        self.fail_disarm = fail_disarm
        self.fail_force = fail_force
        self.fail_runtime_keys = fail_runtime_keys or set()
        self.fail_audit = fail_audit
        self.submit_events: list[dict[str, object]] = []

    def disarm_live(self, **_kwargs):
        self.trace.append("disarm")
        if self.fail_disarm:
            raise RuntimeError("disarm unavailable")
        return {"armed": False}

    def set_force_dry_run(self, value: bool, **_kwargs):
        self.trace.append(f"force_dry:{int(value)}")
        if self.fail_force:
            raise RuntimeError("force-dry unavailable")

    def set_runtime_value(self, key: str, _value: object):
        self.trace.append(f"runtime:{key}")
        if key in self.fail_runtime_keys:
            raise RuntimeError(f"runtime {key} unavailable")

    def audit_integrity(self):
        self.trace.append("audit")
        if self.fail_audit:
            raise RuntimeError("integrity unavailable")
        return None

    def get_active_offers(self, *, chain: str):
        self.trace.append(f"active:{chain}")
        return []

    def mark_offer_status(self, **_kwargs):
        return False

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("successful exchange cancellation must not create zombie state")

    def record_submit_event(self, **kwargs):
        self.trace.append(f"audit_submit:{kwargs['chain']}")
        self.submit_events.append(dict(kwargs))
        return len(self.submit_events)

    def log_action(self, **kwargs):
        self.trace.append(f"audit_action:{kwargs['chain']}")
        return 1


class _API:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        return [
            {
                "offerId": f"offer-{chain}",
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.trace.append(f"cancel:{chain}")
        self.cancelled.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return True


def test_disarm_persistence_failure_does_not_stop_multichain_cancel(tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    state = _State(trace, fail_disarm=True)
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert api.cancelled == ["bsc", "eth"]
    assert result.preflight_error == "disarm_live: disarm unavailable"
    assert result.total_failed == 1
    assert all(chain.failure_count == 0 for chain in result.chains)
    assert trace.index("settings_dry:1") < trace.index("disarm")
    assert trace.index("disarm") < trace.index("force_dry:1")
    assert trace.index("audit") < trace.index("lookup:bsc")


def test_force_and_runtime_persistence_failures_are_aggregated_without_stopping_cancel(tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    state = _State(
        trace,
        fail_force=True,
        fail_runtime_keys={"killswitch_source"},
    )
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("eth",),
    )

    assert api.cancelled == ["eth"]
    assert result.preflight_error == (
        "set_force_dry_run: force-dry unavailable; "
        "set_runtime_value[killswitch_source]: runtime killswitch_source unavailable"
    )
    assert result.total_failed == 1
    assert result.chains[0].failure_count == 0


def test_all_persistent_preflight_writes_and_audit_can_fail_but_cancel_still_runs(tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    state = _State(
        trace,
        fail_disarm=True,
        fail_force=True,
        fail_runtime_keys={"killswitch_activated_at", "killswitch_source"},
        fail_audit=True,
    )
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert api.cancelled == ["bsc", "eth"]
    assert result.total_failed == 1
    assert result.preflight_error is not None
    assert "disarm_live: disarm unavailable" in result.preflight_error
    assert "set_force_dry_run: force-dry unavailable" in result.preflight_error
    assert "set_runtime_value[killswitch_activated_at]: runtime killswitch_activated_at unavailable" in result.preflight_error
    assert "set_runtime_value[killswitch_source]: runtime killswitch_source unavailable" in result.preflight_error
    assert result.preflight_error.endswith("integrity unavailable")
    assert trace.index("settings_dry:1") < trace.index("disarm")
    assert trace.index("audit") < trace.index("lookup:bsc")
