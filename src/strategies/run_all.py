#!/usr/bin/env python3
"""
Combined Strategy Runner.

Runs multiple strategies simultaneously:
1. Volatility Sniper - Captures MM panic during price spikes
2. Cross-Platform Arb Scanner - Finds Kalshi ↔ Polymarket price gaps

Usage:
    python -m src.strategies.run_all

Configuration via .env:
    # Sniper
    SNIPER_POSITION_SIZE=20.0
    SNIPER_MIN_DISCOUNT=0.05
    SNIPER_ASSETS=ETH,SOL
    SNIPER_VIRTUAL_MODE=true
    
    # Cross-Arb
    ARB_MIN_PROFIT_PCT=0.02
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
from src.strategies.volatility_sniper import VolatilitySniper
from src.strategies.cross_arb import CrossPlatformArbScanner
from config.settings import settings


class CombinedStrategyRunner:
    """
    Runs multiple strategies simultaneously.
    
    Each strategy monitors independently and logs opportunities.
    In virtual mode, no real trades are executed.
    """
    
    def __init__(self):
        self.logger = logger.bind(component="strategy_runner")
        
        # Configuration
        self.sniper_position_size = float(os.getenv("SNIPER_POSITION_SIZE", "20.0"))
        self.sniper_min_discount = float(os.getenv("SNIPER_MIN_DISCOUNT", "0.05"))
        self.sniper_assets = os.getenv("SNIPER_ASSETS", "ETH,SOL").split(",")
        self.virtual_mode = os.getenv("SNIPER_VIRTUAL_MODE", "true").lower() == "true"
        
        self.arb_min_profit = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.02"))
        
        # Components per asset
        self.feeds: dict[str, dict] = {}
        self.consensus_engines: dict[str, ConsensusEngine] = {}
        self.pm_feeds: dict[str, PolymarketFeed] = {}
        
        # Strategies
        self.sniper: VolatilitySniper = None
        self.arb_scanner: CrossPlatformArbScanner = None
        
        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # Stats tracking
        self._start_time = None
        self._sniper_opportunities = 0
        self._arb_opportunities = 0
    
    async def initialize(self) -> bool:
        """Initialize all components."""
        mode_str = "🧪 VIRTUAL" if self.virtual_mode else "💰 REAL"
        self.logger.info(
            f"🚀 Initializing Combined Strategy Runner ({mode_str})",
            sniper_assets=self.sniper_assets,
            sniper_position=f"${self.sniper_position_size}",
            sniper_discount=f"{self.sniper_min_discount:.1%}",
            arb_min_profit=f"{self.arb_min_profit:.1%}",
        )
        
        # Initialize Volatility Sniper
        self.sniper = VolatilitySniper(
            executor=None,  # Virtual mode
            position_size_usd=self.sniper_position_size,
            min_discount_pct=self.sniper_min_discount,
            virtual_mode=True,  # Always virtual for now
        )
        
        self.sniper.set_callbacks(
            on_spike_detected=self._on_spike_detected,
            on_position_opened=self._on_sniper_position,
        )
        
        # Initialize Cross-Platform Arb Scanner
        self.arb_scanner = CrossPlatformArbScanner(
            min_arb_pct=self.arb_min_profit,
            virtual_mode=True,
        )
        
        self.arb_scanner.set_callbacks(
            on_opportunity_found=self._on_arb_opportunity,
        )
        
        # Initialize feeds for sniper
        for asset in self.sniper_assets:
            asset = asset.strip().upper()
            try:
                await self._init_asset_feeds(asset)
            except Exception as e:
                self.logger.error(f"Failed to initialize {asset} feeds", error=str(e))
                continue
        
        if not self.feeds:
            self.logger.warning("⚠️ No sniper assets initialized")
        
        self.logger.info(
            "✅ Strategies initialized",
            sniper_assets=list(self.feeds.keys()),
            arb_scanner="ready",
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
        self.pm_feeds[asset] = pm_feed
    
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
        
        for asset, pm_feed in self.pm_feeds.items():
            asyncio.create_task(pm_feed.start())
            self.logger.debug(f"Started Polymarket feed for {asset}")
        
        # Wait for feeds to connect
        await asyncio.sleep(5)
        
        # Start strategies
        await self.sniper.start()
        
        # Run both loops concurrently
        await asyncio.gather(
            self._sniper_loop(),
            self._arb_scanner_loop(),
            self._status_loop(),
        )
    
    async def _sniper_loop(self) -> None:
        """Sniper monitoring loop."""
        self.logger.info("🎯 Sniper loop started")
        
        while self._running and not self._shutdown_event.is_set():
            try:
                for asset in self.feeds.keys():
                    consensus = self.consensus_engines.get(asset)
                    pm_feed = self.pm_feeds.get(asset)
                    
                    if not consensus or not pm_feed:
                        continue
                    
                    consensus_data = consensus.compute_consensus()
                    pm_data = pm_feed.get_data()
                    
                    if not consensus_data or not pm_data:
                        continue
                    
                    await self.sniper.check_opportunity(asset, consensus_data, pm_data)
                
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Sniper loop error", error=str(e))
                await asyncio.sleep(1)
    
    async def _arb_scanner_loop(self) -> None:
        """Arb scanner loop."""
        self.logger.info("🔍 Arb scanner loop started")
        
        while self._running and not self._shutdown_event.is_set():
            try:
                await self.arb_scanner._scan_for_opportunities()
                await asyncio.sleep(30)  # Scan every 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Arb scanner error", error=str(e))
                await asyncio.sleep(5)
    
    async def _status_loop(self) -> None:
        """Periodic status logging."""
        import time
        
        while self._running and not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(60)  # Status every 60 seconds
                
                runtime = time.time() - self._start_time
                hours = int(runtime // 3600)
                minutes = int((runtime % 3600) // 60)
                
                # Get prices
                prices = {}
                for asset, consensus in self.consensus_engines.items():
                    data = consensus.compute_consensus()
                    if data:
                        prices[asset] = f"${data.consensus_price:.2f}"
                
                sniper_stats = self.sniper.get_stats_summary()
                arb_stats = self.arb_scanner.get_stats_summary()
                
                self.logger.info(
                    "📊 STRATEGY STATUS",
                    runtime=f"{hours}h {minutes}m",
                    prices=prices,
                    sniper_spikes=sniper_stats.get("spikes_detected", 0),
                    sniper_positions=sniper_stats.get("positions_opened", 0),
                    arb_scans=arb_stats.get("scans_completed", 0),
                    arb_opportunities=arb_stats.get("opportunities_found", 0),
                )
                
            except asyncio.CancelledError:
                break
            except Exception:
                pass
    
    def _on_spike_detected(self, asset: str, magnitude: float, price: float) -> None:
        """Called when sniper detects a spike."""
        self.logger.info(
            "🌪️ SPIKE DETECTED",
            asset=asset,
            magnitude=f"{magnitude:+.2%}",
            price=f"${price:.2f}",
        )
    
    def _on_sniper_position(self, position) -> None:
        """Called when sniper opens a position."""
        self._sniper_opportunities += 1
        self.logger.info(
            "🎯 SNIPER OPPORTUNITY",
            position_id=position.position_id,
            asset=position.asset,
            discount=f"{position.discount_pct:.1%}",
            expected_profit=f"${position.total_cost_usd * position.discount_pct:.2f}",
            total_opportunities=self._sniper_opportunities,
        )
    
    def _on_arb_opportunity(self, opp) -> None:
        """Called when arb scanner finds an opportunity."""
        self._arb_opportunities += 1
        self.logger.info(
            "💰 ARB OPPORTUNITY",
            event=opp.event_description[:40],
            profit=f"{opp.profit_pct:.1%}",
            strategy=opp.best_strategy,
            pm_yes=f"${opp.pm_yes_price:.2f}",
            kalshi_yes=f"${opp.kalshi_yes_price:.2f}",
            total_opportunities=self._arb_opportunities,
        )
    
    async def stop(self) -> None:
        """Stop all strategies gracefully."""
        self._running = False
        self._shutdown_event.set()
        
        if self.sniper:
            await self.sniper.stop()
        
        if self.arb_scanner:
            await self.arb_scanner.stop()
        
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
        
        self.logger.info(
            "✅ All strategies stopped",
            sniper_opportunities=self._sniper_opportunities,
            arb_opportunities=self._arb_opportunities,
        )


async def main():
    """Main entry point."""
    runner = CombinedStrategyRunner()
    
    def signal_handler(sig, frame):
        logger.info("🛑 Shutdown requested...")
        asyncio.create_task(runner.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if not await runner.initialize():
        logger.error("❌ Failed to initialize")
        return 1
    
    try:
        await runner.start()
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()
    
    return 0


if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║           POLYBOT STRATEGY RUNNER (VIRTUAL MODE)             ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Running:                                                    ║
║    🎯 Volatility Sniper - Captures MM panic discounts        ║
║    🔍 Cross-Platform Arb - Kalshi ↔ Polymarket gaps          ║
║                                                              ║
║  Press Ctrl+C to stop                                        ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

