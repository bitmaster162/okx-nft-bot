from __future__ import annotations

from pathlib import Path

from okx_nft_bot.killswitch import activate_multichain_killswitch, format_killswitch_result


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
    def __init__(self, trace: list[str], *, audit_error: str | None = None) -> None:
        self.trace = trace
        self.audit_error = audit_error
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def audit_integrity(self):
        self.trace.append("audit")
        if self.audit_error:
            raise RuntimeError(self.audit_error)
        return None

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

    def mark_offer_status(self, **_kwargs):
        return False

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("successful exchange cancel must not create a zombie")

    def record_submit_event(self, **kwargs):
        self.trace.append(f"audit_submit:{kwargs['chain']}")
        self.submit_events.append(dict(kwargs))
        return len(self.submit_events)

    def log_action(self, **kwargs):
        self.trace.append(f"audit_action:{kwargs['chain']}")
        self.actions.append(dict(kwargs))
        return len(self.actions)


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


def test_audit_failure_happens_after_safety_latch_and_does_not_stop_cancellation(tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    state = _State(trace, audit_error="integrity DB unavailable")
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert trace.index("settings_dry:1") < trace.index("disarm")
    assert trace.index("disarm") < trace.index("audit")
    assert trace.index("force_dry:1") < trace.index("audit")
    assert trace.index("audit") < trace.index("lookup:bsc")
    assert api.cancelled == ["bsc", "eth"]
    assert [row["chain"] for row in state.submit_events] == ["bsc", "eth"]

    assert result.preflight_error == "integrity DB unavailable"
    assert result.total_failed == 1
    assert all(chain.failure_count == 0 for chain in result.chains)

    text = format_killswitch_result(result)
    assert "preflight_error=integrity DB unavailable" in text
    assert "total_failed=1" in text


def test_clean_preflight_keeps_normal_result_semantics(tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    state = _State(trace)
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("eth",),
    )

    assert settings.dry_run is True
    assert result.preflight_error is None
    assert result.total_failed == 0
    assert result.chains[0].live_cancelled == 1
    assert trace.index("settings_dry:1") < trace.index("disarm")
    assert trace.index("disarm") < trace.index("audit")
    assert trace.index("force_dry:1") < trace.index("audit")
    assert trace.index("audit") < trace.index("lookup:eth")

    text = format_killswitch_result(result)
    assert "preflight_error=" not in text
    assert "total_failed=0" in text
