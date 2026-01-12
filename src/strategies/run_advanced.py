#!/usr/bin/env python3
"""
Advanced Strategy Runner - Post Jan 2026 Edition.

Runs the fee-aware Advanced Maker Arb strategy that:
1. Snipes fee-free 1-hour markets (old strategy works!)
2. Exploits low-fee extremes on 15-min markets
3. Provides liquidity for rebates on mid-priced 15-min markets

Usage:
    python -m src.strategies.run_advanced

Configuration via .env:
    MAKER_CAPITAL=500.0
    MAKER_VIRTUAL_MODE=true
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

import structlog
from src.utils.logging import setup_logging

# Setup logging first
setup_logging()
logger = structlog.get_logger()

from src.feeds.binance import BinanceFeed
from src.feeds.coinbase import CoinbaseFeed
from src.feeds.kraken import KrakenFeed
from src.feeds.polymarket import PolymarketFeed
from src.engine.consensus import ConsensusEngine
from src.strategies.advanced_maker_arb import AdvancedMakerArb
from src.utils.alerts import DiscordAlerter
from config.settings import settings


class AdvancedRunner:
    """
    Runs the Advanced Maker Arb strategy.
    
    Key features:
    - Scans both 15-min AND 1-hour markets
    - Fee-aware opportunity detection
    - Virtual P&L tracking with CSV export
    """
    
    def __init__(self):
        self.logger = logger.bind(component="advanced_runner")
        
        # Configuration
        self.capital = float(os.getenv("MAKER_CAPITAL", "500.0"))
        self.virtual_mode = os.getenv("MAKER_VIRTUAL_MODE", "true").lower() == "true"
        self.assets = ["ETH", "SOL"]  # Focus on these for now
        
        # Components
        self.feeds: dict[str, dict] = {}
        self.consensus_engines: dict[str, ConsensusEngine] = {}
        
        # Market feeds (both 15-min and 1-hour where available)
        self.pm_feeds_15m: dict[str, PolymarketFeed] = {}
        # self.pm_feeds_1h: dict[str, PolymarketFeed] = {}  # TODO: Add 1-hour market discovery
        
        # Strategy
        self.strategy: AdvancedMakerArb = None
        
        # Discord alerter
        self.discord: DiscordAlerter = None
        
        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._start_time = None
    
    async def initialize(self) -> bool:
        """Initialize all components."""
        mode_str = "🧪 VIRTUAL" if self.virtual_mode else "💰 REAL"
        self.logger.info(
            f"🏦 Initializing Advanced Maker Arb ({mode_str})",
            capital=f"${self.capital:.2f}",
            assets=self.assets,
        )
        
        # Initialize Discord alerter
        discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
        if discord_webhook:
            self.discord = DiscordAlerter(discord_webhook)
            self.logger.info("✅ Discord alerter initialized")
        else:
            self.logger.warning("⚠️ No DISCORD_WEBHOOK_URL - alerts disabled")
        
        # Initialize strategy
        self.strategy = AdvancedMakerArb(
            virtual_mode=self.virtual_mode,
            capital_usd=self.capital,
            discord_alerter=self.discord,
        )
        
        self.strategy.set_callbacks(
            on_opportunity=self._on_opportunity,
        )
        
        # Initialize feeds for each asset
        for asset in self.assets:
            try:
                await self._init_asset_feeds(asset)
            except Exception as e:
                self.logger.error(f"Failed to initialize {asset} feeds", error=str(e))
                continue
        
        if not self.feeds:
            self.logger.error("❌ No assets initialized")
            return False
        
        self.logger.info(
            "✅ Advanced strategy initialized",
            assets=list(self.feeds.keys()),
        )
        return True
    
    async def _init_asset_feeds(self, asset: str) -> None:
        """Initialize feeds for a single asset."""
        symbols = settings.exchanges.symbols.get(asset, {})
        
        binance = BinanceFeed(symbols.get("binance", f"{asset.lower()}usdt"))
        coinbase = CoinbaseFeed(symbols.get("coinbase", f"{asset}-USD"))
        kraken = KrakenFeed(symbols.get("kraken", f"{asset}/USD"))
        
        pm_feed = PolymarketFeed(asset=asset)
        consensus = ConsensusEngine()
        
        def make_callback(eng, feed, name):
            def cb(tick):
                metrics = feed.get_metrics()
                eng.update_exchange(name, metrics)
            return cb
        
        binance.add_callback(make_callback(consensus, binance, "binance"))
        coinbase.add_callback(make_callback(consensus, coinbase, "coinbase"))
        kraken.add_callback(make_callback(consensus, kraken, "kraken"))
        
        self.feeds[asset] = {
            "binance": binance,
            "coinbase": coinbase,
            "kraken": kraken,
        }
        self.consensus_engines[asset] = consensus
        self.pm_feeds_15m[asset] = pm_feed
    
    async def start(self) -> None:
        """Start all strategies."""
        import time
        self._running = True
        self._start_time = time.time()
        
        # Start all feeds
        for asset, feeds in self.feeds.items():
            for name, feed in feeds.items():
                asyncio.create_task(feed.start())
                self.logger.debug(f"Started {name} feed for {asset}")
        
        for asset, pm_feed in self.pm_feeds_15m.items():
            asyncio.create_task(pm_feed.start())
            self.logger.debug(f"Started Polymarket 15m feed for {asset}")
        
        # Wait for feeds to connect
        await asyncio.sleep(5)
        
        # Start strategy
        await self.strategy.start()
        
        # Run loops
        await asyncio.gather(
            self._scan_loop(),
            self._status_loop(),
        )
    
    async def _scan_loop(self) -> None:
        """Main scanning loop."""
        self.logger.info("🔍 Scan loop started - monitoring for opportunities")
        
        while self._running and not self._shutdown_event.is_set():
            try:
                for asset in self.feeds.keys():
                    if self._shutdown_event.is_set():
                        return
                    
                    consensus = self.consensus_engines.get(asset)
                    pm_feed = self.pm_feeds_15m.get(asset)
                    
                    if not consensus or not pm_feed:
                        continue
                    
                    consensus_data = consensus.compute_consensus()
                    pm_data = pm_feed.get_data()
                    
                    if not consensus_data or not pm_data:
                        continue
                    
                    # Check 15-min market
                    await self.strategy.check_opportunity(
                        asset=asset,
                        market_type="15m",
                        consensus=consensus_data,
                        pm_data=pm_data,
                    )
                    
                    # TODO: Also check 1-hour markets when we add that feed
                
                # Interruptible sleep
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=0.5)
                    return  # Shutdown signaled
                except asyncio.TimeoutError:
                    pass  # Normal, continue scanning
                
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.error("Scan loop error", error=str(e))
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
                    return
                except asyncio.TimeoutError:
                    pass
    
    async def _status_loop(self) -> None:
        """Periodic status logging."""
        import time
        
        while self._running and not self._shutdown_event.is_set():
            try:
                # Interruptible 60s sleep
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=60.0)
                    return  # Shutdown signaled
                except asyncio.TimeoutError:
                    pass  # Normal, log status
                
                if self._shutdown_event.is_set():
                    return
                
                runtime = time.time() - self._start_time
                hours = int(runtime // 3600)
                minutes = int((runtime % 3600) // 60)
                
                # Get prices
                prices = {}
                for asset, consensus in self.consensus_engines.items():
                    data = consensus.compute_consensus()
                    if data:
                        prices[asset] = f"${data.consensus_price:.2f}"
                
                summary = self.strategy.get_summary()
                
                self.logger.info(
                    "📊 STRATEGY STATUS",
                    runtime=f"{hours}h {minutes}m",
                    prices=prices,
                    **summary,
                )
                
            except asyncio.CancelledError:
                return
            except Exception:
                pass
    
    def _on_opportunity(self, log) -> None:
        """Called when an opportunity is detected."""
        self.logger.info(
            f"💰 {log.strategy.upper()} OPPORTUNITY",
            asset=log.asset,
            market_type=log.market_type,
            gap=f"{log.potential_gap_pct:.2%}",
            fee=f"{log.dynamic_fee_pct:.2%}",
            net_pnl=f"${log.net_virtual_pnl:.2f}",
        )
    
    async def stop(self) -> None:
        """Stop gracefully."""
        self._running = False
        self._shutdown_event.set()
        
        # Send final summary to Discord
        if self.strategy and self.discord:
            try:
                summary = self.strategy.get_summary()
                await self.discord.send_maker_arb_daily_summary(summary)
            except Exception as e:
                self.logger.debug("Failed to send final Discord summary", error=str(e))
        
        if self.strategy:
            await self.strategy.stop()
        
        # Close Discord client
        if self.discord:
            await self.discord.close()
        
        for asset, feeds in self.feeds.items():
            for name, feed in feeds.items():
                try:
                    await feed.stop()
                except Exception:
                    pass
        
        for pm_feed in self.pm_feeds_15m.values():
            try:
                await pm_feed.stop()
            except Exception:
                pass
        
        self.logger.info("✅ Advanced runner stopped")


async def main():
    """Main entry point."""
    runner = AdvancedRunner()
    
    # Use asyncio-native signal handling
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("🛑 Shutdown requested (Ctrl+C)...")
        stop_event.set()
    
    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    print("""
╔══════════════════════════════════════════════════════════════╗
║     ADVANCED MAKER ARB - POST JAN 2026 FEE STRUCTURE         ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Strategy Modes:                                             ║
║    📈 SNIPER - Fee-free 1h/daily markets                    ║
║    🎯 EXTREME - Low-fee plays at price extremes             ║
║    🏦 MAKER - Rebate earning on mid-price 15m markets       ║
║                                                              ║
║  All trades logged to logs/maker_arb/*.csv                  ║
║  Press Ctrl+C to stop and export                            ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    if not await runner.initialize():
        logger.error("❌ Failed to initialize")
        return 1
    
    try:
        # Run until stop signal
        run_task = asyncio.create_task(runner.start())
        stop_task = asyncio.create_task(stop_event.wait())
        
        # Wait for either completion or stop signal
        done, pending = await asyncio.wait(
            [run_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        
        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
                
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("🧹 Cleaning up...")
        await runner.stop()
        logger.info("✅ Shutdown complete")
    
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Force quit")
        exit_code = 0
    sys.exit(exit_code)

