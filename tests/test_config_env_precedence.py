from __future__ import annotations

import os
from pathlib import Path

import okx_nft_bot.config as config


def _fake_dotenv_loader(base_values: dict[str, str], profile_values: dict[str, str]):
    def _load(path=None, *, override=False, **_kwargs):
        values = base_values if path is None else profile_values
        for key, value in values.items():
            if override or key not in os.environ:
                os.environ[key] = value
        return True

    return _load


def test_explicit_process_env_wins_over_base_and_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "prod.env").write_text("placeholder=1\n")

    monkeypatch.setenv("APP_PROFILE", "prod")
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("EXECUTION_CHAIN", "eth")
    monkeypatch.setenv("MAX_BNB_PER_DAY", "0.25")

    monkeypatch.setattr(
        config,
        "load_dotenv",
        _fake_dotenv_loader(
            {
                "DRY_RUN": "0",
                "EXECUTION_CHAIN": "bsc",
                "MAX_BNB_PER_DAY": "9",
            },
            {
                "DRY_RUN": "0",
                "EXECUTION_CHAIN": "bsc",
                "MAX_BNB_PER_DAY": "7",
            },
        ),
    )

    settings = config.load_settings()

    assert settings.dry_run is True
    assert settings.execution_chain == "eth"
    assert settings.max_bnb_per_day == 0.25
    assert os.environ["DRY_RUN"] == "1"
    assert os.environ["EXECUTION_CHAIN"] == "eth"


def test_profile_still_overrides_base_dotenv_when_process_env_is_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "prod.env").write_text("placeholder=1\n")

    for key in ("DRY_RUN", "EXECUTION_CHAIN", "MAX_BNB_PER_DAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_PROFILE", "prod")
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))

    monkeypatch.setattr(
        config,
        "load_dotenv",
        _fake_dotenv_loader(
            {
                "DRY_RUN": "1",
                "EXECUTION_CHAIN": "bsc",
                "MAX_BNB_PER_DAY": "5",
            },
            {
                "DRY_RUN": "0",
                "EXECUTION_CHAIN": "eth",
                "MAX_BNB_PER_DAY": "2",
            },
        ),
    )

    settings = config.load_settings()

    assert settings.dry_run is False
    assert settings.execution_chain == "eth"
    assert settings.max_bnb_per_day == 2.0
