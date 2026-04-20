"""
OKX NFT Bot CLI v2 - Refactored with improved architecture

Features:
- Async command processing
- Better error handling
- Performance monitoring
- Integrated PnL and decision layers
"""

from __future__ import annotations

import asyncio
import logging
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict
from datetime import datetime

import click
from decimal import Decimal

# Import refactored components
from okx_nft_bot.sniper.parasite_hunter_v2 import ParasiteHunterV2, ParasiteHunterConfig, create_parasite_hunter_v2
from okx_nft_bot.pnl.engine import PnLEngine, create_pnl_engine
from okx_nft_bot.decision.engine import DecisionEngine, create_decision_engine
from okx_nft_bot.execution.governor import ExecutionGovernorV2Stub, create_execution_governor

log = logging.getLogger(__name__)


class BotContext:
    """Shared bot context across commands"""
    
    def __init__(self):
        self.pnl_engine: Optional[PnLEngine] = None
        self.decision_engine: Optional[DecisionEngine] = None
        self.execution_governor: Optional[ExecutionGovernorV2Stub] = None
        self.parasite_hunter: Optional[ParasiteHunterV2] = None
        
    async def initialize(self):
        """Initialize all components"""
        self.pnl_engine = create_pnl_engine()
        self.decision_engine = create_decision_engine()
        self.execution_governor = create_execution_governor()
        
        # Start execution governor
        await self.execution_governor.start()
        
        log.info("🚀 Bot components initialized")
    
    async def shutdown(self):
        """Shutdown all components"""
        if self.execution_governor:
            await self.execution_governor.stop()
        
        log.info("🛑 Bot components shutdown")


# Global context
ctx = BotContext()


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.option('--config', '-c', type=click.Path(exists=True), help='Config file path')
@click.pass_context
def cli(ctx_click, verbose, config):
    """OKX NFT Bot v2 - Advanced NFT trading with PnL tracking"""
    # Setup logging
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Store context
    ctx_click.ensure_object(dict)
    ctx_click.obj['verbose'] = verbose
    ctx_click.obj['config'] = config


@cli.command()
@click.option('--dry-run', is_flag=True, default=True, help='Run in dry-run mode')
@click.option('--cycles', type=int, default=3, help='Number of scan cycles')
@click.option('--interval', type=int, default=300, help='Interval between cycles (seconds)')
async def run_parasite_hunter(dry_run, cycles, interval):
    """Run optimized Parasite Hunter v2"""
    try:
        # Initialize components
        await ctx.initialize()
        
        # Create config
        config = ParasiteHunterConfig(
            enabled=True,
            dry_run=dry_run,
            scan_interval=interval
        )
        
        # Create hunter
        hunter = create_parasite_hunter_v2(config)
        
        click.echo(f"🦠 Starting Parasite Hunter v2")
        click.echo(f"🔄 Cycles: {cycles}, Interval: {interval}s")
        click.echo(f"🔧 Dry-run: {dry_run}")
        
        # Run cycles
        for i in range(cycles):
            click.echo(f"\n--- Cycle {i+1}/{cycles} ---")
            
            results = await hunter.run_scan_cycle()
            
            click.echo(f"📊 Results:")
            click.echo(f"  WL Capture: {results['phase1_wl']['offers_placed']} offers placed")
            click.echo(f"  Parasite Hunt: {results['phase2_parasite']['offers_placed']} offers placed")
            click.echo(f"  Missclick Buy: {results['phase3_missclick']['purchases']} purchases")
            click.echo(f"  Total time: {results['total_time']:.2f}s")
            
            if i < cycles - 1:  # Don't sleep after last cycle
                await asyncio.sleep(interval)
        
        # Show final metrics
        await _show_pnl_metrics()
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


@cli.command()
@click.option('--collection', type=str, help='Collection address')
@click.option('--limit', type=int, default=10, help='Number of top performers to show')
async def show_pnl(collection, limit):
    """Show PnL metrics"""
    try:
        await ctx.initialize()
        
        if collection:
            # Show specific collection
            pnl = ctx.pnl_engine.collection_pnl.get(collection)
            if pnl:
                click.echo(f"\n📊 Collection PnL: {pnl.collection_name}")
                click.echo(f"  Realized PnL: {pnl.realized_pnl} USDT")
                click.echo(f"  Unrealized PnL: {pnl.unrealized_pnl} USDT")
                click.echo(f"  Total PnL: {pnl.total_pnl} USDT")
                click.echo(f"  Fill Rate: {pnl.fill_rate:.2%}")
                click.echo(f"  Avg Holding: {pnl.avg_holding_hours:.1f} hours")
            else:
                click.echo(f"❌ Collection {collection} not found")
        else:
            # Show top performers
            top_performers = ctx.pnl_engine.get_top_performers(limit)
            
            click.echo(f"\n🏆 Top {limit} Collections by PnL:")
            for i, pnl in enumerate(top_performers, 1):
                click.echo(f"{i:2d}. {pnl.collection_name}")
                click.echo(f"     PnL: {pnl.total_pnl} USDT")
                click.echo(f"     Fill Rate: {pnl.fill_rate:.2%}")
                click.echo(f"     Exposure: {pnl.current_exposure} USDT")
        
        # Show portfolio summary
        portfolio = ctx.pnl_engine.portfolio_metrics
        click.echo(f"\n💰 Portfolio Summary:")
        click.echo(f"  Total PnL: {portfolio.total_pnl} USDT")
        click.echo(f"  Realized: {portfolio.total_realized_pnl} USDT")
        click.echo(f"  Unrealized: {portfolio.total_unrealized_pnl} USDT")
        click.echo(f"  Win Rate: {portfolio.win_rate:.2%}")
        click.echo(f"  Active Positions: {portfolio.active_positions}")
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


@cli.command()
@click.option('--collection', type=str, help='Collection address')
@click.option('--token-id', type=str, help='Token ID')
@click.option('--price', type=float, help='Offer price')
@click.option('--currency', type=str, default='USDT', help='Currency')
async def place_offer(collection, token_id, price, currency):
    """Place a single offer"""
    try:
        await ctx.initialize()
        
        if not all([collection, token_id, price]):
            click.echo("❌ Collection, token ID, and price are required", err=True)
            return
        
        # Create execution request
        from okx_nft_bot.execution.governor import ExecutionRequest
        
        request = ExecutionRequest(
            request_id=f"manual_{datetime.now().timestamp()}",
            collection_address=collection,
            token_id=token_id,
            action="place_offer",
            price=Decimal(str(price)),
            currency=currency
        )
        
        # Submit request
        success = await ctx.execution_governor.submit_execution(request)
        
        if success:
            click.echo(f"✅ Offer submitted: {token_id} @ {price} {currency}")
        else:
            click.echo(f"❌ Failed to submit offer", err=True)
        
        # Show status
        status = ctx.execution_governor.get_status()
        click.echo(f"\n📊 Execution Status:")
        click.echo(f"  Queue: {status['queue_size']}")
        click.echo(f"  Active: {status['active_executions']}")
        click.echo(f"  Safety: {status['safety_level']}")
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


@cli.command()
async def status():
    """Show bot status"""
    try:
        await ctx.initialize()
        
        # Execution governor status
        exec_status = ctx.execution_governor.get_status()
        
        click.echo("🤖 Bot Status:")
        click.echo(f"  Running: {exec_status['running']}")
        click.echo(f"  Queue Size: {exec_status['queue_size']}")
        click.echo(f"  Active Executions: {exec_status['active_executions']}")
        click.echo(f"  Safety Level: {exec_status['safety_level']}")
        
        # Performance metrics
        metrics = exec_status.get('performance_metrics', {})
        if metrics:
            click.echo(f"\n📈 Performance (1h):")
            click.echo(f"  Success Rate: {metrics.get('success_rate_1h', 0):.2%}")
            click.echo(f"  Avg Execution Time: {metrics.get('avg_execution_time_1h', 0):.2f}s")
            click.echo(f"  Total Executions: {metrics.get('total_executions_1h', 0)}")
        
        # Exposure
        exposure = exec_status.get('exposure_tracker', {})
        if exposure:
            total_exposure = sum(Decimal(str(v)) for v in exposure.values())
            click.echo(f"\n💰 Exposure:")
            click.echo(f"  Total: {total_exposure} USDT")
            click.echo(f"  Collections: {len(exposure)}")
        
        # Circuit breakers
        circuit_breakers = exec_status.get('circuit_breakers', {})
        if any(circuit_breakers.values()):
            click.echo(f"\n⚠️ Circuit Breakers Active:")
            for collection, active in circuit_breakers.items():
                if active:
                    click.echo(f"  {collection[:8]}...: ACTIVE")
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


@cli.command()
@click.option('--format', type=click.Choice(['json', 'table']), default='table', help='Output format')
@click.option('--output', type=click.Path(), help='Output file')
async def export_metrics(format, output):
    """Export all metrics"""
    try:
        await ctx.initialize()
        
        # Get metrics
        metrics = ctx.pnl_engine.export_metrics()
        
        if format == 'json':
            output_text = json.dumps(metrics, indent=2, default=str)
        else:  # table
            output_text = _format_metrics_as_table(metrics)
        
        if output:
            Path(output).write_text(output_text)
            click.echo(f"📄 Metrics exported to: {output}")
        else:
            click.echo(output_text)
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


@cli.command()
@click.option('--address', type=str, help='Collection address to analyze')
async def analyze_collection(address):
    """Analyze collection performance"""
    try:
        await ctx.initialize()
        
        if not address:
            click.echo("❌ Collection address required", err=True)
            return
        
        # Get collection PnL
        pnl = ctx.pnl_engine.collection_pnl.get(address)
        if not pnl:
            click.echo(f"❌ Collection {address} not found", err=True)
            return
        
        click.echo(f"\n📊 Collection Analysis: {pnl.collection_name}")
        click.echo(f"Address: {address}")
        
        # PnL metrics
        click.echo(f"\n💰 PnL Metrics:")
        click.echo(f"  Realized PnL: {pnl.realized_pnl} USDT")
        click.echo(f"  Unrealized PnL: {pnl.unrealized_pnl} USDT")
        click.echo(f"  Total PnL: {pnl.total_pnl} USDT")
        click.echo(f"  PnL per Offer: {pnl.pnl_per_offer} USDT")
        click.echo(f"  PnL per USDT Exposure: {pnl.pnl_per_usdt_exposure}")
        
        # Performance metrics
        click.echo(f"\n📈 Performance:")
        click.echo(f"  Total Trades: {pnl.total_trades}")
        click.echo(f"  Successful Trades: {pnl.successful_trades}")
        click.echo(f"  Fill Rate: {pnl.fill_rate:.2%}")
        click.echo(f"  Cancel Ratio: {pnl.cancel_ratio:.2%}")
        
        # Timing metrics
        click.echo(f"\n⏱️ Timing:")
        click.echo(f"  Avg Holding: {pnl.avg_holding_hours:.1f} hours")
        if pnl.median_time_to_fill:
            click.echo(f"  Median Time to Fill: {pnl.median_time_to_fill} minutes")
        
        # Exposure metrics
        click.echo(f"\n💸 Exposure:")
        click.echo(f"  Current Exposure: {pnl.current_exposure} USDT")
        click.echo(f"  Max Exposure: {pnl.max_exposure} USDT")
        click.echo(f"  Exposure Utilization: {pnl.exposure_utilization:.2%}")
        
        # Risk assessment
        click.echo(f"\n⚠️ Risk Assessment:")
        if pnl.fill_rate < 0.3:
            click.echo("  ⚠️ Low fill rate - consider adjusting strategy")
        if pnl.cancel_ratio > 0.5:
            click.echo("  ⚠️ High cancel ratio - market may be competitive")
        if pnl.avg_holding_hours > 48:
            click.echo("  ⚠️ Long holding periods - liquidity concerns")
        if pnl.current_exposure > pnl.max_exposure * 0.8:
            click.echo("  ⚠️ High exposure concentration")
        
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise
    finally:
        await ctx.shutdown()


async def _show_pnl_metrics():
    """Show PnL metrics"""
    if not ctx.pnl_engine:
        return
    
    portfolio = ctx.pnl_engine.portfolio_metrics
    click.echo(f"\n💰 PnL Summary:")
    click.echo(f"  Total PnL: {portfolio.total_pnl} USDT")
    click.echo(f"  Realized: {portfolio.total_realized_pnl} USDT")
    click.echo(f"  Unrealized: {portfolio.total_unrealized_pnl} USDT")
    click.echo(f"  Win Rate: {portfolio.win_rate:.2%}")


def _format_metrics_as_table(metrics: Dict[str, Any]) -> str:
    """Format metrics as table"""
    lines = []
    
    # Portfolio metrics
    portfolio = metrics.get('portfolio', {})
    lines.append("Portfolio Metrics:")
    lines.append(f"  Total PnL: {portfolio.get('total_pnl', 0)} USDT")
    lines.append(f"  Realized PnL: {portfolio.get('realized_pnl', 0)} USDT")
    lines.append(f"  Unrealized PnL: {portfolio.get('unrealized_pnl', 0)} USDT")
    lines.append(f"  Win Rate: {portfolio.get('win_rate', 0):.2%}")
    lines.append("")
    
    # Top collections
    collections = metrics.get('collections', {})
    if collections:
        lines.append("Top Collections:")
        for addr, data in list(collections.items())[:5]:
            name = data.get('name', addr[:8])
            pnl = data.get('total_pnl', 0)
            lines.append(f"  {name}: {pnl} USDT")
    
    return "\n".join(lines)


def main():
    """Main entry point"""
    # Run async CLI
    def run_async():
        return asyncio.run(cli())
    
    # Patch click to support async commands
    original_cli = cli
    async def async_cli(*args, **kwargs):
        return await original_cli.main(*args, **kwargs)
    
    cli.main = async_cli
    
    # Run CLI
    cli()


if __name__ == '__main__':
    main()
