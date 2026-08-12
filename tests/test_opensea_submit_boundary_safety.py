from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient


PARAMETERS = {
    "offerer": "0x" + "1" * 40,
    "offer": [{"startAmount": "1000000000000000"}],
}


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _client(response=None):
    client = OpenSeaClient.__new__(OpenSeaClient)
    client.settings = SimpleNamespace(
        opensea_api_key="test-key",
        opensea_api_base="https://api.opensea.test/api",
    )
    client.transport = _Transport(response if response is not None else {})
    return client


def test_opensea_submit_blocks_before_http_post(monkeypatch):
    client = _client({"order_hash": "0xshould-not-send"})
    monkeypatch.setattr(
        client,
        "_live_submit_block_reason",
        lambda **_kwargs: "live arm required",
    )

    with pytest.raises(RuntimeError, match="OpenSea live submit blocked: live arm required"):
        client._submit_opensea_offer(PARAMETERS, "0xsig", "eth")

    assert client.transport.calls == []


def test_opensea_submit_without_exchange_id_is_failure(monkeypatch):
    client = _client({"status": "ok"})
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **_kwargs: None)

    with pytest.raises(
        RuntimeError,
        match="Failed to submit offer to OpenSea: OpenSea submit response missing order id",
    ):
        client._submit_opensea_offer(PARAMETERS, "0xsig", "eth")

    assert len(client.transport.calls) == 1


@pytest.mark.parametrize("placeholder", ["pending", " none ", "NULL", ""])
def test_opensea_placeholder_id_is_not_success(monkeypatch, placeholder):
    client = _client({"order_hash": placeholder})
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="OpenSea submit response missing order id"):
        client._submit_opensea_offer(PARAMETERS, "0xsig", "eth")

    assert len(client.transport.calls) == 1


def test_opensea_real_exchange_id_preserves_success(monkeypatch):
    client = _client({"order_hash": "0xabc123"})
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **_kwargs: None)

    result = client._submit_opensea_offer(PARAMETERS, "0xsig", "ethereum")

    assert result["offer_id"] == "0xabc123"
    assert result["order_id"] == "0xabc123"
    assert result["status"] == "submitted"
    assert len(client.transport.calls) == 1


class _State:
    def __init__(self, *, failed=None, force_dry=False):
        self.failed = failed or []
        self.force_dry = force_dry

    def get_killswitch_failed_offers(self, *, chain):
        assert chain == "eth"
        return self.failed

    def is_force_dry_run(self):
        return self.force_dry


class _Governor:
    effective = False
    arm_state = {"armed": True, "expires_at": None}
    state = _State()

    def __init__(self, *, settings, **_kwargs):
        self.settings = settings
        self.state = self.__class__.state

    def effective_dry_run(self):
        return self.__class__.effective

    def get_live_arm_state(self):
        return dict(self.__class__.arm_state)

    def check_live_submit_allowed(self, **_kwargs):
        pytest.fail("OpenSea mirror safety recheck must not re-apply cooldown/spend gates")


def _install_governor(monkeypatch, *, effective=False, failed=None, force_dry=False, armed=True, expires_at=None):
    import okx_nft_bot.execution_governor as governor_module

    _Governor.effective = effective
    _Governor.state = _State(failed=failed, force_dry=force_dry)
    _Governor.arm_state = {"armed": armed, "expires_at": expires_at}
    monkeypatch.setattr(governor_module, "ExecutionGovernor", _Governor)


def test_opensea_boundary_recheck_is_safety_only(monkeypatch):
    _install_governor(monkeypatch, armed=True)
    client = _client()

    assert client._live_submit_block_reason(chain="eth") is None


def test_opensea_boundary_recheck_blocks_force_dry(monkeypatch):
    _install_governor(monkeypatch, effective=True, force_dry=True)
    client = _client()

    assert client._live_submit_block_reason(chain="eth") == "dry_run_enabled"


def test_opensea_boundary_recheck_blocks_zombie_killswitch(monkeypatch):
    _install_governor(monkeypatch, failed=[object()])
    client = _client()

    assert client._live_submit_block_reason(chain="eth").startswith("killswitch_failed:")


def test_opensea_boundary_recheck_blocks_expired_arm(monkeypatch):
    _install_governor(
        monkeypatch,
        armed=False,
        expires_at="2026-08-12T00:00:00+00:00",
    )
    client = _client()

    assert client._live_submit_block_reason(chain="eth") == "live arm expired"
