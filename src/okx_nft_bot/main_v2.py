"""
OKX NFT Bot v2 - Main entry point with optimized architecture

Features:
- Async initialization
- Graceful shutdown
- Health monitoring
- Performance tracking
- Error recovery
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime

from okx_nft_bot.config.manager_v2 import ConfigManager, load_config
from okx_nft_bot.pnl.engine import PnLEngine, create_pnl_engine
from okx_nft_bot.decision.engine import DecisionEngine, create_decision_engine
from okx_nft_bot.execution.governor import ExecutionGovernor, create_execution_governor
from okx_nft_bot.sniper.parasite_hunter import ParasiteHunter
from okx_nft_bot.cli_v2 import cli as cli_v2

log = logging.getLogger(__name__)


class BotV2:
    """Main bot class with optimized architecture"""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.config_manager = ConfigManager(config_path)
        
        # Components
        self.config: Optional[any] = None
        self.pnl_engine: Optional[PnLEngine] = None
        self.decision_engine: Optional[DecisionEngine] = None
        self.execution_governor: Optional[ExecutionGovernor] = None
        self.parasite_hunter: Optional[ParasiteHunter] = None
        
        # State
        self.running = False
        self.startup_time: Optional[datetime] = None
        self.shutdown_requested = False
        
        # Background tasks
        self.background_tasks: list[asyncio.Task] = []
        
        # Health monitoring
        self.health_status: dict[str, any] = {}
        
    async def initialize(self) -> None:
        """Initialize all bot components"""
        try:
            log.info("🚀 Initializing OKX NFT Bot v2...")
            self.startup_time = datetime.now()
            
            # Load configuration
            log.info("📋 Loading configuration...")
            self.config = await self.config_manager.load()
            logging.getLogger().setLevel(getattr(logging, self.config.log_level))
            
            # Initialize PnL engine
            log.info("💰 Initializing PnL engine...")
            self.pnl_engine = create_pnl_engine()
            
            # Initialize decision engine
            log.info("🧠 Initializing decision engine...")
            self.decision_engine = create_decision_engine()
            
            # Initialize execution governor
            log.info("⚙️ Initializing execution governor...")
            self.execution_governor = create_execution_governor()
            await self.execution_governor.start()
            
            # Initialize parasite hunter
            if self.config.parasite_hunter.enabled:
                log.info("🦠 Initializing parasite hunter...")
                # Load configs for parasite hunter
                import json
                import os
                
                # Load binance whitelist
                whitelist_path = Path(__file__).parent.parent / "data" / "binance_whitelist.json"
                binance_whitelist = {}
                if whitelist_path.exists():
                    with open(whitelist_path) as f:
                        wl_list = json.load(f)
                        for item in wl_list:
                            addr = item.get("contract_address", "").lower()
                            if addr:
                                binance_whitelist[addr] = item
                
                # Load buy config
                buy_config_path = Path(__file__).parent.parent / "config" / "buy_config.json"
                buy_config = {}
                if buy_config_path.exists():
                    with open(buy_config_path) as f:
                        buy_config = json.load(f)
                
                self.parasite_hunter = ParasiteHunter(
                    binance_whitelist=binance_whitelist,
                    buy_config=buy_config
                )
            
            # Start configuration watching
            await self.config_manager.start_watching()
            
            # Start background tasks
            await self._start_background_tasks()
            
            log.info("✅ Bot initialization completed")
            
        except Exception as e:
            log.error(f"❌ Failed to initialize bot: {e}")
            raise
    
    async def start(self) -> None:
        """Start the bot"""
        if self.running:
            log.warning("⚠️ Bot is already running")
            return
        
        try:
            await self.initialize()
            self.running = True
            
            log.info("🤖 OKX NFT Bot v2 started successfully")
            
            # Show startup summary
            await self._show_startup_summary()
            
            # Keep running until shutdown
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(1)
                
        except Exception as e:
            log.error(f"❌ Failed to start bot: {e}")
            raise
        finally:
            await self.shutdown()
    
    async def shutdown(self) -> None:
        """Graceful shutdown"""
        if not self.running:
            return
        
        log.info("🛑 Shutting down OKX NFT Bot v2...")
        self.running = False
        
        try:
            # Cancel background tasks
            for task in self.background_tasks:
                task.cancel()
            
            if self.background_tasks:
                await asyncio.gather(*self.background_tasks, return_exceptions=True)
            
            # Stop parasite hunter
            if self.parasite_hunter:
                log.info("🦠 Stopping parasite hunter...")
                # Parasite hunter doesn't have explicit stop method in current implementation
            
            # Stop execution governor
            if self.execution_governor:
                log.info("⚙️ Stopping execution governor...")
                await self.execution_governor.stop()
            
            # Stop configuration watching
            await self.config_manager.stop_watching()
            
            # Save configuration
            await self.config_manager.save()
            
            # Show shutdown summary
            await self._show_shutdown_summary()
            
            log.info("✅ Bot shutdown completed")
            
        except Exception as e:
            log.error(f"❌ Error during shutdown: {e}")
    
    async def _start_background_tasks(self) -> None:
        """Start background monitoring tasks"""
        # Health monitoring
        if self.config.metrics_enabled:
            self.background_tasks.append(
                asyncio.create_task(self._health_monitoring_worker())
            )
        
        # Performance monitoring
        self.background_tasks.append(
            asyncio.create_task(self._performance_monitoring_worker())
        )
        
        # PnL reporting
        if self.config.pnl.enabled and self.config.pnl.reporting.get("auto_backup"):
            self.background_tasks.append(
                asyncio.create_task(self._pnl_reporting_worker())
            )
        
        # Parasite hunter scanning
        if self.config.parasite_hunter.enabled and self.parasite_hunter:
            self.background_tasks.append(
                asyncio.create_task(self._parasite_hunter_worker())
            )
    
    async def _health_monitoring_worker(self) -> None:
        """Background health monitoring"""
        while self.running:
            try:
                await self._update_health_status()
                await asyncio.sleep(self.config.health_check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ Health monitoring error: {e}")
                await asyncio.sleep(30)  # Back off on error
    
    async def _performance_monitoring_worker(self) -> None:
        """Background performance monitoring"""
        while self.running:
            try:
                await self._update_performance_metrics()
                await asyncio.sleep(60)  # Update every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ Performance monitoring error: {e}")
                await asyncio.sleep(60)
    
    async def _pnl_reporting_worker(self) -> None:
        """Background PnL reporting"""
        interval = self.config.pnl.reporting.get("export_interval_minutes", 60)
        
        while self.running:
            try:
                await self._export_pnl_metrics()
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ PnL reporting error: {e}")
                await asyncio.sleep(interval * 60)
    
    async def _parasite_hunter_worker(self) -> None:
        """Background parasite hunter scanning"""
        scan_interval = getattr(self.config.parasite_hunter, 'scan_interval', 300)
        
        while self.running:
            try:
                if self.parasite_hunter and self.parasite_hunter.enabled:
                    log.info("🦠 Running parasite hunter scan cycle...")
                    # Run one scan cycle
                    report = self.parasite_hunter.scan_all()
                    if report:
                        log.info(f"📊 Parasite hunter scan complete: {report.offers_placed} placed, {report.offers_failed} failed, {report.collections_skipped} skipped")
                await asyncio.sleep(scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ Parasite hunter scan error: {e}")
                await asyncio.sleep(60)  # Back off on error
    
    async def _update_health_status(self) -> None:
        """Update health status"""
        health = {
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": (datetime.now() - self.startup_time).total_seconds() if self.startup_time else 0,
            "components": {}
        }
        
        # Check PnL engine
        if self.pnl_engine:
            health["components"]["pnl_engine"] = {
                "status": "healthy",
                "collections_tracked": len(self.pnl_engine.collection_pnl),
                "total_pnl": float(self.pnl_engine.portfolio_metrics.total_pnl)
            }
        
        # Check decision engine
        if self.decision_engine:
            health["components"]["decision_engine"] = {
                "status": "healthy",
                "decisions_made": len(self.decision_engine.decision_history)
            }
        
        # Check execution governor
        if self.execution_governor:
            status = self.execution_governor.get_status()
            health["components"]["execution_governor"] = {
                "status": "healthy" if status["running"] else "stopped",
                "queue_size": status["queue_size"],
                "active_executions": status["active_executions"],
                "safety_level": status["safety_level"]
            }
        
        # Check parasite hunter
        if self.parasite_hunter:
            health["components"]["parasite_hunter"] = {
                "status": "healthy",
                "performance_metrics": self.parasite_hunter.performance_metrics
            }
        
        self.health_status = health
    
    async def _update_performance_metrics(self) -> None:
        """Update performance metrics"""
        if not self.execution_governor:
            return
        
        metrics = self.execution_governor.performance_metrics
        if metrics:
            log.debug(f"📊 Performance: Success Rate={metrics.get('success_rate_1h', 0):.2%}, "
                     f"Avg Time={metrics.get('avg_execution_time_1h', 0):.2f}s")
    
    async def _export_pnl_metrics(self) -> None:
        """Export PnL metrics"""
        if not self.pnl_engine:
            return
        
        try:
            metrics = self.pnl_engine.export_metrics()
            
            # Save to file
            backup_dir = Path(self.config.backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"pnl_metrics_{timestamp}.json"
            filepath = backup_dir / filename
            
            import json
            with open(filepath, 'w') as f:
                json.dump(metrics, f, indent=2, default=str)
            
            log.debug(f"💾 PnL metrics exported to {filepath}")
            
        except Exception as e:
            log.error(f"❌ Failed to export PnL metrics: {e}")
    
    async def _show_startup_summary(self) -> None:
        """Show startup summary"""
        log.info("🎯 Startup Summary:")
        log.info(f"  Collections: {len(self.config.collections)} configured, {len(self.config_manager.get_enabled_collections())} enabled")
        log.info(f"  PnL Engine: {'✅' if self.config.pnl.enabled else '❌'}")
        log.info(f"  Decision Engine: {'✅' if self.config.decision.enabled else '❌'}")
        log.info(f"  Execution Governor: {'✅' if self.config.execution.enabled else '❌'}")
        log.info(f"  Parasite Hunter: {'✅' if self.config.parasite_hunter.enabled else '❌'}")
        log.info(f"  Dry Run: {'✅' if self.config.execution.dry_run else '❌'}")
        log.info(f"  Max Exposure: {self.config.execution.max_total_exposure} USDT")
    
    async def _show_shutdown_summary(self) -> None:
        """Show shutdown summary"""
        if not self.startup_time:
            return
        
        uptime = datetime.now() - self.startup_time
        
        log.info("📊 Shutdown Summary:")
        log.info(f"  Uptime: {uptime}")
        
        if self.pnl_engine:
            portfolio = self.pnl_engine.portfolio_metrics
            log.info(f"  Final PnL: {portfolio.total_pnl} USDT")
            log.info(f"  Win Rate: {portfolio.win_rate:.2%}")
        
        if self.execution_governor:
            status = self.execution_governor.get_status()
            log.info(f"  Total Executions: {len(self.execution_governor.execution_history)}")
        
        log.info("👋 Goodbye!")
    
    def request_shutdown(self) -> None:
        """Request graceful shutdown"""
        log.info("🛑 Shutdown requested...")
        self.shutdown_requested = True


class GracefulShutdownHandler:
    """Handle graceful shutdown signals"""
    
    def __init__(self, bot: BotV2):
        self.bot = bot
    
    def handle_signal(self, signum, frame):
        """Handle shutdown signal"""
        log.info(f"🛑 Received signal {signum}, initiating graceful shutdown...")
        self.bot.request_shutdown()


def setup_signal_handlers(bot: BotV2) -> None:
    """Setup signal handlers for graceful shutdown"""
    handler = GracefulShutdownHandler(bot)
    
    # Handle SIGINT (Ctrl+C) and SIGTERM
    signal.signal(signal.SIGINT, handler.handle_signal)
    signal.signal(signal.SIGTERM, handler.handle_signal)


async def run_bot(config_path: Optional[str] = None) -> None:
    """Main bot runner"""
    bot = BotV2(config_path)
    setup_signal_handlers(bot)
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("🛑 Received keyboard interrupt")
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        raise
    finally:
        await bot.shutdown()


def main() -> None:
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="OKX NFT Bot v2")
    parser.add_argument("--config", "-c", type=str, help="Configuration file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--cli", action="store_true", help="Run CLI instead of bot")
    
    args, unknown = parser.parse_known_args()
    
    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    try:
        if args.cli or len(unknown) > 0:
            # Run CLI
            import sys
            sys.argv = [sys.argv[0]] + unknown
            cli_v2()
        else:
            # Run bot
            asyncio.run(run_bot(args.config))
    
    except KeyboardInterrupt:
        log.info("🛑 Interrupted by user")
        sys.exit(0)
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
