from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from okx_nft_bot.analytics.execution_fills import (
    _SubmitCandidate,
    _normalize_token_id,
    _score_candidate,
)
from okx_nft_bot.models import NFTEvent


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
COLLECTION = "0x1111111111111111111111111111111111111111"


@pytest.mark.parametrize("value", [0, "0"])
def test_token_zero_is_preserved_as_item_scope(value) -> None:
    assert _normalize_token_id(value) == "0"


@pytest.mark.parametrize("value", [None, False, "", "col", "collection", "none"])
def test_collection_scope_markers_remain_absent(value) -> None:
    assert _normalize_token_id(value) is None


def _candidate(token_id: str | None) -> _SubmitCandidate:
    return _SubmitCandidate(
        submit_log_id=1,
        order_hash="order-r66-repair",
        engine="test",
        action_type="submit",
        collection=COLLECTION,
        contract_address=COLLECTION,
        token_id=token_id,
        chain="eth",
        currency="ETH",
        submit_price=1.0,
        created_at=NOW,
        raw_reason=None,
    )


def _event(token_id: str) -> NFTEvent:
    return NFTEvent(
        event_id=f"sale-r66-repair-{token_id}",
        market="okx",
        event_type="sale",
        collection=COLLECTION,
        token_id=token_id,
        contract_address=COLLECTION,
        price=1.0,
        currency="ETH",
        quantity=1,
        maker="0xmaker",
        taker="0xtaker",
        tx_hash="0xtx",
        event_time=NOW + timedelta(minutes=1),
        raw_source="test",
    )


def test_token_zero_candidate_does_not_match_other_item() -> None:
    score = _score_candidate(
        _candidate(_normalize_token_id("0")),
        _event("7"),
        window=timedelta(hours=72),
        pre_submit_slack=timedelta(minutes=5),
        price_tolerance_pct=0.03,
    )
    assert score is None


def test_token_zero_candidate_matches_token_zero() -> None:
    score = _score_candidate(
        _candidate(_normalize_token_id("0")),
        _event("0"),
        window=timedelta(hours=72),
        pre_submit_slack=timedelta(minutes=5),
        price_tolerance_pct=0.03,
    )
    assert score is not None
    assert "token_exact" in score.note
