from pathlib import Path

from okx_nft_bot.alerts.filters import evaluate_event
from okx_nft_bot.config import Settings
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.rules.rule_packs import RulePack


def build_settings() -> Settings:
    return Settings(
        app_env="test",
        db_path=Path("test.sqlite3"),
        okx_api_base="https://web3.okx.com",
        okx_api_key=None,
        okx_api_secret=None,
        okx_api_passphrase=None,
        okx_chain="eth",
        okx_collection_address=None,
        okx_collection_slug=None,
        okx_platform=None,
        okx_page_limit=20,
        okx_request_timeout=20,
        okx_max_retries=3,
        okx_rate_limit_per_sec=5.0,
        okx_enable_details=False,
        okx_max_pages_per_run=5,
        okx_cursor_namespace="test",
        collection_allowlist=("Allowed",),
        min_price=2.0,
        min_volume=100.0,
        rules_path=Path("rule_packs.json"),
        telegram_bot_token=None,
        telegram_chat_id=None,
        webhook_url=None,
        notification_mode="passed_only",
    )


def test_filters_block_non_matching_collection_and_price() -> None:
    settings = build_settings()
    event = NFTEvent(
        event_id="e1",
        market="okx",
        event_type="sale",
        collection="Other",
        token_id="1",
        price=1.0,
        currency="ETH",
        quantity=1,
        event_time="2026-03-07T10:00:00+00:00",
        volume_24h=50.0,
        raw_source="test",
    )

    decision = evaluate_event(event, settings)

    assert decision.passed is False
    assert "collection_not_allowlisted" in decision.reasons
    assert "price_below_min" in decision.reasons
    assert "volume_below_min" in decision.reasons


def test_rule_pack_match_is_recorded() -> None:
    settings = build_settings()
    event = NFTEvent(
        event_id="e2",
        market="okx",
        event_type="sale",
        collection="Allowed",
        token_id="1",
        price=3.0,
        currency="ETH",
        quantity=1,
        event_time="2026-03-07T10:00:00+00:00",
        volume_24h=150.0,
        raw_source="test",
    )
    packs = [RulePack(name="high_value", min_price=2.5, markets=("okx",), event_types=("sale",))]

    decision = evaluate_event(event, settings, rule_packs=packs)

    assert decision.passed is True
    assert decision.matched_rules == ["high_value"]
