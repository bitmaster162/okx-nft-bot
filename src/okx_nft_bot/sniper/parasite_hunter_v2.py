"""
Parasite Hunter v5 - Refactored with PnL Engine and Decision Layer

Optimizations:
- Separated concerns: PnL tracking, decision making, execution
- Async/await for non-blocking operations
- Type hints and dataclasses for better maintainability
- Circuit breaker pattern for API resilience
- Metrics collection and performance monitoring
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from enum import Enum
import time
from decimal import Decimal

from okx_nft_bot.currency import canonical_currency_symbol
from okx_nft_bot.pnl.engine import PnLEngine
from okx_nft_bot.decision.engine import DecisionEngine
from okx_nft_bot.execution.governor import ExecutionGovernor

log = logging.getLogger("sniper.parasite_hunter_v2")


class TicketClass(Enum):
    LOW = "low_ticket"      # < 2 USDT
    MID = "mid_ticket"      # 2-50 USDT  
    HIGH = "high_ticket"    # > 50 USDT


class Phase(Enum):
    WL_CAPTURE = "wl_capture"
    PARASITE_HUNT = "parasite_hunt"
    MISSCLICK_BUY = "missclick_buy"


@dataclass
class CollectionMetrics:
    address: str
    name: str
    ticket_class: TicketClass
    floor_price: Decimal
    volume_24h: Decimal
    pnl_metrics: Dict[str, Decimal]
    fill_rate: Decimal
    toxic_score: Decimal


@dataclass
class OfferDecision:
    collection: str
    token_id: str
    current_price: Decimal
    our_price: Decimal
    expected_pnl: Decimal
    confidence: float
    strategy: str
    phase: Phase


@dataclass
class ParasiteHunterConfig:
    enabled: bool = True
    dry_run: bool = True
    max_per_collection: int = 50
    undercut_bps: int = 50
    max_usd: Decimal = Decimal("0.51")
    delay_seconds: float = 1.0
    scan_interval: int = 300
    collection_delay: float = 2.0
    chains: List[str] = field(default_factory=lambda: ["bsc", "eth"])
    offer_currencies: List[str] = field(default_factory=lambda: ["WBNB", "USDT"])
    nonwl_max_usd: Decimal = Decimal("0.01")
    nonwl_qty: int = 10
    friend_wallets: Set[str] = field(default_factory=set)
    buyer_wallet: str = ""
    slug_map: Dict[str, str] = field(default_factory=dict)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    async def call(self, func, *args, **kwargs):
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "HALF_OPEN"
            else:
                raise Exception("Circuit breaker is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.failure_count = 0
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
            raise e


class ParasiteHunterV2:
    """Refactored Parasite Hunter with PnL Engine and Decision Layer"""
    
    def __init__(self, config: ParasiteHunterConfig):
        self.config = config
        self.pnl_engine = PnLEngine()
        self.decision_engine = DecisionEngine()
        self.execution_governor = ExecutionGovernor()
        self.circuit_breaker = CircuitBreaker()
        
        self.collection_metrics: Dict[str, CollectionMetrics] = {}
        self.active_offers: Dict[str, OfferDecision] = {}
        self.performance_metrics: Dict[str, Any] = {}
        
    async def run_scan_cycle(self) -> Dict[str, Any]:
        """Main scan cycle with all three phases"""
        start_time = time.time()
        results = {
            "phase1_wl": {"scanned": 0, "offers_placed": 0, "errors": 0},
            "phase2_parasite": {"scanned": 0, "offers_placed": 0, "errors": 0},
            "phase3_missclick": {"scanned": 0, "purchases": 0, "errors": 0},
            "total_time": 0
        }
        
        try:
            # Phase 1: WL Capture
            await self._execute_phase_wl_capture(results["phase1_wl"])
            
            # Phase 2: Parasite Hunt  
            await self._execute_phase_parasite_hunt(results["phase2_parasite"])
            
            # Phase 3: Missclick Buy
            await self._execute_phase_missclick_buy(results["phase3_missclick"])
            
        except Exception as e:
            log.error(f"Scan cycle failed: {e}")
            
        results["total_time"] = time.time() - start_time
        self._update_performance_metrics(results)
        
        return results
    
    async def _execute_phase_wl_capture(self, phase_results: Dict[str, Any]):
        """Phase 1: Capture WL collections"""
        log.info("🎯 PHASE 1: WL CAPTURE on Binance collections")
        
        for collection in await self._get_wl_collections():
            try:
                phase_results["scanned"] += 1
                offers = await self.circuit_breaker.call(
                    self._get_collection_offers, collection.address
                )
                
                decisions = await self.decision_engine.evaluate_offers(
                    collection, offers, Phase.WL_CAPTURE
                )
                
                for decision in decisions:
                    if await self._should_place_offer(decision):
                        await self._place_offer(decision)
                        phase_results["offers_placed"] += 1
                        
                await asyncio.sleep(self.config.collection_delay)
                
            except Exception as e:
                phase_results["errors"] += 1
                log.error(f"Error in WL capture for {collection.address}: {e}")
    
    async def _execute_phase_parasite_hunt(self, phase_results: Dict[str, Any]):
        """Phase 2: Hunt parasite offers on non-WL collections"""
        log.info("🦠 PHASE 2: PARASITE NON-WL HUNT")
        
        for wallet in await self._get_parasite_wallets():
            try:
                offers = await self.circuit_breaker.call(
                    self._get_wallet_offers, wallet
                )
                
                # Group by collection
                collection_offers = self._group_offers_by_collection(offers)
                
                for collection_addr, coll_offers in collection_offers.items():
                    if len(coll_offers) > self.config.nonwl_qty:
                        continue
                        
                    collection = await self._get_collection_metrics(collection_addr)
                    if not collection:
                        continue
                        
                    decisions = await self.decision_engine.evaluate_offers(
                        collection, coll_offers, Phase.PARASITE_HUNT
                    )
                    
                    for decision in decisions:
                        if await self._should_place_offer(decision):
                            await self._place_offer(decision)
                            phase_results["offers_placed"] += 1
                            
                phase_results["scanned"] += len(collection_offers)
                
            except Exception as e:
                phase_results["errors"] += 1
                log.error(f"Error in parasite hunt for wallet {wallet}: {e}")
    
    async def _execute_phase_missclick_buy(self, phase_results: Dict[str, Any]):
        """Phase 3: Buy missclick listings on WL"""
        log.info("💰 PHASE 3: MISSCLICK BUY")
        
        for collection in await self._get_wl_collections():
            try:
                phase_results["scanned"] += 1
                listings = await self.circuit_breaker.call(
                    self._get_collection_listings, collection.address
                )
                
                decisions = await self.decision_engine.evaluate_listings(
                    collection, listings
                )
                
                for decision in decisions:
                    if await self._should_buy_listing(decision):
                        await self._execute_purchase(decision)
                        phase_results["purchases"] += 1
                        
                await asyncio.sleep(self.config.collection_delay)
                
            except Exception as e:
                phase_results["errors"] += 1
                log.error(f"Error in missclick buy for {collection.address}: {e}")
    
    async def _should_place_offer(self, decision: OfferDecision) -> bool:
        """Decision logic with PnL analysis"""
        if not self.config.enabled or self.config.dry_run:
            return False
            
        # PnL check
        if decision.expected_pnl < 0:
            log.debug(f"Negative PnL for {decision.token_id}: {decision.expected_pnl}")
            return False
            
        # Confidence check
        if decision.confidence < 0.7:
            log.debug(f"Low confidence for {decision.token_id}: {decision.confidence}")
            return False
            
        # Rate limiting
        if not await self.execution_governor.can_place_offer():
            log.debug("Rate limited - cannot place offer")
            return False
            
        return True
    
    async def _place_offer(self, decision: OfferDecision):
        """Execute offer placement with tracking"""
        try:
            result = await self.execution_governor.place_offer(
                collection=decision.collection,
                token_id=decision.token_id,
                price=decision.our_price,
                currency="USDT"
            )
            
            # Track PnL
            await self.pnl_engine.track_offer_placement(decision, result)
            
            # Update active offers
            self.active_offers[f"{decision.collection}:{decision.token_id}"] = decision
            
            log.info(f"✅ Placed offer: {decision.token_id} @ {decision.our_price}")
            
        except Exception as e:
            log.error(f"Failed to place offer: {e}")
            await self.pnl_engine.track_offer_error(decision, str(e))
    
    def _update_performance_metrics(self, results: Dict[str, Any]):
        """Update performance metrics for monitoring"""
        self.performance_metrics = {
            "last_scan_time": time.time(),
            "scan_duration": results["total_time"],
            "offers_placed_total": (
                results["phase1_wl"]["offers_placed"] + 
                results["phase2_parasite"]["offers_placed"]
            ),
            "purchases_total": results["phase3_missclick"]["purchases"],
            "errors_total": (
                results["phase1_wl"]["errors"] + 
                results["phase2_parasite"]["errors"] + 
                results["phase3_missclick"]["errors"]
            ),
            "active_offers_count": len(self.active_offers)
        }
        
        # Log performance
        log.info(f"📊 Scan completed in {results['total_time']:.2f}s")
        log.info(f"📈 Offers placed: {self.performance_metrics['offers_placed_total']}")
        log.info(f"💰 Purchases: {self.performance_metrics['purchases_total']}")
        log.info(f"⚠️ Errors: {self.performance_metrics['errors_total']}")
    
    # Placeholder methods for API calls
    async def _get_wl_collections(self) -> List[CollectionMetrics]:
        """Get Binance whitelist collections"""
        # Implementation needed
        return []
    
    async def _get_parasite_wallets(self) -> List[str]:
        """Get parasite wallet addresses"""
        # Implementation needed  
        return []
    
    async def _get_collection_offers(self, address: str) -> List[Any]:
        """Get offers for collection"""
        # Implementation needed
        return []
    
    async def _get_wallet_offers(self, wallet: str) -> List[Any]:
        """Get offers from specific wallet"""
        # Implementation needed
        return []
    
    async def _get_collection_listings(self, address: str) -> List[Any]:
        """Get listings for collection"""
        # Implementation needed
        return []
    
    async def _get_collection_metrics(self, address: str) -> Optional[CollectionMetrics]:
        """Get collection metrics"""
        # Implementation needed
        return None
    
    def _group_offers_by_collection(self, offers: List[Any]) -> Dict[str, List[Any]]:
        """Group offers by collection address"""
        grouped = {}
        for offer in offers:
            collection = offer.get("collection_address")
            if collection not in grouped:
                grouped[collection] = []
            grouped[collection].append(offer)
        return grouped
    
    async def _should_buy_listing(self, decision) -> bool:
        """Check if should buy listing"""
        # Implementation needed
        return False
    
    async def _execute_purchase(self, decision):
        """Execute purchase"""
        # Implementation needed
        pass


# Factory function
def create_parasite_hunter_v2(config: ParasiteHunterConfig) -> ParasiteHunterV2:
    """Create optimized Parasite Hunter v2 instance"""
    return ParasiteHunterV2(config)
