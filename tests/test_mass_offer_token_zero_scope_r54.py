from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.mass_offer.engine import MassOfferEngine
from okx_nft_bot.mass_offer.token_scope_safety import install_mass_offer_token_scope_safety


class _Governor:
    def __init__(self, *, effective_dry_run: bool = False) -> None:
        self._effective_dry_run = effective_dry_run

    def effective_dry_run(self, requested: bool) -> bool:
        return self._effective_dry_run

    def check_live_submit_allowed(self, **kwargs):
        return None


class _API:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_offer(self, **kwargs):
        self.calls.append(kwargs)
        return {"offer_id": "offer-r54", "status": "submitted"}


class _State:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert_active_offer(self, **kwargs):
        self.upserts.append(kwargs)


def _engine(*, dry_run: bool = False) -> MassOfferEngine:
    engine = object.__new__(MassOfferEngine)
    engine.settings = SimpleNamespace(
        mass_offer_duration_hours=24,
        mass_offer_dry_run=False,
    )
    engine.governor = _Governor(effective_dry_run=dry_run)
    engine.api_client = _API()
    engine.state = _State()
    engine._load_buyer_account = lambda: SimpleNamespace(address="0x0000000000000000000000000000000000000001")
    return engine


def _place(engine: MassOfferEngine, token_id):
    return engine.place_single_offer(
        collection_address="0x0000000000000000000000000000000000000002",
        token_id=token_id,
        price_wbnb=0.01,
        currency_address="0x0000000000000000000000000000000000000003",
        chain="bsc",
        duration_hours=1,
        dry_run=False,
    )


@pytest.mark.parametrize("token_id", [0, "0"])
def test_r54_literal_zero_reaches_okx_as_item_zero(token_id):
    engine = _engine()

    result = _place(engine, token_id)

    assert result == (True, None)
    assert len(engine.api_client.calls) == 1
    call = engine.api_client.calls[0]
    assert call["token_id"] == 0
    assert isinstance(call["token_id"], int)
    assert engine.state.upserts[0]["order_hash"] == "offer-r54"


@pytest.mark.parametrize("token_id", ["", "col", "collection", False])
def test_r54_existing_collection_aliases_are_unchanged(token_id):
    engine = _engine()

    result = _place(engine, token_id)

    assert result == (True, None)
    assert engine.api_client.calls[0]["token_id"] == ""


def test_r54_nonzero_item_semantics_are_unchanged():
    engine = _engine()

    result = _place(engine, 7)

    assert result == (True, None)
    assert engine.api_client.calls[0]["token_id"] == 7


@pytest.mark.parametrize("token_id", [0, "0"])
def test_r54_dry_run_zero_stays_item_zero_without_api_effect(token_id):
    engine = _engine(dry_run=True)

    result = _place(engine, token_id)

    assert result == (True, None)
    assert engine.api_client.calls == []
    assert engine.state.upserts[0]["order_hash"] == "dryrun:single:0"


def test_r54_installer_is_active_and_idempotent():
    before = MassOfferEngine.place_single_offer
    assert getattr(before, "_r54_mass_offer_token_zero_scope", False) is True

    install_mass_offer_token_scope_safety(MassOfferEngine)

    assert MassOfferEngine.place_single_offer is before
