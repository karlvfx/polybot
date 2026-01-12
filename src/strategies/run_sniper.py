#!/usr/bin/env python3
"""
Volatility Sniper Runner.

Runs the volatility spike strategy using existing infrastructure.

Usage:
    python -m src.strategies.run_sniper

Configuration via .env:
    SNIPER_POSITION_SIZE=20.0  # USD per spike (split between YES/NO)
    SNIPER_MIN_DISCOUNT=0.05   # 5% minimum discount required
    SNIPER_ASSETS=ETH,SOL      # Assets to monitor (avoid BTC - MMs too fast)
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
from src.trading.maker_orders import MakerOrderExecutor
from src.strategies.volatility_sniper import VolatilitySniper
from config.settings import settings


class SniperRunner:
    """
    Runs the Volatility Sniper strategy.
    
    Monitors configured assets for volatility spikes and executes
    dual-sided trades when discounts are detected.
    """
    
    def __init__(self):
        self.logger = logger.bind(component="sniper_runner")
        
        # Configuration
        self.position_size = float(os.getenv("SNIPER_POSITION_SIZE", "20.0"))
        self.min_discount = float(os.getenv("SNIPER_MIN_DISCOUNT", "0.05"))
        self.assets = os.getenv("SNIPER_ASSETS", "ETH,SOL").split(",")
        self.virtual_mode = os.getenv("SNIPER_VIRTUAL_MODE", "true").lower() == "true"
        
        # Components per asset
        self.feeds: dict[str, dict] = {}
        self.consensus_engines: dict[str, ConsensusEngine] = {}
        self.pm_feeds: dict[str, PolymarketFeed] = {}
        
        # Strategy
        self.executor: MakerOrderExecutor = None
        self.sniper: VolatilitySniper = None
        
        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()
    
    async def initialize(self) -> bool:
        """Initialize all components."""
        mode_str = "🧪 VIRTUAL" if self.virtual_mode else "💰 REAL"
        self.logger.info(
            f"🎯 Initializing Volatility Sniper ({mode_str})",
            assets=self.assets,
            position_size=f"${self.position_size}",
            min_discount=f"{self.min_discount:.1%}",
            virtual_mode=self.virtual_mode,
        )
        
        # Initialize executor (only needed for real mode)
        if not self.virtual_mode:
            private_key = os.getenv("PRIVATE_KEY", "")
            if not private_key:
                self.logger.error("❌ PRIVATE_KEY not set in .env")
                return False
            
            self.executor = MakerOrderExecutor(private_key=private_key)
            if not await self.executor.initialize():
                self.logger.error("❌ Failed to initialize executor")
                return False
        else:
            self.logger.info("🧪 Virtual mode - skipping executor initialization")
        
        # Initialize sniper
        self.sniper = VolatilitySniper(
            executor=self.executor if not self.virtual_mode else None,
            position_size_usd=self.position_size,
            min_discount_pct=self.min_discount,
            virtual_mode=self.virtual_mode,
        )
        
        # Set up callbacks
        self.sniper.set_callbacks(
            on_spike_detected=self._on_spike_detected,
            on_position_opened=self._on_position_opened,
            on_position_closed=self._on_position_closed,
        )
        
        # Initialize feeds for each asset
        for asset in self.assets:
            asset = asset.strip().upper()
            self.logger.info(f"Initializing feeds for {asset}")
            
            try:
                await self._init_asset_feeds(asset)
            except Exception as e:
                self.logger.error(f"Failed to initialize {asset} feeds", error=str(e))
                continue
        
        if not self.feeds:
            self.logger.error("❌ No assets initialized")
            return False
        
        self.logger.info(
            "✅ Sniper initialized",
            active_assets=list(self.feeds.keys()),
        )
        return True
    
    async def _init_asset_feeds(self, asset: str) -> None:
        """Initialize feeds for a single asset."""
        # Get exchange symbols for this asset
        symbols = settings.exchanges.symbols.get(asset, {})
        
        # Exchange feeds
        binance = BinanceFeed(symbols.get("binance", f"{asset.lower()}usdt"))
        coinbase = CoinbaseFeed(symbols.get("coinbase", f"{asset}-USD"))
        kraken = KrakenFeed(symbols.get("kraken", f"{asset}/USD"))
        
        # Polymarket feed with auto-discovery
        pm_feed = PolymarketFeed(asset=asset)
        
        # Consensus engine
        consensus = ConsensusEngine()
        
        # Register callbacks
        def make_callback(eng, feed, name):
            def cb(tick):
                metrics = feed.get_metrics()
                eng.update_exchange(name, metrics)
            return cb
        
        binance.add_callback(make_callback(consensus, binance, "binance"))
        coinbase.add_callback(make_callback(consensus, coinbase, "coinbase"))
        kraken.add_callback(make_callback(consensus, kraken, "kraken"))
        
        # Store
        self.feeds[asset] = {
            "binance": binance,
            "coinbase": coinbase,
            "kraken": kraken,
        }
        self.consensus_engines[asset] = consensus
        self.pm_feeds[asset] = pm_feed
    
    async def start(self) -> None:
        """Start the sniper."""
        self._running = True
        
        # Start all feeds
        feed_tasks = []
        for asset, feeds in self.feeds.items():
            for name, feed in feeds.items():
                feed_tasks.append(asyncio.create_task(feed.start()))
                self.logger.info(f"Started {name} feed for {asset}")
        
        # Start PM feeds
        for asset, pm_feed in self.pm_feeds.items():
            feed_tasks.append(asyncio.create_task(pm_feed.start()))
            self.logger.info(f"Started Polymarket feed for {asset}")
        
        # Wait for feeds to connect
        await asyncio.sleep(5)
        
        # Start sniper
        await self.sniper.start()
        
        # Main loop
        await self._main_loop()
    
    async def _main_loop(self) -> None:
        """Main monitoring loop."""
        self.logger.info("🎯 Sniper active - monitoring for opportunities")
        
        check_interval = 0.5  # Check every 500ms
        status_interval = 30  # Status log every 30s
        last_status = 0
        
        while self._running and not self._shutdown_event.is_set():
            try:
                now = asyncio.get_event_loop().time()
                
                # Check each asset for opportunities
                for asset in self.feeds.keys():
                    consensus = self.consensus_engines.get(asset)
                    pm_feed = self.pm_feeds.get(asset)
                    
                    if not consensus or not pm_feed:
                        continue
                    
                    # Get current data
                    consensus_data = consensus.compute_consensus()
                    pm_data = pm_feed.get_data()
                    
                    if not consensus_data or not pm_data:
                        continue
                    
                    # Check for spike opportunity
                    await self.sniper.check_opportunity(asset, consensus_data, pm_data)
                
                # Periodic status log
                if now - last_status >= status_interval:
                    last_status = now
                    self._log_status()
                
                await asyncio.sleep(check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in main loop", error=str(e))
                await asyncio.sleep(1)
        
        self.logger.info("🛑 Sniper stopped")
    
    def _log_status(self) -> None:
        """Log current status."""
        stats = self.sniper.get_stats_summary()
        
        # Get price data
        prices = {}
        for asset, consensus in self.consensus_engines.items():
            data = consensus.compute_consensus()
            if data:
                prices[asset] = f"${data.consensus_price:.2f}"
        
        self.logger.info(
            "📊 Sniper Status",
            prices=prices,
            **stats,
        )
    
    def _on_spike_detected(self, asset: str, magnitude: float, price: float) -> None:
        """Called when a spike is detected."""
        self.logger.info(
            "🌪️ Spike callback",
            asset=asset,
            magnitude=f"{magnitude:+.2%}",
            price=f"${price:.2f}",
        )
    
    def _on_position_opened(self, position) -> None:
        """Called when a position is opened."""
        self.logger.info(
            "🎯 Position opened callback",
            position_id=position.position_id,
            discount=f"{position.discount_pct:.1%}",
        )
    
    def _on_position_closed(self, position) -> None:
        """Called when a position is closed."""
        self.logger.info(
            "💰 Position closed callback",
            position_id=position.position_id,
            pnl=f"${position.realized_pnl:.2f}",
        )
    
    async def stop(self) -> None:
        """Stop the sniper gracefully."""
        self._running = False
        self._shutdown_event.set()
        
        # Stop sniper
        if self.sniper:
            await self.sniper.stop()
        
        # Stop feeds
        for asset, feeds in self.feeds.items():
            for name, feed in feeds.items():
                try:
                    await feed.stop()
                except Exception:
                    pass
        
        for pm_feed in self.pm_feeds.values():
            try:
                await pm_feed.stop()
            except Exception:
                pass
        
        self.logger.info("✅ Sniper shutdown complete")


async def main():
    """Main entry point."""
    runner = SniperRunner()
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        logger.info("🛑 Shutdown requested...")
        asyncio.create_task(runner.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize
    if not await runner.initialize():
        logger.error("❌ Failed to initialize")
        return 1
    
    # Run
    try:
        await runner.start()
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

