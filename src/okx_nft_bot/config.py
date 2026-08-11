from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


_SECRET_FIELDS = frozenset({
    "okx_api_key", "okx_api_secret", "okx_api_passphrase",
    "opensea_api_key", "magiceden_api_key",
    "telegram_bot_token",
    "buyer_wallet_private_key",
    "webhook_url",
})


@dataclass(slots=True)
class Settings:
    app_env: str
    db_path: Path
    active_market: str = 'okx'
    okx_api_base: str = 'https://web3.okx.com'
    okx_api_key: str | None = None
    okx_api_secret: str | None = None
    okx_api_passphrase: str | None = None
    okx_chain: str = 'eth'
    okx_collection_address: str | None = None
    okx_collection_slug: str | None = None
    okx_platform: str | None = None
    okx_page_limit: int = 20
    okx_request_timeout: int = 20
    okx_max_retries: int = 4
    okx_rate_limit_per_sec: float = 1.5
    okx_enable_details: bool = False
    okx_max_pages_per_run: int = 5
    okx_cursor_namespace: str = 'default'
    opensea_api_base: str = 'https://api.opensea.io'
    opensea_api_key: str | None = None
    opensea_collection_slug: str | None = None
    opensea_chain: str = 'ethereum'
    opensea_request_timeout: int = 20
    opensea_max_retries: int = 4
    opensea_rate_limit_per_sec: float = 2.0
    opensea_page_limit: int = 20
    opensea_max_pages_per_run: int = 5
    opensea_cursor_namespace: str = 'default'
    magiceden_api_base: str = 'https://api-mainnet.magiceden.dev'
    magiceden_api_key: str | None = None
    magiceden_chain: str = 'bsc'
    magiceden_collection_address: str | None = None
    magiceden_collection_slug: str | None = None
    magiceden_request_timeout: int = 20
    magiceden_max_retries: int = 4
    magiceden_rate_limit_per_sec: float = 2.0
    magiceden_page_limit: int = 20
    magiceden_cursor_namespace: str = 'default'
    collection_allowlist: tuple[str, ...] = ()
    min_price: float | None = None
    min_volume: float | None = None
    rules_path: Path = Path('./config/rule_packs.json')
    registry_path: Path = Path('./config/collections_registry.json')
    scheduler_interval_seconds: int = 60
    daemon_max_cycles: int | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_poll_timeout: int = 10
    telegram_admin_chat_ids: tuple[str, ...] = ()
    webhook_url: str | None = None
    notification_mode: str = 'passed_only'
    metrics_path: Path = Path('./data/runtime_metrics.json')
    health_max_staleness_seconds: int = 300
    health_alert_cooldown_seconds: int = 900
    app_profile: str = 'dev'
    profiles_dir: Path = Path('./deploy/profiles')
    backup_dir: Path = Path('./data/backups')
    # v13+v15: Rival wallet addresses for counter-bidding detection
    rival_wallets: tuple[str, ...] = ()
    offers_db_path: Path = Path('./data/offers.sqlite3')
    offers_max_pages_per_run: int = 5
    binance_whitelist_path: Path = Path('./data/binance_whitelist.json')
    binance_whitelist_timeout_seconds: int = 10
    # Sniper / buyer wallet
    buyer_wallet_address: str | None = None
    buyer_wallet_private_key: str | None = None
    sniper_config_path: Path = Path('./config/sniper_config.json')
    dry_run: bool = True
    execution_db_path: Path = Path('./data/execution.sqlite3')
    execution_chain: str = 'bsc'
    mass_offer_price_bnb: float = 0.01
    mass_offer_duration_hours: int = 24
    mass_offer_delay_seconds: float = 2.0
    mass_offer_max_total: int = 100
    mass_offer_dry_run: bool = True
    undercut_interval_seconds: int = 30
    undercut_max_offer_age_hours: int = 24
    undercut_attack_ratio: float = 0.75
    undercut_defense_margin_bnb: float = 0.0005
    undercut_max_active_offers: int = 10
    max_live_offers_per_hour: int = 10
    max_bnb_per_day: float = 5.0
    submit_cooldown_seconds: int = 30
    execution_reconcile_max_staleness_seconds: int = 900
    # Sales Stream Daemon
    sales_db_path: Path = Path('./data/sales_stream.sqlite3')
    sales_poll_interval: int = 30
    sales_markets: str = 'okx,opensea,magiceden'
    sales_daemon_max_cycles: int | None = None

    # === GPT-Pro additions 2026-04-27 ===
    okx_rate_limit_per_sec: float = 3.0
    parasite_wallets: tuple[str, ...] = ()
    mass_offer_policy_path: Path = Path('./config/mass_offer_policy.json')
    mass_offer_allocator_policy_path: Path = Path('./config/mass_offer_allocator_policy.json')
    mass_offer_feedback_policy_path: Path = Path('./config/mass_offer_feedback_policy.json')
    mass_offer_budget_policy_path: Path = Path('./config/mass_offer_budget_policy.json')
    mass_offer_rebalance_policy_path: Path = Path('./config/mass_offer_rebalance_policy.json')
    mass_offer_quarantine_policy_path: Path = Path('./config/mass_offer_quarantine_policy.json')
    mass_offer_unwind_report_path: Path = Path('./data/mass_offer_unwind.json')
    mass_offer_economics_report_path: Path = Path('./data/mass_offer_economics.json')
    mass_offer_allocator_report_path: Path = Path('./data/mass_offer_allocator.json')
    mass_offer_feedback_report_path: Path = Path('./data/mass_offer_feedback.json')
    mass_offer_budget_report_path: Path = Path('./data/mass_offer_budget.json')
    mass_offer_rebalance_report_path: Path = Path('./data/mass_offer_rebalance.json')
    mass_offer_quarantine_report_path: Path = Path('./data/mass_offer_quarantine.json')
    mass_offer_plan_report_path: Path = Path('./data/mass_offer_plan.json')
    mass_offer_batch_report_path: Path = Path('./data/mass_offer_batch.json')
    mass_offer_circuit_report_path: Path = Path('./data/mass_offer_circuit.json')
    mass_offer_batch_collection_limit: int = 3
    mass_offer_batch_include_dry_run_collections: bool = False
    mass_offer_batch_sync_policies_before_run: bool = True
    mass_offer_batch_refresh_policies_after_run: bool = True
    mass_offer_batch_sync_budget_before_run: bool = True
    mass_offer_batch_refresh_budget_after_run: bool = True
    mass_offer_batch_sync_rebalance_before_run: bool = True
    mass_offer_batch_refresh_rebalance_after_run: bool = True
    mass_offer_batch_sync_quarantine_before_run: bool = True
    mass_offer_batch_refresh_quarantine_after_run: bool = True
    mass_offer_batch_check_circuit_before_run: bool = True
    mass_offer_batch_check_circuit_during_run: bool = True
    mass_offer_economics_window_days: int = 30
    mass_offer_allocator_window_days: int = 30
    mass_offer_feedback_window_days: int = 14
    mass_offer_circuit_window_hours: int = 12
    mass_offer_economics_event_limit: int = 10000
    mass_offer_budget_caution_multiplier: float = 0.55
    mass_offer_budget_reserve_bnb: float = 0.0
    mass_offer_rebalance_window_days: int = 14
    mass_offer_quarantine_window_days: int = 14
    mass_offer_quarantine_cooldown_hours: int = 12
    mass_offer_quarantine_dry_run_hours: int = 36
    mass_offer_quarantine_block_hours: int = 72
    mass_offer_unwind_window_days: int = 14
    mass_offer_unwind_target_release_bnb: float = 0.0
    mass_offer_unwind_max_cancels: int = 5
    mass_offer_unwind_block_release_fraction: float = 0.25
    mass_offer_circuit_enabled: bool = True
    mass_offer_circuit_blocks_live: bool = True
    mass_offer_circuit_min_live_campaigns: int = 2
    mass_offer_circuit_min_target_total: int = 3
    mass_offer_circuit_max_failed_ratio: float = 0.6
    mass_offer_circuit_max_blocked_ratio: float = 0.5
    mass_offer_circuit_max_no_submit_ratio: float = 0.5
    mass_offer_circuit_caution_factor: float = 0.6
    wallet_pnl_report_path: Path = Path('./data/wallet_pnl_report.json')
    wallet_pnl_reference_event_limit: int = 20000
    execution_fill_report_path: Path = Path('./data/execution_fill_report.json')
    execution_fill_reference_event_limit: int = 20000
    execution_fill_reconcile_window_hours: int = 72
    execution_fill_price_tolerance_pct: float = 0.03
    execution_fill_pre_submit_slack_minutes: int = 5
    execution_health_enabled: bool = True
    execution_health_auto_force_dry_run: bool = True
    execution_health_report_path: Path = Path('./data/execution_health_report.json')
    execution_health_window_hours: int = 6
    execution_health_event_limit: int = 5000
    execution_health_min_attempts: int = 5
    execution_health_max_failure_ratio: float = 0.5
    execution_health_max_consecutive_failures: int = 4
    execution_health_failure_burst_window_minutes: int = 15
    execution_health_max_failure_burst: int = 3
    execution_health_caution_factor: float = 0.6
    pnl_guard_enabled: bool = True
    pnl_guard_auto_force_dry_run: bool = True
    pnl_guard_report_path: Path = Path('./data/pnl_guard_report.json')
    pnl_guard_window_hours: int = 24
    pnl_guard_max_daily_loss_bnb: float = 0.5
    pnl_guard_profit_lock_bnb: float = 0.0
    pnl_guard_max_recent_loss_streak: int = 4
    pnl_guard_min_closed_positions_for_profit_lock: int = 2
    portfolio_risk_enabled: bool = True
    portfolio_risk_auto_force_dry_run: bool = True
    portfolio_risk_report_path: Path = Path('./data/portfolio_risk_report.json')
    portfolio_risk_max_orphan_sales: int = 5
    portfolio_risk_max_unpriced_positions: int = 25
    portfolio_risk_max_unpriced_ratio: float = 0.5
    portfolio_risk_min_inventory_cost: float = 1.0
    portfolio_risk_max_drawdown_pct: float = 0.3
    portfolio_risk_max_reconcile_age_seconds: int = 1800
    portfolio_risk_max_fill_reconcile_age_seconds: int = 3600
    portfolio_risk_max_active_exposure_bnb: float | None = None
    portfolio_risk_max_killswitch_failed: int = 0

    def __repr__(self) -> str:
        """Redact sensitive fields (API keys, private keys, tokens) from repr."""
        import dataclasses
        parts = []
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if f.name in _SECRET_FIELDS and val:
                val_str = repr(str(val)[:4] + "***REDACTED***")
            else:
                val_str = repr(val)
            parts.append(f"{f.name}={val_str}")
        return f"Settings({', '.join(parts)})"


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(',') if part.strip())


def _optional_float(value: str | None) -> float | None:
    if value is None or value == '':
        return None
    return float(value)


def _optional_int(value: str | None) -> int | None:
    if value is None or value == '':
        return None
    return int(value)


def _optional_bool(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _safe_bool(env_name: str, default: bool = True) -> bool:
    """Read a bool env var with a safe default when unset or blank.

    Empty string or whitespace-only values return ``default`` (not False).
    Prevents ``DRY_RUN=`` (blank) from silently disabling safety guards.
    """
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    return _optional_bool(val)


def _ensure_parent(p: Path) -> Path:
    """Create parent directory for a path if it doesn't exist."""
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


SUPPORTED_EXECUTION_CHAINS: tuple[str, ...] = ("bsc", "eth")


def validate_execution_chain(chain: str) -> str:
    resolved = chain.lower()
    if resolved not in SUPPORTED_EXECUTION_CHAINS:
        raise ValueError(
            f"chain {resolved!r} not in supported: {SUPPORTED_EXECUTION_CHAINS}"
        )
    return resolved


_DEFAULT_RPC_URLS: dict[str, tuple[str, ...]] = {
    "bsc": (
        "https://bsc-dataseed.binance.org/",
        "https://bsc-dataseed1.defibit.io/",
        "https://bsc.nodereal.io",
    ),
    "eth": (
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
    ),
    "polygon": ("https://polygon-rpc.com/",),
    "arbitrum": ("https://arb1.arbitrum.io/rpc",),
}


def get_rpc_urls(chain: str) -> tuple[str, ...]:
    """Return the ordered RPC URL list for a chain (primary first).

    Reads from env ``<CHAIN>_RPC_URLS`` (comma-separated) and falls back
    to the built-in defaults. Keeps backward compatibility with the legacy
    ``BUYER_RPC_URL`` / ``BUYER_RPC_URL_ETH`` single-URL overrides by
    prepending them to the list when set.
    """
    key = chain.lower()
    env_list = _split_csv(os.getenv(f"{key.upper()}_RPC_URLS"))
    legacy_single: str | None = None
    if key == "bsc":
        legacy_single = os.getenv("BUYER_RPC_URL")
    elif key == "eth":
        legacy_single = os.getenv("BUYER_RPC_URL_ETH")
    urls: list[str] = []
    if legacy_single:
        urls.append(legacy_single.strip())
    urls.extend(env_list)
    urls.extend(_DEFAULT_RPC_URLS.get(key, ()))
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return tuple(out)


def load_settings(profile_override: str | None = None) -> Settings:
    # Preserve the environment provided by the process/container. Base .env is
    # allowed to fill gaps and the selected profile may override those defaults,
    # but a profile must never weaken an explicit runtime safety override.
    process_env = dict(os.environ)
    load_dotenv(override=False)
    profile = (profile_override or os.getenv('APP_PROFILE') or 'dev').strip().lower()
    profiles_dir = Path(os.getenv('PROFILES_DIR', './deploy/profiles'))
    profile_path = profiles_dir / f'{profile}.env'
    if profile_path.exists():
        load_dotenv(profile_path, override=True)
        os.environ.update(process_env)

    db_path = Path(os.getenv('DB_PATH', './data/okx_nft_bot.sqlite3'))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path = Path(os.getenv('RULES_PATH', './config/rule_packs.json'))
    registry_path = Path(os.getenv('REGISTRY_PATH', './config/collections_registry.json'))
    metrics_path = Path(os.getenv('METRICS_PATH', './data/runtime_metrics.json'))
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(os.getenv('BACKUP_DIR', './data/backups'))
    backup_dir.mkdir(parents=True, exist_ok=True)
    execution_db_path = Path(os.getenv('EXECUTION_DB_PATH', './data/execution.sqlite3'))
    execution_db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env=os.getenv('APP_ENV', 'dev'),
        db_path=db_path,
        active_market=os.getenv('ACTIVE_MARKET', 'okx').strip().lower(),
        okx_api_base=os.getenv('OKX_API_BASE', 'https://web3.okx.com'),
        okx_api_key=os.getenv('OKX_API_KEY') or None,
        okx_api_secret=os.getenv('OKX_API_SECRET') or None,
        okx_api_passphrase=os.getenv('OKX_API_PASSPHRASE') or None,
        okx_chain=os.getenv('OKX_CHAIN', 'eth'),
        okx_collection_address=os.getenv('OKX_COLLECTION_ADDRESS') or None,
        okx_collection_slug=os.getenv('OKX_COLLECTION_SLUG') or None,
        okx_platform=os.getenv('OKX_PLATFORM') or None,
        okx_page_limit=int(os.getenv('OKX_PAGE_LIMIT', '20')),
        okx_request_timeout=int(os.getenv('OKX_REQUEST_TIMEOUT', '20')),
        okx_max_retries=int(os.getenv('OKX_MAX_RETRIES', '4')),
        okx_rate_limit_per_sec=float(os.getenv('OKX_RATE_LIMIT_PER_SEC', '3.0')) / max(
            int(os.getenv('CONTAINER_COUNT_FOR_RATE_SPLIT', '1')), 1
        ),
        okx_enable_details=_optional_bool(os.getenv('OKX_ENABLE_DETAILS')),
        okx_max_pages_per_run=int(os.getenv('OKX_MAX_PAGES_PER_RUN', '5')),
        okx_cursor_namespace=os.getenv('OKX_CURSOR_NAMESPACE', 'trades'),
        opensea_api_base=os.getenv('OPENSEA_API_BASE', 'https://api.opensea.io'),
        opensea_api_key=os.getenv('OPENSEA_API_KEY') or None,
        opensea_collection_slug=os.getenv('OPENSEA_COLLECTION_SLUG') or None,
        opensea_chain=os.getenv('OPENSEA_CHAIN', 'ethereum'),
        opensea_request_timeout=int(os.getenv('OPENSEA_REQUEST_TIMEOUT', os.getenv('OKX_REQUEST_TIMEOUT', '20'))),
        opensea_max_retries=int(os.getenv('OPENSEA_MAX_RETRIES', os.getenv('OKX_MAX_RETRIES', '4'))),
        opensea_rate_limit_per_sec=float(os.getenv('OPENSEA_RATE_LIMIT_PER_SEC', '2.0')),
        opensea_page_limit=int(os.getenv('OPENSEA_PAGE_LIMIT', '20')),
        opensea_max_pages_per_run=int(os.getenv('OPENSEA_MAX_PAGES_PER_RUN', '5')),
        opensea_cursor_namespace=os.getenv('OPENSEA_CURSOR_NAMESPACE', 'events'),
        magiceden_api_base=os.getenv('MAGICEDEN_API_BASE', 'https://api-mainnet.magiceden.dev'),
        magiceden_api_key=os.getenv('MAGICEDEN_API_KEY') or None,
        magiceden_chain=os.getenv('MAGICEDEN_CHAIN', 'bsc'),
        magiceden_collection_address=os.getenv('MAGICEDEN_COLLECTION_ADDRESS') or None,
        magiceden_collection_slug=os.getenv('MAGICEDEN_COLLECTION_SLUG') or None,
        magiceden_request_timeout=int(os.getenv('MAGICEDEN_REQUEST_TIMEOUT', '20')),
        magiceden_max_retries=int(os.getenv('MAGICEDEN_MAX_RETRIES', '4')),
        magiceden_rate_limit_per_sec=float(os.getenv('MAGICEDEN_RATE_LIMIT_PER_SEC', '2.0')),
        magiceden_page_limit=int(os.getenv('MAGICEDEN_PAGE_LIMIT', '20')),
        magiceden_cursor_namespace=os.getenv('MAGICEDEN_CURSOR_NAMESPACE', 'default'),
        collection_allowlist=_split_csv(os.getenv('COLLECTION_ALLOWLIST')),
        min_price=_optional_float(os.getenv('MIN_PRICE')),
        min_volume=_optional_float(os.getenv('MIN_VOLUME')),
        rules_path=rules_path,
        registry_path=registry_path,
        scheduler_interval_seconds=int(os.getenv('SCHEDULER_INTERVAL_SECONDS', '60')),
        daemon_max_cycles=_optional_int(os.getenv('DAEMON_MAX_CYCLES')),
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN') or None,
        telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID') or None,
        telegram_poll_timeout=int(os.getenv('TELEGRAM_POLL_TIMEOUT', '10')),
        telegram_admin_chat_ids=_split_csv(os.getenv('TELEGRAM_ADMIN_CHAT_IDS')),
        webhook_url=os.getenv('WEBHOOK_URL') or None,
        notification_mode=os.getenv('NOTIFICATION_MODE', 'passed_only').strip().lower(),
        metrics_path=metrics_path,
        health_max_staleness_seconds=int(os.getenv('HEALTH_MAX_STALENESS_SECONDS', '300')),
        health_alert_cooldown_seconds=int(os.getenv('HEALTH_ALERT_COOLDOWN_SECONDS', '900')),
        app_profile=profile,
        profiles_dir=profiles_dir,
        backup_dir=backup_dir,
        rival_wallets=_split_csv(os.getenv('RIVAL_WALLETS')),
        offers_db_path=_ensure_parent(Path(os.getenv('OFFERS_DB_PATH', './data/offers.sqlite3'))),
        offers_max_pages_per_run=int(os.getenv('OFFERS_MAX_PAGES_PER_RUN', '5')),
        binance_whitelist_path=Path(os.getenv('BINANCE_WHITELIST_PATH', './data/binance_whitelist.json')),
        binance_whitelist_timeout_seconds=int(os.getenv('BINANCE_WHITELIST_TIMEOUT_SECONDS', '10')),
        buyer_wallet_address=os.getenv('BUYER_WALLET_ADDRESS') or None,
        buyer_wallet_private_key=os.getenv('BUYER_WALLET_PRIVATE_KEY') or None,
        sniper_config_path=Path(os.getenv('SNIPER_CONFIG_PATH', './config/sniper_config.json')),
        dry_run=_safe_bool('DRY_RUN', default=True),
        execution_db_path=execution_db_path,
        execution_chain=os.getenv('EXECUTION_CHAIN', 'bsc'),
        mass_offer_price_bnb=float(os.getenv('MASS_OFFER_PRICE_BNB', '0.01')),
        mass_offer_duration_hours=int(os.getenv('MASS_OFFER_DURATION_HOURS', '24')),
        mass_offer_delay_seconds=float(os.getenv('MASS_OFFER_DELAY_SECONDS', '2')),
        mass_offer_max_total=int(os.getenv('MASS_OFFER_MAX_TOTAL', '100')),
        mass_offer_dry_run=_safe_bool('MASS_OFFER_DRY_RUN', default=True),
        undercut_interval_seconds=int(os.getenv('UNDERCUT_INTERVAL_SECONDS', '30')),
        undercut_max_offer_age_hours=int(os.getenv('UNDERCUT_MAX_OFFER_AGE_HOURS', '24')),
        undercut_attack_ratio=float(os.getenv('UNDERCUT_ATTACK_RATIO', '0.75')),
        undercut_defense_margin_bnb=float(os.getenv('UNDERCUT_DEFENSE_MARGIN_BNB', '0.0005')),
        undercut_max_active_offers=int(os.getenv('UNDERCUT_MAX_ACTIVE_OFFERS', '10')),
        max_live_offers_per_hour=int(os.getenv('MAX_LIVE_OFFERS_PER_HOUR', '10')),
        max_bnb_per_day=float(os.getenv('MAX_BNB_PER_DAY', '5.0')),
        submit_cooldown_seconds=int(os.getenv('SUBMIT_COOLDOWN_SECONDS', '30')),
        execution_reconcile_max_staleness_seconds=int(os.getenv('EXECUTION_RECONCILE_MAX_STALENESS_SECONDS', '900')),
        sales_db_path=_ensure_parent(Path(os.getenv('SALES_DB_PATH', './data/sales_stream.sqlite3'))),
        sales_poll_interval=int(os.getenv('SALES_POLL_INTERVAL', '30')),
        sales_markets=os.getenv('SALES_MARKETS', 'okx,opensea,magiceden'),
        sales_daemon_max_cycles=_optional_int(os.getenv('SALES_DAEMON_MAX_CYCLES')),
    )