"""
PnL Engine - Realized and Unrealized Profit/Loss Tracking

Features:
- Real-time PnL calculation
- Collection-level attribution
- Performance metrics
- Risk exposure tracking
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import json

log = logging.getLogger("pnl.engine")

warnings.warn(
    "okx_nft_bot.pnl.engine is a deprecated stub (hardcoded placeholder prices "
    "and in-memory state). Do not use in production. See AUDIT_2026_04_19.md A2.",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass
class Trade:
    trade_id: str
    collection_address: str
    token_id: str
    buy_price: Decimal
    buy_currency: str
    sell_price: Optional[Decimal] = None
    sell_currency: Optional[str] = None
    buy_time: datetime = field(default_factory=datetime.now)
    sell_time: Optional[datetime] = None
    fees: Decimal = Decimal("0")
    pnl: Optional[Decimal] = None
    holding_period_hours: Optional[int] = None


@dataclass
class CollectionPnL:
    collection_address: str
    collection_name: str
    
    # PnL metrics
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    
    # Performance metrics
    total_trades: int = 0
    successful_trades: int = 0
    fill_rate: Decimal = Decimal("0")
    
    # Timing metrics
    avg_holding_hours: Decimal = Decimal("0")
    median_time_to_fill: Optional[int] = None
    
    # Exposure metrics
    current_exposure: Decimal = Decimal("0")
    max_exposure: Decimal = Decimal("0")
    exposure_utilization: Decimal = Decimal("0")
    
    # Risk metrics
    pnl_per_offer: Decimal = Decimal("0")
    pnl_per_usdt_exposure: Decimal = Decimal("0")
    cancel_ratio: Decimal = Decimal("0")
    
    last_updated: datetime = field(default_factory=datetime.now)


@dataclass
class PortfolioMetrics:
    total_realized_pnl: Decimal = Decimal("0")
    total_unrealized_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    total_exposure: Decimal = Decimal("0")
    active_positions: int = 0
    portfolio_age_hours: Decimal = Decimal("0")
    
    # Performance ratios
    sharpe_ratio: Optional[Decimal] = None
    max_drawdown: Decimal = Decimal("0")
    win_rate: Decimal = Decimal("0")
    
    last_updated: datetime = field(default_factory=datetime.now)


class PnLEngine:
    """Advanced PnL tracking and analysis engine.

    DEPRECATED: This is a stub with hardcoded placeholder values.
    - Returns Decimal("345") as BNB price (line 178)
    - Returns Decimal("1") as current price (line 292)
    - Returns Decimal("10000") as max exposure (line 221)
    - Returns Decimal("0.1") as cancel_ratio (line 228)
    - State is in-memory only (lost on restart)

    DO NOT USE IN PRODUCTION. See AUDIT_2026_04_19.md A2.
    """

    def __init__(self):
        self.trades: Dict[str, Trade] = {}
        self.collection_pnl: Dict[str, CollectionPnL] = {}
        self.portfolio_metrics = PortfolioMetrics()
        self.price_cache: Dict[str, Decimal] = {}
        
        # Performance tracking
        self.pnl_history: List[Decimal] = []
        self.exposure_history: List[Decimal] = []
        
    async def record_trade(self, trade: Trade) -> None:
        """Record a new trade"""
        self.trades[trade.trade_id] = trade
        
        # Update collection PnL
        await self._update_collection_pnl(trade.collection_address)
        
        # Update portfolio metrics
        await self._update_portfolio_metrics()
        
        log.info(f"📊 Recorded trade: {trade.trade_id} - PnL: {trade.pnl or 'Pending'}")
    
    async def record_sale(self, trade_id: str, sell_price: Decimal, sell_currency: str, fees: Decimal = Decimal("0")) -> None:
        """Record a sale and calculate PnL"""
        if trade_id not in self.trades:
            log.error(f"Trade {trade_id} not found")
            return
            
        trade = self.trades[trade_id]
        trade.sell_price = sell_price
        trade.sell_currency = sell_currency
        trade.sell_time = datetime.now()
        trade.fees = fees
        
        # Calculate PnL
        trade.pnl = await self._calculate_trade_pnl(trade)
        trade.holding_period_hours = int((trade.sell_time - trade.buy_time).total_seconds() / 3600)
        
        # Update metrics
        await self._update_collection_pnl(trade.collection_address)
        await self._update_portfolio_metrics()
        
        log.info(f"💰 Sale recorded: {trade_id} - PnL: {trade.pnl} USDT")
    
    async def _calculate_trade_pnl(self, trade: Trade) -> Decimal:
        """Calculate PnL for a trade"""
        if not trade.sell_price:
            return Decimal("0")
            
        # Convert to USDT for consistent PnL calculation
        buy_usdt = await self._convert_to_usdt(trade.buy_price, trade.buy_currency)
        sell_usdt = await self._convert_to_usdt(trade.sell_price, trade.sell_currency)
        
        pnl = sell_usdt - buy_usdt - trade.fees
        return pnl
    
    async def _convert_to_usdt(self, amount: Decimal, currency: str) -> Decimal:
        """Convert amount to USDT"""
        if currency == "USDT":
            return amount
        elif currency == "BNB":
            # Use cached price or fetch from API
            bnb_price = await self._get_price("BNB")
            return amount * bnb_price
        elif currency == "WBNB":
            wbnb_price = await self._get_price("WBNB")
            return amount * wbnb_price
        else:
            log.warning(f"Unknown currency: {currency}")
            return amount
    
    async def _get_price(self, symbol: str) -> Decimal:
        """Get price for symbol (with caching)"""
        if symbol in self.price_cache:
            return self.price_cache[symbol]
            
        # Fetch from price API (placeholder)
        price = await self._fetch_price_from_api(symbol)
        self.price_cache[symbol] = price
        
        return price
    
    async def _fetch_price_from_api(self, symbol: str) -> Decimal:
        """Fetch price from API (placeholder implementation)"""
        # Implementation needed - fetch from price API
        if symbol == "BNB" or symbol == "WBNB":
            return Decimal("345")  # Placeholder price
        return Decimal("1")
    
    async def _update_collection_pnl(self, collection_address: str) -> None:
        """Update PnL metrics for a collection"""
        collection_trades = [t for t in self.trades.values() if t.collection_address == collection_address]
        
        if not collection_trades:
            return
            
        pnl = CollectionPnL(
            collection_address=collection_address,
            collection_name=await self._get_collection_name(collection_address)
        )
        
        # Calculate realized PnL
        realized_trades = [t for t in collection_trades if t.pnl is not None]
        pnl.realized_pnl = sum(t.pnl for t in realized_trades)
        
        # Calculate unrealized PnL
        unrealized_trades = [t for t in collection_trades if t.pnl is None]
        for trade in unrealized_trades:
            current_price = await self._get_current_price(collection_address)
            current_usdt = await self._convert_to_usdt(current_price, "USDT")
            buy_usdt = await self._convert_to_usdt(trade.buy_price, trade.buy_currency)
            pnl.unrealized_pnl += current_usdt - buy_usdt
        
        pnl.total_pnl = pnl.realized_pnl + pnl.unrealized_pnl
        
        # Performance metrics
        pnl.total_trades = len(collection_trades)
        pnl.successful_trades = len(realized_trades)
        pnl.fill_rate = Decimal(pnl.successful_trades) / Decimal(pnl.total_trades) if pnl.total_trades > 0 else Decimal("0")
        
        # Timing metrics
        holding_periods = [t.holding_period_hours for t in realized_trades if t.holding_period_hours]
        if holding_periods:
            pnl.avg_holding_hours = sum(Decimal(h) for h in holding_periods) / len(holding_periods)
            pnl.median_time_to_fill = sorted(holding_periods)[len(holding_periods) // 2]
        
        # Exposure metrics
        pnl.current_exposure = sum(trade.buy_price for trade in unrealized_trades)
        pnl.max_exposure = pnl.current_exposure  # Simplified
        pnl.exposure_utilization = pnl.current_exposure / Decimal("10000") if pnl.current_exposure > 0 else Decimal("0")  # Assuming 10k max exposure
        
        # Risk metrics
        pnl.pnl_per_offer = pnl.realized_pnl / pnl.successful_trades if pnl.successful_trades > 0 else Decimal("0")
        pnl.pnl_per_usdt_exposure = pnl.total_pnl / pnl.current_exposure if pnl.current_exposure > 0 else Decimal("0")
        
        # Cancel ratio (placeholder)
        pnl.cancel_ratio = Decimal("0.1")  # Simplified
        
        self.collection_pnl[collection_address] = pnl
    
    async def _update_portfolio_metrics(self) -> None:
        """Update portfolio-level metrics"""
        if not self.collection_pnl:
            return
            
        metrics = PortfolioMetrics()
        
        # Aggregate PnL
        metrics.total_realized_pnl = sum(pnl.realized_pnl for pnl in self.collection_pnl.values())
        metrics.total_unrealized_pnl = sum(pnl.unrealized_pnl for pnl in self.collection_pnl.values())
        metrics.total_pnl = metrics.total_realized_pnl + metrics.total_unrealized_pnl
        
        # Aggregate exposure
        metrics.total_exposure = sum(pnl.current_exposure for pnl in self.collection_pnl.values())
        metrics.active_positions = sum(1 for pnl in self.collection_pnl.values() if pnl.current_exposure > 0)
        
        # Portfolio age
        if self.trades:
            oldest_trade = min(self.trades.values(), key=lambda t: t.buy_time)
            metrics.portfolio_age_hours = Decimal((datetime.now() - oldest_trade.buy_time).total_seconds() / 3600)
        
        # Performance ratios
        total_trades = sum(pnl.total_trades for pnl in self.collection_pnl.values())
        successful_trades = sum(pnl.successful_trades for pnl in self.collection_pnl.values())
        metrics.win_rate = Decimal(successful_trades) / Decimal(total_trades) if total_trades > 0 else Decimal("0")
        
        # Sharpe ratio (simplified)
        if len(self.pnl_history) > 1:
            returns = [float(p) for p in self.pnl_history[-30:]]  # Last 30 periods
            if len(returns) > 1:
                avg_return = sum(returns) / len(returns)
                variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
                metrics.sharpe_ratio = Decimal(str(avg_return / (variance ** 0.5) if variance > 0 else 0))
        
        # Max drawdown
        if len(self.pnl_history) > 1:
            peak = max(self.pnl_history)
            current = self.pnl_history[-1]
            drawdown = (peak - current) / peak if peak > 0 else Decimal("0")
            metrics.max_drawdown = max(metrics.max_drawdown, drawdown)
        
        self.portfolio_metrics = metrics
        
        # Update history
        self.pnl_history.append(metrics.total_pnl)
        self.exposure_history.append(metrics.total_exposure)
        
        # Keep history limited
        if len(self.pnl_history) > 1000:
            self.pnl_history = self.pnl_history[-1000:]
        if len(self.exposure_history) > 1000:
            self.exposure_history = self.exposure_history[-1000:]
    
    async def _get_collection_name(self, address: str) -> str:
        """Get collection name from address"""
        # Implementation needed - lookup from registry or API
        return f"Collection_{address[:8]}"
    
    async def _get_current_price(self, collection_address: str) -> Decimal:
        """Get current floor price for collection"""
        # Implementation needed - fetch from API
        return Decimal("1")  # Placeholder
    
    def get_top_performers(self, limit: int = 10) -> List[CollectionPnL]:
        """Get top performing collections by PnL"""
        sorted_pnl = sorted(self.collection_pnl.values(), key=lambda x: x.total_pnl, reverse=True)
        return sorted_pnl[:limit]
    
    def get_worst_performers(self, limit: int = 10) -> List[CollectionPnL]:
        """Get worst performing collections by PnL"""
        sorted_pnl = sorted(self.collection_pnl.values(), key=lambda x: x.total_pnl)
        return sorted_pnl[:limit]
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """Get risk summary"""
        return {
            "total_exposure": float(self.portfolio_metrics.total_exposure),
            "max_drawdown": float(self.portfolio_metrics.max_drawdown),
            "sharpe_ratio": float(self.portfolio_metrics.sharpe_ratio) if self.portfolio_metrics.sharpe_ratio else None,
            "win_rate": float(self.portfolio_metrics.win_rate),
            "active_positions": self.portfolio_metrics.active_positions,
            "portfolio_age_hours": float(self.portfolio_metrics.portfolio_age_hours)
        }
    
    def export_metrics(self) -> Dict[str, Any]:
        """Export all metrics for dashboard"""
        return {
            "portfolio": {
                "total_pnl": float(self.portfolio_metrics.total_pnl),
                "realized_pnl": float(self.portfolio_metrics.total_realized_pnl),
                "unrealized_pnl": float(self.portfolio_metrics.total_unrealized_pnl),
                "total_exposure": float(self.portfolio_metrics.total_exposure),
                "active_positions": self.portfolio_metrics.active_positions,
                "win_rate": float(self.portfolio_metrics.win_rate),
                "sharpe_ratio": float(self.portfolio_metrics.sharpe_ratio) if self.portfolio_metrics.sharpe_ratio else None
            },
            "collections": {
                addr: {
                    "name": pnl.collection_name,
                    "total_pnl": float(pnl.total_pnl),
                    "realized_pnl": float(pnl.realized_pnl),
                    "unrealized_pnl": float(pnl.unrealized_pnl),
                    "fill_rate": float(pnl.fill_rate),
                    "avg_holding_hours": float(pnl.avg_holding_hours),
                    "current_exposure": float(pnl.current_exposure)
                }
                for addr, pnl in self.collection_pnl.items()
            },
            "risk": self.get_risk_summary()
        }


# Factory function
def create_pnl_engine() -> PnLEngine:
    """Create PnL engine instance"""
    return PnLEngine()
