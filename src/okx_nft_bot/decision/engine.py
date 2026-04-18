"""
Decision Engine - Smart decision making with EV model and risk management

Features:
- Expected Value (EV) calculations
- Risk-adjusted decision making
- Multi-factor scoring
- Policy enforcement
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
import json
from datetime import datetime

from okx_nft_bot.sniper.parasite_hunter_v2 import CollectionMetrics, OfferDecision, Phase, TicketClass

log = logging.getLogger("decision.engine")


class DecisionType(Enum):
    PLACE_OFFER = "place_offer"
    CANCEL_OFFER = "cancel_offer"
    BUY_LISTING = "buy_listing"
    RELIST_INVENTORY = "relist_inventory"
    ADJUST_PRICE = "adjust_price"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class DecisionFactors:
    """Factors influencing decision making"""
    floor_price: Decimal
    second_floor_price: Decimal
    spread: Decimal
    depth_10: Decimal
    sales_velocity_24h: Decimal
    parasite_frequency: Decimal
    inventory_pressure: Decimal
    collection_age_hours: Decimal
    price_volatility: Decimal
    seller_quality: Decimal
    market_trend: Decimal
    liquidity_score: Decimal


@dataclass
class EVCalculation:
    """Expected Value calculation result"""
    expected_profit: Decimal
    fill_probability: Decimal
    expected_exit: Decimal
    holding_cost: Decimal
    cancel_cost: Decimal
    fees: Decimal
    total_ev: Decimal
    confidence_score: float
    risk_level: RiskLevel


@dataclass
class DecisionResult:
    """Result of decision engine"""
    decision_type: DecisionType
    should_act: bool
    confidence: float
    expected_pnl: Decimal
    risk_level: RiskLevel
    reasoning: str
    factors: DecisionFactors
    ev_calculation: Optional[EVCalculation] = None
    recommended_price: Optional[Decimal] = None
    max_exposure: Optional[Decimal] = None


class DecisionEngine:
    """Advanced decision making engine with EV model"""
    
    def __init__(self):
        self.decision_history: List[DecisionResult] = []
        self.factor_weights: Dict[str, Decimal] = self._initialize_weights()
        self.risk_limits: Dict[str, Decimal] = self._initialize_risk_limits()
        self.performance_stats: Dict[str, Any] = {}
        
    def _initialize_weights(self) -> Dict[str, Decimal]:
        """Initialize factor weights for EV calculation"""
        return {
            "floor_price": Decimal("0.15"),
            "spread": Decimal("0.20"),
            "depth_10": Decimal("0.10"),
            "sales_velocity": Decimal("0.15"),
            "parasite_frequency": Decimal("0.10"),
            "inventory_pressure": Decimal("0.10"),
            "price_volatility": Decimal("0.10"),
            "seller_quality": Decimal("0.10")
        }
    
    def _initialize_risk_limits(self) -> Dict[str, Decimal]:
        """Initialize risk limits"""
        return {
            "max_position_size": Decimal("1000"),  # Max 1000 USDT per position
            "max_portfolio_exposure": Decimal("10000"),  # Max 10k total exposure
            "max_drawdown": Decimal("0.20"),  # Max 20% drawdown
            "min_fill_rate": Decimal("0.30"),  # Min 30% fill rate
            "max_cancel_ratio": Decimal("0.50")  # Max 50% cancel ratio
        }
    
    async def evaluate_offers(self, collection: CollectionMetrics, offers: List[Any], phase: Phase) -> List[OfferDecision]:
        """Evaluate offers and return decisions"""
        decisions = []
        
        for offer in offers:
            try:
                decision = await self._evaluate_single_offer(collection, offer, phase)
                if decision:
                    decisions.append(decision)
            except Exception as e:
                log.error(f"Error evaluating offer: {e}")
        
        # Sort by expected PnL
        decisions.sort(key=lambda d: d.expected_pnl, reverse=True)
        
        return decisions
    
    async def evaluate_listings(self, collection: CollectionMetrics, listings: List[Any]) -> List[OfferDecision]:
        """Evaluate listings for missclick buy"""
        decisions = []
        
        for listing in listings:
            try:
                decision = await self._evaluate_listing(collection, listing)
                if decision and decision.expected_pnl > 0:
                    decisions.append(decision)
            except Exception as e:
                log.error(f"Error evaluating listing: {e}")
        
        return decisions
    
    async def _evaluate_single_offer(self, collection: CollectionMetrics, offer: Any, phase: Phase) -> Optional[OfferDecision]:
        """Evaluate a single offer"""
        # Extract offer data
        current_price = Decimal(str(offer.get("price", 0)))
        token_id = str(offer.get("token_id", ""))
        
        # Get decision factors
        factors = await self._calculate_factors(collection, offer)
        
        # Calculate EV
        ev_calc = await self._calculate_ev(collection, current_price, factors, phase)
        
        # Apply risk filters
        if not await self._pass_risk_filters(collection, ev_calc, factors):
            return None
        
        # Generate decision
        our_price = await self._calculate_our_price(current_price, ev_calc, factors, phase)
        expected_pnl = ev_calc.expected_profit
        
        return OfferDecision(
            collection=collection.address,
            token_id=token_id,
            current_price=current_price,
            our_price=our_price,
            expected_pnl=expected_pnl,
            confidence=ev_calc.confidence_score,
            strategy=self._determine_strategy(collection, phase),
            phase=phase
        )
    
    async def _evaluate_listing(self, collection: CollectionMetrics, listing: Any) -> Optional[OfferDecision]:
        """Evaluate a listing for purchase"""
        price = Decimal(str(listing.get("price", 0)))
        token_id = str(listing.get("token_id", ""))
        
        # Check if price is below threshold
        max_buy_price = await self._get_max_buy_price(collection)
        if price > max_buy_price:
            return None
        
        # Calculate expected PnL
        expected_exit = await self._estimate_exit_price(collection)
        fees = price * Decimal("0.02")  # 2% fees
        expected_pnl = expected_exit - price - fees
        
        if expected_pnl <= 0:
            return None
        
        return OfferDecision(
            collection=collection.address,
            token_id=token_id,
            current_price=price,
            our_price=price,  # Buy at listing price
            expected_pnl=expected_pnl,
            confidence=0.8,  # High confidence for listings
            strategy="missclick_buy",
            phase=Phase.MISSCLICK_BUY
        )
    
    async def _calculate_factors(self, collection: CollectionMetrics, offer: Any) -> DecisionFactors:
        """Calculate decision factors"""
        # Get market data
        floor_price = collection.floor_price
        second_floor_price = await self._get_second_floor_price(collection.address)
        spread = (second_floor_price - floor_price) / floor_price if floor_price > 0 else Decimal("0")
        
        depth_10 = await self._get_depth_10(collection.address)
        sales_velocity = collection.volume_24h
        parasite_frequency = await self._get_parasite_frequency(collection.address)
        inventory_pressure = await self._get_inventory_pressure(collection.address)
        collection_age = await self._get_collection_age(collection.address)
        price_volatility = await self._get_price_volatility(collection.address)
        seller_quality = await self._get_seller_quality(offer)
        market_trend = await self._get_market_trend()
        liquidity_score = await self._get_liquidity_score(collection.address)
        
        return DecisionFactors(
            floor_price=floor_price,
            second_floor_price=second_floor_price,
            spread=spread,
            depth_10=depth_10,
            sales_velocity_24h=sales_velocity,
            parasite_frequency=parasite_frequency,
            inventory_pressure=inventory_pressure,
            collection_age_hours=collection_age,
            price_volatility=price_volatility,
            seller_quality=seller_quality,
            market_trend=market_trend,
            liquidity_score=liquidity_score
        )
    
    async def _calculate_ev(self, collection: CollectionMetrics, price: Decimal, factors: DecisionFactors, phase: Phase) -> EVCalculation:
        """Calculate Expected Value"""
        # Fill probability based on factors
        fill_probability = await self._estimate_fill_probability(price, factors, phase)
        
        # Expected exit price
        expected_exit = await self._estimate_exit_price(collection)
        
        # Costs
        holding_cost = await self._calculate_holding_cost(collection, price)
        cancel_cost = price * Decimal("0.01")  # 1% cancel cost
        fees = price * Decimal("0.02")  # 2% transaction fees
        
        # Expected profit
        expected_profit = (expected_exit - price) * fill_probability - holding_cost - cancel_cost - fees
        
        # Total EV
        total_ev = expected_profit * fill_probability - (holding_cost + cancel_cost + fees) * (1 - fill_probability)
        
        # Confidence score
        confidence_score = await self._calculate_confidence_score(factors, fill_probability, total_ev)
        
        # Risk level
        risk_level = await self._assess_risk_level(factors, total_ev, fill_probability)
        
        return EVCalculation(
            expected_profit=expected_profit,
            fill_probability=fill_probability,
            expected_exit=expected_exit,
            holding_cost=holding_cost,
            cancel_cost=cancel_cost,
            fees=fees,
            total_ev=total_ev,
            confidence_score=confidence_score,
            risk_level=risk_level
        )
    
    async def _estimate_fill_probability(self, price: Decimal, factors: DecisionFactors, phase: Phase) -> Decimal:
        """Estimate probability of fill"""
        base_prob = Decimal("0.5")
        
        # Adjust based on factors
        if price < factors.floor_price * Decimal("0.95"):  # Below floor
            base_prob += Decimal("0.3")
        
        if factors.spread > Decimal("0.1"):  # High spread
            base_prob += Decimal("0.2")
        
        if factors.sales_velocity_24h > Decimal("100"):  # High volume
            base_prob += Decimal("0.1")
        
        if factors.parasite_frequency > Decimal("0.5"):  # High parasite activity
            base_prob += Decimal("0.1")
        
        if factors.liquidity_score > Decimal("0.7"):  # High liquidity
            base_prob += Decimal("0.1")
        
        # Phase adjustments
        if phase == Phase.WL_CAPTURE:
            base_prob += Decimal("0.2")
        elif phase == Phase.PARASITE_HUNT:
            base_prob -= Decimal("0.1")
        
        # Cap at reasonable bounds
        return max(Decimal("0.1"), min(Decimal("0.9"), base_prob))
    
    async def _estimate_exit_price(self, collection: CollectionMetrics) -> Decimal:
        """Estimate exit price for PnL calculation"""
        # Use floor price as base, adjust for strategy
        base_exit = collection.floor_price
        
        # Strategy multipliers
        if collection.ticket_class == TicketClass.LOW:
            return base_exit * Decimal("3")  # 3x for low ticket
        elif collection.ticket_class == TicketClass.MID:
            return base_exit * Decimal("1.5")  # 1.5x for mid ticket
        else:  # HIGH
            return base_exit * Decimal("1.2")  # 1.2x for high ticket
    
    async def _calculate_holding_cost(self, collection: CollectionMetrics, price: Decimal) -> Decimal:
        """Calculate holding cost"""
        # Base holding cost: 0.1% per day
        daily_cost = price * Decimal("0.001")
        
        # Adjust by collection age and volatility
        age_multiplier = min(collection.ticket_class.value if hasattr(collection.ticket_class, 'value') else 1, Decimal("2"))
        volatility_multiplier = min(Decimal("1") + collection.pnl_metrics.get("volatility", Decimal("0")), Decimal("3"))
        
        return daily_cost * age_multiplier * volatility_multiplier
    
    async def _calculate_confidence_score(self, factors: DecisionFactors, fill_prob: Decimal, ev: Decimal) -> float:
        """Calculate confidence score"""
        confidence = 0.5  # Base confidence
        
        # Factor contributions
        if factors.liquidity_score > Decimal("0.7"):
            confidence += 0.2
        if factors.sales_velocity_24h > Decimal("100"):
            confidence += 0.1
        if fill_prob > Decimal("0.7"):
            confidence += 0.1
        if ev > Decimal("0"):
            confidence += 0.1
        
        return min(0.95, confidence)  # Cap at 95%
    
    async def _assess_risk_level(self, factors: DecisionFactors, ev: Decimal, fill_prob: Decimal) -> RiskLevel:
        """Assess risk level"""
        risk_score = 0
        
        # High risk factors
        if factors.price_volatility > Decimal("0.3"):
            risk_score += 1
        if factors.liquidity_score < Decimal("0.3"):
            risk_score += 1
        if fill_prob < Decimal("0.3"):
            risk_score += 1
        if ev < Decimal("0"):
            risk_score += 2
        
        # Determine risk level
        if risk_score >= 3:
            return RiskLevel.EXTREME
        elif risk_score >= 2:
            return RiskLevel.HIGH
        elif risk_score >= 1:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW
    
    async def _calculate_our_price(self, current_price: Decimal, ev: EVCalculation, factors: DecisionFactors, phase: Phase) -> Decimal:
        """Calculate our offer price"""
        # Base undercut amount
        if phase == Phase.WL_CAPTURE:
            undercut_bps = 50  # 0.5%
        elif phase == Phase.PARASITE_HUNT:
            undercut_bps = 100  # 1%
        else:
            undercut_bps = 200  # 2%
        
        undercut_amount = current_price * Decimal(undercut_bps) / Decimal("10000")
        our_price = current_price - undercut_amount
        
        # Adjust based on EV and confidence
        if ev.confidence_score > 0.8 and ev.expected_profit > Decimal("10"):
            our_price += undercut_amount * Decimal("0.5")  # Less aggressive
        
        return our_price
    
    async def _pass_risk_filters(self, collection: CollectionMetrics, ev: EVCalculation, factors: DecisionFactors) -> bool:
        """Check if decision passes risk filters"""
        # EV filter
        if ev.total_ev <= 0:
            return False
        
        # Confidence filter
        if ev.confidence_score < 0.6:
            return False
        
        # Risk level filter
        if ev.risk_level in [RiskLevel.EXTREME]:
            return False
        
        # Exposure filter
        current_exposure = await self._get_current_exposure(collection.address)
        if current_exposure > self.risk_limits["max_position_size"]:
            return False
        
        return True
    
    def _determine_strategy(self, collection: CollectionMetrics, phase: Phase) -> str:
        """Determine strategy name"""
        base_strategy = f"{collection.ticket_class.value}_{phase.value}"
        return base_strategy
    
    # Placeholder methods for data fetching
    async def _get_second_floor_price(self, address: str) -> Decimal:
        return Decimal("1.1")
    
    async def _get_depth_10(self, address: str) -> Decimal:
        return Decimal("10")
    
    async def _get_parasite_frequency(self, address: str) -> Decimal:
        return Decimal("0.3")
    
    async def _get_inventory_pressure(self, address: str) -> Decimal:
        return Decimal("0.2")
    
    async def _get_collection_age(self, address: str) -> Decimal:
        return Decimal("720")  # 30 days
    
    async def _get_price_volatility(self, address: str) -> Decimal:
        return Decimal("0.2")
    
    async def _get_seller_quality(self, offer: Any) -> Decimal:
        return Decimal("0.7")
    
    async def _get_market_trend(self) -> Decimal:
        return Decimal("0.1")
    
    async def _get_liquidity_score(self, address: str) -> Decimal:
        return Decimal("0.6")
    
    async def _get_max_buy_price(self, collection: CollectionMetrics) -> Decimal:
        if collection.ticket_class == TicketClass.LOW:
            return Decimal("0.5")
        elif collection.ticket_class == TicketClass.MID:
            return Decimal("5")
        else:
            return Decimal("50")
    
    async def _get_current_exposure(self, address: str) -> Decimal:
        return Decimal("100")  # Placeholder


# Factory function
def create_decision_engine() -> DecisionEngine:
    """Create decision engine instance"""
    return DecisionEngine()
