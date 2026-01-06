"""
Multi-Asset Manager - Manages feeds and signal detection for multiple assets.
"""

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

import structlog

from config.settings import settings
from src.feeds.binance import BinanceFeed
from src.feeds.coinbase import CoinbaseFeed
from src.feeds.kraken import KrakenFeed
from src.feeds.chainlink import ChainlinkFeed
from src.feeds.polymarket import PolymarketFeed
from src.engine.consensus import ConsensusEngine
from src.models.schemas import ExchangeTick, ConsensusData, OracleData, PolymarketData

logger = structlog.get_logger()


@dataclass
class AssetFeeds:
    """Container for all feeds related to a single asset."""
    asset: str
    binance: Optional[BinanceFeed] = None
    coinbase: Optional[CoinbaseFeed] = None
    kraken: Optional[KrakenFeed] = None
    chainlink: Optional[ChainlinkFeed] = None
    polymarket: Optional[PolymarketFeed] = None
    consensus_engine: Optional[ConsensusEngine] = None


class MultiAssetManager:
    """
    Manages feeds and data for multiple assets simultaneously.
    
    Allows the bot to monitor BTC, ETH, SOL, XRP etc. at the same time,
    increasing the number of trading opportunities.
    """
    
    def __init__(self):
        self.logger = logger.bind(component="multi_asset_manager")
        
        # Parse assets from settings
        self.assets: List[str] = [
            a.strip().upper() 
            for a in settings.assets.split(",") 
            if a.strip()
        ]
        
        # Asset feeds and engines
        self.asset_feeds: Dict[str, AssetFeeds] = {}
        
        # Tasks
        self._tasks: List[asyncio.Task] = []
        self._running = False
        
        self.logger.info("MultiAssetManager created", assets=self.assets)
    
    def _get_chainlink_address(self, asset: str) -> Optional[str]:
        """Get Chainlink feed address for an asset."""
        address_map = {
            "BTC": settings.chainlink.btc_usd_feed_address,
            "ETH": settings.chainlink.eth_usd_feed_address,
            "SOL": settings.chainlink.sol_usd_feed_address,
            "XRP": settings.chainlink.xrp_usd_feed_address,
        }
        addr = address_map.get(asset, "")
        return addr if addr else None
    
    async def initialize(self) -> None:
        """Initialize feeds for all configured assets."""
        symbols = settings.exchanges.symbols
        
        self.logger.info(f"Starting initialization for {len(self.assets)} assets: {self.assets}")
        self.logger.info(f"Available symbols config: {list(symbols.keys())}")
        
        for asset in self.assets:
            self.logger.info(f"Initializing feeds for {asset}")
            
            if asset not in symbols:
                self.logger.warning(f"No exchange symbols configured for {asset}, skipping")
                continue
            
            asset_symbols = symbols[asset]
            self.logger.info(f"{asset} symbols: {asset_symbols}")
            
            # Create feeds
            feeds = AssetFeeds(asset=asset)
            
            # Exchange feeds
            feeds.binance = BinanceFeed(
                symbol=asset_symbols["binance"],
                ws_url=settings.exchanges.binance_ws_url,
            )
            feeds.coinbase = CoinbaseFeed(
                product_id=asset_symbols["coinbase"],
                ws_url=settings.exchanges.coinbase_ws_url,
            )
            feeds.kraken = KrakenFeed(
                pair=asset_symbols["kraken"],
                ws_url=settings.exchanges.kraken_ws_url,
            )
            
            # Chainlink feed (if address available)
            chainlink_address = self._get_chainlink_address(asset)
            if chainlink_address and settings.chainlink.polygon_rpc_url:
                feeds.chainlink = ChainlinkFeed(
                    feed_address=chainlink_address,
                    rpc_url=settings.chainlink.polygon_rpc_url,
                    ws_url=settings.chainlink.polygon_ws_url,
                )
            else:
                self.logger.warning(f"No Chainlink feed for {asset}")
            
            # Polymarket feed with auto-discovery
            feeds.polymarket = PolymarketFeed(
                market_id=None,
                ws_url=settings.polymarket.ws_url,
                auto_discover=True,
                asset=asset,
            )
            
            # Consensus engine
            feeds.consensus_engine = ConsensusEngine()
            
            # Setup callbacks to update consensus
            def make_callback(feed_name: str, current_asset: str):
                def callback(tick: ExchangeTick):
                    af = self.asset_feeds.get(current_asset)
                    if af and af.consensus_engine:
                        feed = getattr(af, feed_name)
                        if feed:
                            metrics = feed.get_metrics()
                            af.consensus_engine.update_exchange(feed_name, metrics)
                return callback
            
            feeds.binance.add_callback(make_callback("binance", asset))
            feeds.coinbase.add_callback(make_callback("coinbase", asset))
            feeds.kraken.add_callback(make_callback("kraken", asset))
            
            self.asset_feeds[asset] = feeds
            self.logger.info(
                f"Feeds initialized for {asset}",
                has_binance=feeds.binance is not None,
                has_coinbase=feeds.coinbase is not None,
                has_kraken=feeds.kraken is not None,
                has_chainlink=feeds.chainlink is not None,
                has_polymarket=feeds.polymarket is not None,
            )
        
        # Discover Polymarket markets for all assets
        for asset, feeds in self.asset_feeds.items():
            if feeds.polymarket:
                self.logger.info(f"Discovering Polymarket market for {asset}...")
                try:
                    discovered = await feeds.polymarket._discover_market()
                    if discovered:
                        market_info = feeds.polymarket._discovered_market
                        self.logger.info(
                            f"Discovered {asset} market",
                            question=market_info.question[:50] if market_info else "N/A"
                        )
                    else:
                        self.logger.warning(f"Could not discover market for {asset}")
                except Exception as e:
                    self.logger.error(f"Error discovering {asset} market", error=str(e))
    
    async def start(self) -> None:
        """Start all feeds for all assets."""
        self._running = True
        
        for asset, feeds in self.asset_feeds.items():
            self.logger.info(f"Starting feeds for {asset}")
            
            # Start exchange feeds
            if feeds.binance:
                self._tasks.append(
                    asyncio.create_task(feeds.binance.start(), name=f"{asset}_binance")
                )
            if feeds.coinbase:
                self._tasks.append(
                    asyncio.create_task(feeds.coinbase.start(), name=f"{asset}_coinbase")
                )
            if feeds.kraken:
                self._tasks.append(
                    asyncio.create_task(feeds.kraken.start(), name=f"{asset}_kraken")
                )
            
            # Start Chainlink feed
            if feeds.chainlink:
                self._tasks.append(
                    asyncio.create_task(feeds.chainlink.start(), name=f"{asset}_chainlink")
                )
            
            # Start Polymarket feed
            if feeds.polymarket:
                self._tasks.append(
                    asyncio.create_task(feeds.polymarket.start(), name=f"{asset}_polymarket")
                )
        
        self.logger.info(f"Started {len(self._tasks)} feed tasks for {len(self.asset_feeds)} assets")
    
    async def stop(self) -> None:
        """Stop all feeds."""
        self._running = False
        self.logger.info("Stopping MultiAssetManager...")
        
        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        # Wait for cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Stop individual feeds
        for asset, feeds in self.asset_feeds.items():
            try:
                if feeds.binance:
                    await feeds.binance.stop()
                if feeds.coinbase:
                    await feeds.coinbase.stop()
                if feeds.kraken:
                    await feeds.kraken.stop()
                if feeds.chainlink:
                    await feeds.chainlink.stop()
                if feeds.polymarket:
                    await feeds.polymarket.stop()
            except Exception as e:
                self.logger.error(f"Error stopping {asset} feeds", error=str(e))
        
        self._tasks.clear()
        self.logger.info("MultiAssetManager stopped")
    
    def get_consensus(self, asset: str) -> Optional[ConsensusData]:
        """Get consensus data for an asset."""
        feeds = self.asset_feeds.get(asset)
        if feeds and feeds.consensus_engine:
            return feeds.consensus_engine.compute_consensus()
        return None
    
    def get_oracle_data(self, asset: str) -> Optional[OracleData]:
        """Get oracle data for an asset."""
        feeds = self.asset_feeds.get(asset)
        if feeds and feeds.chainlink:
            return feeds.chainlink.get_data()
        return None
    
    def get_polymarket_data(self, asset: str) -> Optional[PolymarketData]:
        """Get Polymarket data for an asset."""
        feeds = self.asset_feeds.get(asset)
        if feeds and feeds.polymarket:
            return feeds.polymarket.get_data()
        return None
    
    def get_all_data(self) -> Dict[str, Dict[str, Any]]:
        """Get all data for all assets."""
        result = {}
        for asset in self.assets:
            result[asset] = {
                "consensus": self.get_consensus(asset),
                "oracle": self.get_oracle_data(asset),
                "polymarket": self.get_polymarket_data(asset),
            }
        return result
    
    def get_status(self) -> Dict[str, Dict[str, bool]]:
        """Get connection status for all feeds."""
        status = {}
        for asset, feeds in self.asset_feeds.items():
            status[asset] = {
                "binance": feeds.binance.health.connected if feeds.binance else False,
                "coinbase": feeds.coinbase.health.connected if feeds.coinbase else False,
                "kraken": feeds.kraken.health.connected if feeds.kraken else False,
                "chainlink": feeds.chainlink.connected if feeds.chainlink else False,
                "polymarket": feeds.polymarket.health.connected if feeds.polymarket else False,
            }
        return status

