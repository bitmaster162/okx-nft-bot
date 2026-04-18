from __future__ import annotations

import json
from pathlib import Path

import pytest

from okx_nft_bot.cli import cmd_alert_ack, cmd_alert_reset, cmd_alert_snooze, cmd_alert_status
from okx_nft_bot.config import load_settings
from okx_nft_bot.undercutter.state import PositionState


def _seed_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))


def test_cmd_alert_ack_acknowledges_current_issue(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_env(tmp_path, monkeypatch)
    settings = load_settings()
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.mark_offer_status(order_hash='live-offer-1', status='killswitch_failed')

    exit_code = cmd_alert_ack(note='manual_ack')

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['acknowledged'] is True
    assert payload['health_reason'] == 'execution_killswitch_failed'
    assert payload['control']['acknowledged_reason'] == 'execution_killswitch_failed'


def test_cmd_alert_ack_requires_current_alertable_issue(tmp_path: Path, monkeypatch) -> None:
    _seed_env(tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match='No current alertable health issue'):
        cmd_alert_ack(note=None)


def test_cmd_alert_snooze_status_and_reset_roundtrip(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_env(tmp_path, monkeypatch)

    snooze_exit = cmd_alert_snooze(minutes=30, reason='maintenance')
    assert snooze_exit == 0
    snooze_payload = json.loads(capsys.readouterr().out)
    assert snooze_payload['snoozed'] is True
    assert snooze_payload['control']['snoozed'] is True

    status_exit = cmd_alert_status()
    assert status_exit == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload['control']['snoozed'] is True
    assert status_payload['control']['snooze_reason'] == 'maintenance'

    reset_exit = cmd_alert_reset()
    assert reset_exit == 0
    reset_payload = json.loads(capsys.readouterr().out)
    assert reset_payload['reset'] is True
    assert reset_payload['control']['snoozed'] is False
    assert reset_payload['control']['acknowledged_reason'] is None
