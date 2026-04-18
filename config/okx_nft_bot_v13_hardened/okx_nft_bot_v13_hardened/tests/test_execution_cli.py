from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from okx_nft_bot.config import Settings
from okx_nft_bot.execution_cli import (
    cmd_audit_state,
    cmd_arm_live,
    build_parser,
    cmd_counterbid_config,
    cmd_counterbid_run,
    cmd_counterbid_scan,
    cmd_disarm_live,
    cmd_preview_counterbid,
    cmd_reconcile_state,
    cmd_undercut_history,
    cmd_undercut_status,
    cmd_undercut_withdraw,
)
from okx_nft_bot.undercutter.state import PositionState


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=True,
    )


def test_execution_parser_exposes_only_execution_commands() -> None:
    parser = build_parser()
    choices = set(parser._subparsers._group_actions[0].choices.keys())  # type: ignore[attr-defined]
    assert choices == {
        "preview-counterbid",
        "counterbid-scan",
        "counterbid-run",
        "counterbid-config",
        "counterbid-status",
        "undercut-run",
        "undercut-daemon",
        "undercut-status",
        "undercut-history",
        "undercut-withdraw",
        "reconcile-state",
        "audit-state",
        "arm-live",
        "disarm-live",
    }
    parsed = parser.parse_args(["undercut-daemon"])
    assert parsed.cycles == 0


def test_preview_counterbid_cli_smoke(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings), \
         patch("okx_nft_bot.execution_cli.preview_counterbid", return_value={"dry_run": True, "signature": "0xabc"}):
        assert cmd_preview_counterbid(collection="0xabc", price=0.5, chain="bsc") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["signature"] == "0xabc"


def test_counterbid_config_cli_roundtrip(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings):
        assert cmd_counterbid_config(
            action="add",
            address="0xabc",
            chain="bsc",
            min_price=0.1,
            max_price=1.0,
            margin=0.001,
        ) == 0
        add_payload = json.loads(capsys.readouterr().out)
        assert add_payload["address"] == "0xabc"

        assert cmd_counterbid_config(
            action="list",
            address=None,
            chain="bsc",
            min_price=0.1,
            max_price=1.0,
            margin=0.001,
        ) == 0
        listed = json.loads(capsys.readouterr().out)
        assert len(listed) == 1


def test_counterbid_scan_and_run_cli_use_batch_result(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    fake_result = MagicMock()
    fake_result.to_dict.return_value = {"chain": "bsc", "tasks": [], "valid_count": 0, "invalid_count": 0, "refresh_results": []}
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings), \
         patch("okx_nft_bot.execution_cli.CounterBidder") as mock_bidder:
        mock_bidder.return_value.process_batch.return_value = fake_result
        assert cmd_counterbid_scan(collection=None, chain="bsc", refresh=False) == 0
        scan_payload = json.loads(capsys.readouterr().out)
        assert scan_payload["chain"] == "bsc"
        assert cmd_counterbid_run(collection=None, chain="bsc", refresh=True) == 0
        run_payload = json.loads(capsys.readouterr().out)
        assert run_payload["chain"] == "bsc"


def test_undercut_status_history_and_withdraw_cli_roundtrip(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="dryrun-1", collection="0xabc", chain="bsc", price_bnb=0.5)
    state.log_action(
        action_type="ATTACK",
        collection="0xabc",
        chain="bsc",
        order_hash="dryrun-1",
        old_price_bnb=None,
        new_price_bnb=0.5,
        reason="seeded",
        executed=True,
    )

    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings), \
         patch("okx_nft_bot.execution_cli.UndercutEngine") as mock_engine:
        mock_engine.return_value.status.return_value = {"active_offers": [{"order_hash": "dryrun-1"}], "recent_actions": []}
        assert cmd_undercut_status(chain="bsc") == 0
        status_payload = json.loads(capsys.readouterr().out)
        assert status_payload["active_offers"][0]["order_hash"] == "dryrun-1"

    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings):
        assert cmd_undercut_history(collection="0xabc", limit=5) == 0
        history_payload = json.loads(capsys.readouterr().out)
        assert history_payload[0]["action_type"] == "ATTACK"

        assert cmd_undercut_withdraw(collection="0xabc") == 0
        withdraw_payload = json.loads(capsys.readouterr().out)
        assert withdraw_payload["withdrawn"] == 1


def test_execution_cli_rejects_non_bsc_chain(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings):
        with pytest.raises(SystemExit, match="only --chain bsc"):
            cmd_counterbid_scan(collection=None, chain="eth", refresh=False)


def test_reconcile_state_cli_uses_governor(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings), \
         patch("okx_nft_bot.execution_cli.ExecutionGovernor") as mock_governor:
        mock_governor.return_value.reconcile_active_offers.return_value.to_dict.return_value = {
            "chain": "bsc",
            "exchange_seen": 2,
            "local_marked_missing": 1,
        }
        assert cmd_reconcile_state(chain="bsc") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["exchange_seen"] == 2
    mock_governor.return_value.reconcile_active_offers.assert_called_once_with(chain="bsc")


def test_audit_state_cli_reports_integrity_summary(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    state.set_runtime_value("live_armed_until", "bad-ts")

    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings):
        assert cmd_audit_state() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["issue_count"] >= 1
    assert "live_armed_until" in payload["runtime_keys_cleared"]


def test_arm_and_disarm_live_cli_roundtrip(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    with patch("okx_nft_bot.execution_cli.load_settings", return_value=settings):
        assert cmd_arm_live(minutes=15, reason="unit") == 0
        arm_payload = json.loads(capsys.readouterr().out)
        assert arm_payload["armed"] is True

        assert cmd_disarm_live(reason="done") == 0
        disarm_payload = json.loads(capsys.readouterr().out)
        assert disarm_payload["armed"] is False
