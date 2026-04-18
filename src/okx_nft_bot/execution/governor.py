"""
Execution Governor - Rate limiting, safety controls, and execution management

Features:
- Rate limiting per collection and globally
- Safety checks and circuit breakers
- Execution queue management
- Performance monitoring
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Any, Set
from enum import Enum
from datetime import datetime, timedelta
import time
import json

log = logging.getLogger("execution.governor")


class ExecutionStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SafetyLevel(Enum):
    SAFE = "safe"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass
class ExecutionRequest:
    request_id: str
    collection_address: str
    token_id: str
    action: str  # place_offer, cancel_offer, buy_listing
    price: Decimal
    currency: str
    priority: int = 1
    created_at: datetime = field(default_factory=datetime.now)
    scheduled_at: Optional[datetime] = None
    timeout_seconds: int = 30
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class ExecutionResult:
    request_id: str
    status: ExecutionStatus
    success: bool
    error_message: Optional[str] = None
    execution_time: Optional[float] = None
    transaction_hash: Optional[str] = None
    gas_used: Optional[int] = None
    completed_at: datetime = field(default_factory=datetime.now)


@dataclass
class RateLimitConfig:
    global_offers_per_hour: int = 100
    global_offers_per_minute: int = 10
    collection_offers_per_hour: int = 20
    collection_offers_per_minute: int = 5
    wallet_offers_per_hour: int = 50
    cooldown_seconds: int = 30


@dataclass
class SafetyConfig:
    max_exposure_per_collection: Decimal = Decimal("1000")
    max_total_exposure: Decimal = Decimal("10000")
    max_price_deviation: Decimal = Decimal("0.5")  # 50% deviation
    min_confidence_score: float = 0.6
    emergency_stop_enabled: bool = True
    auto_circuit_breaker: bool = True


class ExecutionGovernor:
    """Advanced execution management with safety controls"""
    
    def __init__(self, rate_limit_config: Optional[RateLimitConfig] = None, safety_config: Optional[SafetyConfig] = None):
        self.rate_limit_config = rate_limit_config or RateLimitConfig()
        self.safety_config = safety_config or SafetyConfig()
        
        # Execution state
        self.execution_queue: asyncio.Queue = asyncio.Queue()
        self.active_executions: Dict[str, ExecutionRequest] = {}
        self.execution_history: List[ExecutionResult] = []
        
        # Rate limiting
        self.global_rate_tracker: Dict[str, List[datetime]] = {
            "hourly": [], "minute": []
        }
        self.collection_rate_tracker: Dict[str, Dict[str, List[datetime]]] = {}
        self.wallet_rate_tracker: Dict[str, List[datetime]] = {
            "hourly": [], "minute": []
        }
        
        # Safety monitoring
        self.safety_level = SafetyLevel.SAFE
        self.circuit_breaker_state: Dict[str, bool] = {}
        self.exposure_tracker: Dict[str, Decimal] = {}
        self.performance_metrics: Dict[str, Any] = {}
        
        # Background tasks
        self._execution_task: Optional[asyncio.Task] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False
        
    async def start(self):
        """Start execution governor"""
        if self._running:
            return
            
        self._running = True
        self._execution_task = asyncio.create_task(self._execution_worker())
        self._monitoring_task = asyncio.create_task(self._monitoring_worker())
        
        log.info("🚀 Execution Governor started")
    
    async def stop(self):
        """Stop execution governor"""
        if not self._running:
            return
            
        self._running = False
        
        if self._execution_task:
            self._execution_task.cancel()
        if self._monitoring_task:
            self._monitoring_task.cancel()
        
        # Wait for tasks to complete
        await asyncio.gather(self._execution_task, self._monitoring_task, return_exceptions=True)
        
        log.info("🛑 Execution Governor stopped")
    
    async def submit_execution(self, request: ExecutionRequest) -> bool:
        """Submit execution request"""
        try:
            # Safety checks
            if not await self._safety_check(request):
                log.warning(f"❌ Safety check failed for request {request.request_id}")
                return False
            
            # Rate limiting check
            if not await self._rate_limit_check(request):
                log.warning(f"⏱️ Rate limit exceeded for request {request.request_id}")
                return False
            
            # Add to queue
            await self.execution_queue.put(request)
            self.active_executions[request.request_id] = request
            
            log.info(f"📤 Execution request submitted: {request.request_id}")
            return True
            
        except Exception as e:
            log.error(f"Error submitting execution request: {e}")
            return False
    
    async def can_place_offer(self, collection_address: str) -> bool:
        """Quick check if can place offer"""
        # Check global rate limits
        if not await self._check_global_rate_limits():
            return False
        
        # Check collection rate limits
        if not await self._check_collection_rate_limits(collection_address):
            return False
        
        # Check safety level
        if self.safety_level in [SafetyLevel.CRITICAL, SafetyLevel.EMERGENCY]:
            return False
        
        # Check circuit breaker
        if self.circuit_breaker_state.get(collection_address, False):
            return False
        
        return True
    
    async def _execution_worker(self):
        """Background worker for processing execution queue"""
        while self._running:
            try:
                # Get request from queue
                request = await asyncio.wait_for(self.execution_queue.get(), timeout=1.0)
                
                # Process request
                result = await self._execute_request(request)
                
                # Store result
                self.execution_history.append(result)
                
                # Clean up active executions
                self.active_executions.pop(request.request_id, None)
                
                # Update metrics
                await self._update_performance_metrics()
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"Error in execution worker: {e}")
    
    async def _monitoring_worker(self):
        """Background worker for monitoring and safety"""
        while self._running:
            try:
                # Clean up old rate limit entries
                await self._cleanup_rate_limits()
                
                # Update safety level
                await self._update_safety_level()
                
                # Update circuit breakers
                await self._update_circuit_breakers()
                
                # Log status
                await self._log_status()
                
                await asyncio.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                log.error(f"Error in monitoring worker: {e}")
    
    async def _execute_request(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a single request"""
        start_time = time.time()
        
        try:
            log.info(f"🔨 Executing request: {request.request_id}")
            
            # Execute based on action type
            if request.action == "place_offer":
                result = await self._execute_place_offer(request)
            elif request.action == "cancel_offer":
                result = await self._execute_cancel_offer(request)
            elif request.action == "buy_listing":
                result = await self._execute_buy_listing(request)
            else:
                raise ValueError(f"Unknown action: {request.action}")
            
            execution_time = time.time() - start_time
            
            return ExecutionResult(
                request_id=request.request_id,
                status=ExecutionStatus.COMPLETED,
                success=result.get("success", False),
                error_message=result.get("error"),
                execution_time=execution_time,
                transaction_hash=result.get("tx_hash"),
                gas_used=result.get("gas_used")
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            
            # Retry logic
            if request.retry_count < request.max_retries:
                request.retry_count += 1
                request.scheduled_at = datetime.now() + timedelta(seconds=5)
                await self.execution_queue.put(request)
                
                log.info(f"🔄 Retrying request {request.request_id} (attempt {request.retry_count})")
            
            return ExecutionResult(
                request_id=request.request_id,
                status=ExecutionStatus.FAILED,
                success=False,
                error_message=str(e),
                execution_time=execution_time
            )
    
    async def _execute_place_offer(self, request: ExecutionRequest) -> Dict[str, Any]:
        """Execute place offer"""
        # Implementation needed - integrate with actual OKX API
        log.info(f"📝 Placing offer: {request.token_id} @ {request.price} {request.currency}")
        
        # Simulate execution
        await asyncio.sleep(0.5)
        
        # Update exposure
        self.exposure_tracker[request.collection_address] = self.exposure_tracker.get(request.collection_address, Decimal("0")) + request.price
        
        return {
            "success": True,
            "tx_hash": f"0x{request.request_id}",
            "gas_used": 21000
        }
    
    async def _execute_cancel_offer(self, request: ExecutionRequest) -> Dict[str, Any]:
        """Execute cancel offer"""
        log.info(f"❌ Cancelling offer: {request.token_id}")
        
        # Update exposure
        self.exposure_tracker[request.collection_address] = self.exposure_tracker.get(request.collection_address, Decimal("0")) - request.price
        
        return {
            "success": True,
            "tx_hash": f"0xcancel_{request.request_id}",
            "gas_used": 21000
        }
    
    async def _execute_buy_listing(self, request: ExecutionRequest) -> Dict[str, Any]:
        """Execute buy listing"""
        log.info(f"💰 Buying listing: {request.token_id} @ {request.price} {request.currency}")
        
        return {
            "success": True,
            "tx_hash": f"0xbuy_{request.request_id}",
            "gas_used": 50000
        }
    
    async def _safety_check(self, request: ExecutionRequest) -> bool:
        """Perform safety checks"""
        # Exposure check
        current_exposure = self.exposure_tracker.get(request.collection_address, Decimal("0"))
        if current_exposure + request.price > self.safety_config.max_exposure_per_collection:
            log.warning(f"⚠️ Exposure limit exceeded for {request.collection_address}")
            return False
        
        # Total exposure check
        total_exposure = sum(self.exposure_tracker.values())
        if total_exposure + request.price > self.safety_config.max_total_exposure:
            log.warning(f"⚠️ Total exposure limit exceeded")
            return False
        
        # Safety level check
        if self.safety_level == SafetyLevel.EMERGENCY:
            log.warning("🚨 Emergency stop activated")
            return False
        
        return True
    
    async def _rate_limit_check(self, request: ExecutionRequest) -> bool:
        """Check rate limits"""
        now = datetime.now()
        
        # Global rate limits
        if not await self._check_global_rate_limits():
            return False
        
        # Collection rate limits
        if not await self._check_collection_rate_limits(request.collection_address):
            return False
        
        # Wallet rate limits
        if not await self._check_wallet_rate_limits():
            return False
        
        # Add to rate trackers
        await self._add_to_rate_trackers(request.collection_address, now)
        
        return True
    
    async def _check_global_rate_limits(self) -> bool:
        """Check global rate limits"""
        now = datetime.now()
        
        # Hourly limit
        hourly = [t for t in self.global_rate_tracker["hourly"] if t > now - timedelta(hours=1)]
        if len(hourly) >= self.rate_limit_config.global_offers_per_hour:
            return False
        
        # Minute limit
        minute = [t for t in self.global_rate_tracker["minute"] if t > now - timedelta(minutes=1)]
        if len(minute) >= self.rate_limit_config.global_offers_per_minute:
            return False
        
        return True
    
    async def _check_collection_rate_limits(self, collection_address: str) -> bool:
        """Check collection rate limits"""
        now = datetime.now()
        
        if collection_address not in self.collection_rate_tracker:
            self.collection_rate_tracker[collection_address] = {"hourly": [], "minute": []}
        
        tracker = self.collection_rate_tracker[collection_address]
        
        # Hourly limit
        hourly = [t for t in tracker["hourly"] if t > now - timedelta(hours=1)]
        if len(hourly) >= self.rate_limit_config.collection_offers_per_hour:
            return False
        
        # Minute limit
        minute = [t for t in tracker["minute"] if t > now - timedelta(minutes=1)]
        if len(minute) >= self.rate_limit_config.collection_offers_per_minute:
            return False
        
        return True
    
    async def _check_wallet_rate_limits(self) -> bool:
        """Check wallet rate limits"""
        now = datetime.now()
        
        # Hourly limit
        hourly = [t for t in self.wallet_rate_tracker["hourly"] if t > now - timedelta(hours=1)]
        if len(hourly) >= self.rate_limit_config.wallet_offers_per_hour:
            return False
        
        return True
    
    async def _add_to_rate_trackers(self, collection_address: str, timestamp: datetime):
        """Add timestamp to rate trackers"""
        # Global trackers
        self.global_rate_tracker["hourly"].append(timestamp)
        self.global_rate_tracker["minute"].append(timestamp)
        
        # Collection trackers
        if collection_address not in self.collection_rate_tracker:
            self.collection_rate_tracker[collection_address] = {"hourly": [], "minute": []}
        
        self.collection_rate_tracker[collection_address]["hourly"].append(timestamp)
        self.collection_rate_tracker[collection_address]["minute"].append(timestamp)
        
        # Wallet trackers
        self.wallet_rate_tracker["hourly"].append(timestamp)
    
    async def _cleanup_rate_limits(self):
        """Clean up old rate limit entries"""
        now = datetime.now()
        cutoff_hour = now - timedelta(hours=1)
        cutoff_minute = now - timedelta(minutes=1)
        
        # Clean global trackers
        self.global_rate_tracker["hourly"] = [t for t in self.global_rate_tracker["hourly"] if t > cutoff_hour]
        self.global_rate_tracker["minute"] = [t for t in self.global_rate_tracker["minute"] if t > cutoff_minute]
        
        # Clean collection trackers
        for collection in self.collection_rate_tracker:
            self.collection_rate_tracker[collection]["hourly"] = [t for t in self.collection_rate_tracker[collection]["hourly"] if t > cutoff_hour]
            self.collection_rate_tracker[collection]["minute"] = [t for t in self.collection_rate_tracker[collection]["minute"] if t > cutoff_minute]
        
        # Clean wallet trackers
        self.wallet_rate_tracker["hourly"] = [t for t in self.wallet_rate_tracker["hourly"] if t > cutoff_hour]
    
    async def _update_safety_level(self):
        """Update safety level based on metrics"""
        # Calculate safety metrics
        error_rate = await self._calculate_error_rate()
        avg_execution_time = await self._calculate_avg_execution_time()
        exposure_ratio = await self._calculate_exposure_ratio()
        
        # Determine safety level
        if error_rate > 0.5 or avg_execution_time > 10 or exposure_ratio > 0.9:
            self.safety_level = SafetyLevel.EMERGENCY
        elif error_rate > 0.3 or avg_execution_time > 5 or exposure_ratio > 0.7:
            self.safety_level = SafetyLevel.CRITICAL
        elif error_rate > 0.1 or avg_execution_time > 2 or exposure_ratio > 0.5:
            self.safety_level = SafetyLevel.WARNING
        else:
            self.safety_level = SafetyLevel.SAFE
    
    async def _update_circuit_breakers(self):
        """Update circuit breaker states"""
        for collection_address, exposure in self.exposure_tracker.items():
            if exposure > self.safety_config.max_exposure_per_collection * Decimal("0.8"):
                self.circuit_breaker_state[collection_address] = True
            else:
                self.circuit_breaker_state[collection_address] = False
    
    async def _log_status(self):
        """Log current status"""
        queue_size = self.execution_queue.qsize()
        active_count = len(self.active_executions)
        total_exposure = sum(self.exposure_tracker.values())
        
        log.info(f"📊 Status: Queue={queue_size}, Active={active_count}, Exposure={total_exposure}, Safety={self.safety_level.value}")
    
    async def _update_performance_metrics(self):
        """Update performance metrics"""
        recent_results = [r for r in self.execution_history if r.completed_at > datetime.now() - timedelta(hours=1)]
        
        if recent_results:
            success_rate = sum(1 for r in recent_results if r.success) / len(recent_results)
            avg_time = sum(r.execution_time or 0 for r in recent_results) / len(recent_results)
            
            self.performance_metrics = {
                "success_rate_1h": success_rate,
                "avg_execution_time_1h": avg_time,
                "total_executions_1h": len(recent_results),
                "queue_size": self.execution_queue.qsize(),
                "active_executions": len(self.active_executions),
                "safety_level": self.safety_level.value,
                "total_exposure": sum(self.exposure_tracker.values())
            }
    
    async def _calculate_error_rate(self) -> float:
        """Calculate error rate"""
        recent_results = [r for r in self.execution_history if r.completed_at > datetime.now() - timedelta(hours=1)]
        if not recent_results:
            return 0.0
        
        return sum(1 for r in recent_results if not r.success) / len(recent_results)
    
    async def _calculate_avg_execution_time(self) -> float:
        """Calculate average execution time"""
        recent_results = [r for r in self.execution_history if r.completed_at > datetime.now() - timedelta(hours=1) and r.execution_time]
        if not recent_results:
            return 0.0
        
        return sum(r.execution_time for r in recent_results) / len(recent_results)
    
    async def _calculate_exposure_ratio(self) -> float:
        """Calculate exposure ratio"""
        total_exposure = sum(self.exposure_tracker.values())
        return float(total_exposure / self.safety_config.max_total_exposure)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current status"""
        return {
            "running": self._running,
            "queue_size": self.execution_queue.qsize(),
            "active_executions": len(self.active_executions),
            "safety_level": self.safety_level.value,
            "circuit_breakers": self.circuit_breaker_state,
            "exposure_tracker": {k: float(v) for k, v in self.exposure_tracker.items()},
            "performance_metrics": self.performance_metrics
        }


# Factory function
def create_execution_governor() -> ExecutionGovernor:
    """Create execution governor instance"""
    return ExecutionGovernor()
