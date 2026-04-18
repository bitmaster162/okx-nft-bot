"""
Configuration Manager v2 - Centralized configuration with validation

Features:
- Type-safe configuration
- Environment variable integration
- Configuration validation
- Hot reloading
- Configuration templates
"""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Union, Type
from decimal import Decimal
from enum import Enum
import asyncio
from datetime import datetime

from pydantic import BaseModel, Field, validator
from pydantic.config import ConfigDict

log = logging.getLogger("config.manager_v2")


class TicketClass(str, Enum):
    LOW = "low_ticket"
    MID = "mid_ticket"
    HIGH = "high_ticket"


class StrategyType(str, Enum):
    HIGH_VOLUME_LOW_MARGIN = "high_volume_low_margin"
    BALANCED_PNL = "balanced_pnl"
    CONSERVATIVE_LIMITS = "conservative_limits"


class ExitStrategy(str, Enum):
    AGGRESSIVE = "aggressive"
    NEUTRAL = "neutral"
    INVENTORY_UNWIND = "inventory_unwind"
    FORCED_RELEASE = "forced_release"


class Currency(str, Enum):
    USDT = "USDT"
    BNB = "BNB"
    WBNB = "WBNB"
    ETH = "ETH"
    WETH = "WETH"


class Chain(str, Enum):
    BSC = "bsc"
    ETH = "eth"
    POLYGON = "polygon"


class Market(str, Enum):
    OKX = "okx"
    OPENSEA = "opensea"
    MAGICEDEN = "magiceden"
    BINANCE = "binance"


class CollectionConfig(BaseModel):
    """Individual collection configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    name: str
    market: Market
    chain: Chain
    collection_address: str = Field(..., min_length=42, max_length=42)
    collection_slug: str
    enabled: bool = True
    
    # Strategy configuration
    ticket_class: TicketClass = TicketClass.MID
    strategy: StrategyType = StrategyType.BALANCED_PNL
    exit_strategy: ExitStrategy = ExitStrategy.NEUTRAL
    
    # Price configuration
    buy_below_price: Decimal = Field(gt=0)
    relist_price: Decimal = Field(gt=0)
    currency: Currency = Currency.USDT
    
    # Limits
    max_buys_per_cycle: int = Field(gt=0, le=100)
    max_total_buys: int = Field(gt=0, le=1000)
    min_relist_profit_pct: float = Field(ge=-100, le=10000)
    
    # Undercut configuration
    undercut_enabled: bool = True
    undercut_bps: int = Field(gt=0, le=1000)
    cancel_above_price: Optional[Decimal] = None
    
    # Advanced options
    binance_whitelist_only: bool = False
    pnl_tracking: bool = True
    dry_run_override: bool = False
    
    # Metadata
    tags: List[str] = field(default_factory=list)
    comment: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    @validator('relist_price')
    def validate_relist_price(cls, v, values):
        if 'buy_below_price' in values and v <= values['buy_below_price']:
            raise ValueError('relist_price must be greater than buy_below_price')
        return v
    
    @validator('cancel_above_price')
    def validate_cancel_above_price(cls, v, values):
        if v is not None and 'buy_below_price' in values and v <= values['buy_below_price']:
            raise ValueError('cancel_above_price must be greater than buy_below_price')
        return v


class PnLConfig(BaseModel):
    """PnL engine configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    enabled: bool = True
    track_metrics: List[str] = field(default_factory=lambda: [
        "realized_pnl", "unrealized_pnl", "fill_rate", "median_time_to_fill",
        "cancel_ratio", "pnl_per_live_offer", "pnl_per_usdt_exposure"
    ])
    
    collection_tracking: Dict[str, Any] = field(default_factory=lambda: {
        "min_trades_for_stats": 3,
        "pnl_threshold": 0.1,
        "exposure_limit": 100
    })
    
    reporting: Dict[str, Any] = field(default_factory=lambda: {
        "export_interval_minutes": 60,
        "keep_history_days": 30,
        "auto_backup": True
    })


class DecisionConfig(BaseModel):
    """Decision engine configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    enabled: bool = True
    confidence_threshold: float = Field(ge=0, le=1, default=0.7)
    ev_threshold: Decimal = Field(ge=0, default=Decimal("0.1"))
    
    factor_weights: Dict[str, Decimal] = field(default_factory=lambda: {
        "floor_price": Decimal("0.15"),
        "spread": Decimal("0.20"),
        "depth_10": Decimal("0.10"),
        "sales_velocity": Decimal("0.15"),
        "parasite_frequency": Decimal("0.10"),
        "inventory_pressure": Decimal("0.10"),
        "price_volatility": Decimal("0.10"),
        "seller_quality": Decimal("0.10")
    })
    
    risk_limits: Dict[str, Decimal] = field(default_factory=lambda: {
        "max_position_size": Decimal("1000"),
        "max_portfolio_exposure": Decimal("10000"),
        "max_drawdown": Decimal("0.20"),
        "min_fill_rate": Decimal("0.30"),
        "max_cancel_ratio": Decimal("0.50")
    })


class ExecutionConfig(BaseModel):
    """Execution governor configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    enabled: bool = True
    dry_run: bool = True
    
    # Rate limiting
    global_offers_per_hour: int = Field(gt=0, le=1000, default=100)
    global_offers_per_minute: int = Field(gt=0, le=60, default=10)
    collection_offers_per_hour: int = Field(gt=0, le=200, default=20)
    collection_offers_per_minute: int = Field(gt=0, le=20, default=5)
    wallet_offers_per_hour: int = Field(gt=0, le=500, default=50)
    cooldown_seconds: int = Field(gt=0, le=300, default=30)
    
    # Safety
    max_exposure_per_collection: Decimal = Field(gt=0, default=Decimal("1000"))
    max_total_exposure: Decimal = Field(gt=0, default=Decimal("10000"))
    max_price_deviation: Decimal = Field(ge=0, le=1, default=Decimal("0.5"))
    emergency_stop_enabled: bool = True
    auto_circuit_breaker: bool = True
    
    # Performance
    max_concurrent_requests: int = Field(gt=0, le=50, default=10)
    request_timeout_seconds: int = Field(gt=0, le=120, default=15)
    retry_policy: Dict[str, Any] = field(default_factory=lambda: {
        "max_retries": 3,
        "backoff_multiplier": 2,
        "initial_delay_ms": 100
    })


class ParasiteHunterConfig(BaseModel):
    """Parasite hunter configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    enabled: bool = True
    dry_run: bool = True
    
    # Basic settings
    max_per_collection: int = Field(gt=0, le=100, default=50)
    undercut_bps: int = Field(gt=0, le=1000, default=50)
    max_usd: Decimal = Field(gt=0, default=Decimal("0.51"))
    delay_seconds: float = Field(gt=0, default=1.0)
    scan_interval: int = Field(gt=0, le=3600, default=300)
    collection_delay: float = Field(gt=0, default=2.0)
    
    # Chains and currencies
    chains: List[Chain] = field(default_factory=lambda: [Chain.BSC, Chain.ETH])
    offer_currencies: List[Currency] = field(default_factory=lambda: [Currency.WBNB, Currency.USDT])
    
    # Non-WL settings
    nonwl_max_usd: Decimal = Field(gt=0, default=Decimal("0.01"))
    nonwl_qty: int = Field(gt=0, le=100, default=10)
    
    # Wallets
    friend_wallets: List[str] = field(default_factory=list)
    buyer_wallet: str = Field(min_length=42, max_length=42)
    
    # Slug mapping
    slug_map: Dict[str, str] = field(default_factory=dict)


class BotConfig(BaseModel):
    """Main bot configuration"""
    model_config = ConfigDict(validate_assignment=True)
    
    # Core components
    pnl: PnLConfig = field(default_factory=PnLConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    parasite_hunter: ParasiteHunterConfig = field(default_factory=ParasiteHunterConfig)
    
    # Collections
    collections: List[CollectionConfig] = field(default_factory=list)
    
    # Global settings
    log_level: str = Field(default="INFO", regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    data_dir: str = "data"
    backup_dir: str = "data/backups"
    
    # API settings
    api_timeout_seconds: int = Field(gt=0, le=300, default=30)
    api_retry_attempts: int = Field(gt=0, le=10, default=3)
    
    # Monitoring
    metrics_enabled: bool = True
    health_check_interval_seconds: int = Field(gt=0, le=300, default=30)
    
    # Telegram
    telegram_enabled: bool = False
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


class ConfigManager:
    """Advanced configuration manager"""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else Path("config/bot_config_v2.json")
        self._config: Optional[BotConfig] = None
        self._watchers: List[asyncio.Task] = []
        self._running = False
    
    async def load(self) -> BotConfig:
        """Load configuration from file and environment"""
        try:
            # Load base config from file
            if self.config_path.exists():
                with open(self.config_path, 'r') as f:
                    config_data = json.load(f)
                config = BotConfig(**config_data)
                log.info(f"✅ Configuration loaded from {self.config_path}")
            else:
                config = BotConfig()
                log.info("📝 Using default configuration")
            
            # Override with environment variables
            config = await self._apply_env_overrides(config)
            
            # Validate configuration
            await self._validate_config(config)
            
            self._config = config
            return config
            
        except Exception as e:
            log.error(f"❌ Failed to load configuration: {e}")
            raise
    
    async def save(self, config: Optional[BotConfig] = None) -> None:
        """Save configuration to file"""
        config_to_save = config or self._config
        if not config_to_save:
            raise ValueError("No configuration to save")
        
        try:
            # Ensure directory exists
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Update timestamp
            config_to_save.updated_at = datetime.now()
            
            # Save to file
            with open(self.config_path, 'w') as f:
                json.dump(config_to_save.model_dump(), f, indent=2, default=str)
            
            log.info(f"💾 Configuration saved to {self.config_path}")
            
        except Exception as e:
            log.error(f"❌ Failed to save configuration: {e}")
            raise
    
    async def _apply_env_overrides(self, config: BotConfig) -> BotConfig:
        """Apply environment variable overrides"""
        env_mappings = {
            # Execution settings
            'BOT_DRY_RUN': ('execution.dry_run', bool),
            'BOT_MAX_OFFERS_PER_HOUR': ('execution.global_offers_per_hour', int),
            'BOT_MAX_TOTAL_EXPOSURE': ('execution.max_total_exposure', Decimal),
            
            # PnL settings
            'BOT_PNL_ENABLED': ('pnl.enabled', bool),
            
            # Decision settings
            'BOT_CONFIDENCE_THRESHOLD': ('decision.confidence_threshold', float),
            
            # Telegram settings
            'TELEGRAM_BOT_TOKEN': ('telegram_bot_token', str),
            'TELEGRAM_CHAT_ID': ('telegram_chat_id', str),
            
            # Log level
            'LOG_LEVEL': ('log_level', str),
        }
        
        for env_var, (config_path, type_func) in env_mappings.items():
            env_value = os.getenv(env_var)
            if env_value is not None:
                try:
                    # Parse value
                    if type_func == bool:
                        parsed_value = env_value.lower() in ('true', '1', 'yes', 'on')
                    elif type_func == Decimal:
                        parsed_value = Decimal(env_value)
                    else:
                        parsed_value = type_func(env_value)
                    
                    # Apply to config
                    self._set_nested_attr(config, config_path, parsed_value)
                    log.debug(f"🔧 Applied env override: {env_var} -> {config_path} = {parsed_value}")
                    
                except (ValueError, AttributeError) as e:
                    log.warning(f"⚠️ Invalid env value {env_var}={env_value}: {e}")
        
        return config
    
    def _set_nested_attr(self, obj: Any, path: str, value: Any):
        """Set nested attribute using dot notation"""
        parts = path.split('.')
        current = obj
        
        for part in parts[:-1]:
            if not hasattr(current, part):
                raise AttributeError(f"{'.'.join(parts[:-1])} not found")
            current = getattr(current, part)
        
        setattr(current, parts[-1], value)
    
    async def _validate_config(self, config: BotConfig) -> None:
        """Validate configuration"""
        # Validate collections
        collection_addresses = set()
        for collection in config.collections:
            if collection.collection_address in collection_addresses:
                raise ValueError(f"Duplicate collection address: {collection.collection_address}")
            collection_addresses.add(collection.collection_address)
        
        # Validate exposure limits
        total_max_exposure = sum(
            config.execution.max_exposure_per_collection 
            for _ in config.collections
        )
        if total_max_exposure > config.execution.max_total_exposure * 2:
            log.warning(f"⚠️ Total collection exposure ({total_max_exposure}) significantly exceeds portfolio limit ({config.execution.max_total_exposure})")
        
        # Validate parasite hunter
        if config.parasite_hunter.enabled and not config.parasite_hunter.buyer_wallet:
            raise ValueError("buyer_wallet is required when parasite_hunter is enabled")
        
        log.info("✅ Configuration validation passed")
    
    async def start_watching(self) -> None:
        """Start watching for configuration changes"""
        if self._running:
            return
        
        self._running = True
        self._watchers.append(asyncio.create_task(self._watch_config_file()))
        
        log.info("👁️ Configuration watching started")
    
    async def stop_watching(self) -> None:
        """Stop watching for configuration changes"""
        self._running = False
        
        for watcher in self._watchers:
            watcher.cancel()
        
        await asyncio.gather(*self._watchers, return_exceptions=True)
        self._watchers.clear()
        
        log.info("🛑 Configuration watching stopped")
    
    async def _watch_config_file(self) -> None:
        """Watch configuration file for changes"""
        if not self.config_path.exists():
            return
        
        last_modified = self.config_path.stat().st_mtime
        
        while self._running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                if not self.config_path.exists():
                    continue
                
                current_modified = self.config_path.stat().st_mtime
                if current_modified > last_modified:
                    log.info("🔄 Configuration file changed, reloading...")
                    await self.load()
                    last_modified = current_modified
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ Error watching config file: {e}")
    
    def get_collection(self, address: str) -> Optional[CollectionConfig]:
        """Get collection configuration by address"""
        if not self._config:
            return None
        
        for collection in self._config.collections:
            if collection.collection_address == address:
                return collection
        
        return None
    
    def add_collection(self, collection: CollectionConfig) -> None:
        """Add collection configuration"""
        if not self._config:
            raise ValueError("Configuration not loaded")
        
        # Check for duplicates
        if self.get_collection(collection.collection_address):
            raise ValueError(f"Collection {collection.collection_address} already exists")
        
        self._config.collections.append(collection)
        self._config.updated_at = datetime.now()
    
    def remove_collection(self, address: str) -> bool:
        """Remove collection configuration"""
        if not self._config:
            return False
        
        for i, collection in enumerate(self._config.collections):
            if collection.collection_address == address:
                del self._config.collections[i]
                self._config.updated_at = datetime.now()
                return True
        
        return False
    
    def get_enabled_collections(self) -> List[CollectionConfig]:
        """Get enabled collections"""
        if not self._config:
            return []
        
        return [c for c in self._config.collections if c.enabled]
    
    async def create_template(self, template_type: str) -> BotConfig:
        """Create configuration template"""
        if template_type == "conservative":
            return BotConfig(
                execution=ExecutionConfig(
                    dry_run=True,
                    global_offers_per_hour=20,
                    max_total_exposure=Decimal("1000")
                ),
                decision=DecisionConfig(
                    confidence_threshold=0.8,
                    ev_threshold=Decimal("0.5")
                )
            )
        elif template_type == "aggressive":
            return BotConfig(
                execution=ExecutionConfig(
                    dry_run=False,
                    global_offers_per_hour=200,
                    max_total_exposure=Decimal("50000")
                ),
                decision=DecisionConfig(
                    confidence_threshold=0.5,
                    ev_threshold=Decimal("0.01")
                )
            )
        else:
            raise ValueError(f"Unknown template type: {template_type}")
    
    @property
    def config(self) -> BotConfig:
        """Get current configuration"""
        if not self._config:
            raise ValueError("Configuration not loaded")
        return self._config


# Global instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager(config_path: Optional[str] = None) -> ConfigManager:
    """Get global configuration manager instance"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager(config_path)
    return _config_manager


async def load_config(config_path: Optional[str] = None) -> BotConfig:
    """Load configuration using global manager"""
    manager = get_config_manager(config_path)
    return await manager.load()
