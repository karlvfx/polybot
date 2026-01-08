"""
Polymarket Oracle-Lag Trading Bot - Main Application

This bot exploits the delay between real-time crypto spot prices
and Chainlink oracle updates on Polymarket's 15-minute up/down markets.
"""

import asyncio
import signal
import sys
import time
from typing import Optional

import structlog

# Performance optimization: Use uvloop for 2-4x faster asyncio
# NOTE: Disabled - was causing core dumps on some VPS systems
# To re-enable: uncomment the try/except block below
# try:
#     import uvloop
#     uvloop.install()
#     print("✅ uvloop installed - 2-4x faster async performance")
# except ImportError:
#     print("⚠️ uvloop not available - using standard asyncio")
print("ℹ️ Using standard asyncio")

from config.settings import settings, OperatingMode
from src.feeds.binance import BinanceFeed
from src.feeds.coinbase import CoinbaseFeed
from src.feeds.kraken import KrakenFeed
from src.feeds.chainlink import ChainlinkFeed
from src.feeds.polymarket import PolymarketFeed
from src.engine.consensus import ConsensusEngine
from src.engine.signal_detector import SignalDetector
from src.engine.validator import Validator
from src.engine.confidence import ConfidenceScorer
from src.engine.execution import ExecutionEngine
from src.engine.multi_asset import MultiAssetManager
from src.modes.shadow import ShadowMode
from src.modes.alert import AlertMode
from src.modes.night_auto import NightAutoMode
from src.utils.logging import setup_logging, SignalLogger, MetricsLogger, PerformanceTracker
from src.utils.alerts import DiscordAlerter
from src.utils.time_filter import TimeOfDayAnalyzer
from src.utils.session_tracker import session_tracker
from src.models.schemas import ExchangeTick, SignalCandidate

logger = structlog.get_logger()


class TradingBot:
    """
    Main trading bot orchestrator.
    
    Coordinates:
    - Multi-exchange spot feeds
    - Chainlink oracle monitoring
    - Polymarket orderbook monitoring
    - Signal detection and validation
    - Trade execution
    - Logging and alerting
    """
    
    def __init__(self):
        self.logger = logger.bind(component="bot")
        
        # Initialize logging
        setup_logging(settings.log_level)
        self.signal_logger = SignalLogger()
        self.metrics_logger = MetricsLogger()
        self.performance = PerformanceTracker()
        
        # Parse configured assets
        self.assets = [a.strip().upper() for a in settings.assets.split(",") if a.strip()]
        self.logger.info("Configured assets", assets=self.assets)
        
        # Multi-asset manager (handles feeds for all assets)
        self.multi_asset: Optional[MultiAssetManager] = None
        
        # Legacy single-asset feeds (for backwards compatibility)
        self.binance_feed = BinanceFeed(
            symbol=settings.exchanges.binance_symbol,
            ws_url=settings.exchanges.binance_ws_url,
        )
        self.coinbase_feed = CoinbaseFeed(
            product_id=settings.exchanges.coinbase_product_id,
            ws_url=settings.exchanges.coinbase_ws_url,
        )
        self.kraken_feed = KrakenFeed(
            pair=settings.exchanges.kraken_pair,
            ws_url=settings.exchanges.kraken_ws_url,
        )
        
        self.chainlink_feed: Optional[ChainlinkFeed] = None
        self.polymarket_feed: Optional[PolymarketFeed] = None
        
        # Initialize time-of-day analyzer (loads historical win rates)
        self.time_analyzer = TimeOfDayAnalyzer(log_dir="logs")
        self.time_analyzer.load_from_logs()
        
        # Initialize engine components
        self.consensus_engine = ConsensusEngine()
        self.signal_detector = SignalDetector()
        self.validator = Validator()
        
        # Confidence scorer with time-of-day integration
        self.confidence_scorer = ConfidenceScorer(time_analyzer=self.time_analyzer)
        
        # Execution engine (initialized later with credentials)
        self.execution_engine: Optional[ExecutionEngine] = None
        
        # Operating mode
        self.mode: Optional[ShadowMode | AlertMode | NightAutoMode] = None
        
        # Discord alerter
        self.alerter: Optional[DiscordAlerter] = None
        if settings.alerts.discord_webhook_url:
            self.alerter = DiscordAlerter(settings.alerts.discord_webhook_url)
        
        # Control flags
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # Metrics
        self._last_signal_check_ms = 0
        self._signal_check_interval_ms = 500  # Check every 500ms
    
    def _setup_exchange_callbacks(self) -> None:
        """Register callbacks for exchange data updates."""
        def on_binance_tick(tick: ExchangeTick):
            metrics = self.binance_feed.get_metrics()
            self.consensus_engine.update_exchange("binance", metrics)
        
        def on_coinbase_tick(tick: ExchangeTick):
            metrics = self.coinbase_feed.get_metrics()
            self.consensus_engine.update_exchange("coinbase", metrics)
        
        def on_kraken_tick(tick: ExchangeTick):
            metrics = self.kraken_feed.get_metrics()
            self.consensus_engine.update_exchange("kraken", metrics)
        
        self.binance_feed.add_callback(on_binance_tick)
        self.coinbase_feed.add_callback(on_coinbase_tick)
        self.kraken_feed.add_callback(on_kraken_tick)
    
    async def _initialize_chainlink(self) -> bool:
        """Initialize Chainlink feed."""
        if not settings.chainlink.polygon_rpc_url:
            self.logger.warning("No Polygon RPC URL configured - Chainlink feed disabled")
            return False
        
        self.chainlink_feed = ChainlinkFeed(
            feed_address=settings.chainlink.btc_usd_feed_address,
            rpc_url=settings.chainlink.polygon_rpc_url,
            ws_url=settings.chainlink.polygon_ws_url,
        )
        return True
    
    async def _initialize_polymarket(self) -> bool:
        """Initialize Polymarket feed with auto-discovery."""
        # Create feed with auto-discovery enabled
        self.polymarket_feed = PolymarketFeed(
            market_id=None,  # Always auto-discover
            ws_url=settings.polymarket.ws_url,
            auto_discover=True,
        )
        
        self.logger.info("Discovering BTC 15-minute market...")
        discovered = await self.polymarket_feed._discover_market()
        
        if discovered and self.polymarket_feed._discovered_market:
            market_info = self.polymarket_feed._discovered_market
            
            # Notify Discord about discovered market
            if self.alerter:
                await self.alerter.send_message(
                    f"🔍 **Market Discovered**\n"
                    f"**Question:** {market_info.question[:100]}\n"
                    f"**Type:** {market_info.outcome.upper()}\n"
                    f"**ID:** `{market_info.condition_id[:40]}...`\n"
                    f"_Market will auto-refresh every 15 minutes_"
                )
            return True
        else:
            self.logger.error("Could not discover BTC 15-minute market")
            if self.alerter:
                await self.alerter.send_message(
                    "❌ **Market Discovery Failed**\n"
                    "Could not find BTC 15-minute market.\n"
                    "Will retry on next startup."
                )
            return False
    
    async def _initialize_execution(self) -> bool:
        """Initialize execution engine with feed references for pre-trade checks."""
        if not settings.wallet_address or not settings.private_key:
            self.logger.warning("No wallet configured - execution disabled")
            return False
        
        if not settings.chainlink.polygon_rpc_url:
            self.logger.warning("No RPC URL - execution disabled")
            return False
        
        self.execution_engine = ExecutionEngine(
            rpc_url=settings.chainlink.polygon_rpc_url,
            wallet_address=settings.wallet_address,
            private_key=settings.private_key,
            polymarket_feed=self.polymarket_feed,  # For pre-trade slippage simulation
            chainlink_feed=self.chainlink_feed,    # For adaptive exit oracle tracking
        )
        
        return await self.execution_engine.initialize()
    
    def _initialize_mode(self) -> None:
        """Initialize operating mode based on settings."""
        if settings.mode == OperatingMode.SHADOW:
            self.mode = ShadowMode()
            self.logger.info("Initialized SHADOW mode")
        
        elif settings.mode == OperatingMode.ALERT:
            # Initialize AlertMode with feed references for virtual trading
            self.logger.info(
                "Initializing ALERT mode",
                has_polymarket_feed=self.polymarket_feed is not None,
                has_chainlink_feed=self.chainlink_feed is not None,
            )
            self.mode = AlertMode(
                discord_webhook_url=settings.alerts.discord_webhook_url,
                polymarket_feed=self.polymarket_feed,
                chainlink_feed=self.chainlink_feed,
            )
            self.logger.info(
                "ALERT mode initialized",
                virtual_trader_active=self.mode._virtual_trader is not None,
            )
        
        elif settings.mode == OperatingMode.NIGHT_AUTO:
            if not self.execution_engine:
                self.logger.error("Night auto mode requires execution engine")
                self.mode = ShadowMode()  # Fallback to shadow
            else:
                self.mode = NightAutoMode(
                    self.execution_engine,
                    settings.alerts.discord_webhook_url,
                )
                self.logger.info("Initialized NIGHT_AUTO mode")
        
        self.mode.activate()
    
    async def _check_signals(self) -> None:
        """Check for trading signals across all assets IN PARALLEL."""
        # Rate limit signal checking
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_signal_check_ms < self._signal_check_interval_ms:
            return
        self._last_signal_check_ms = now_ms
        
        # Check signals for ALL assets simultaneously (parallel, not sequential)
        if self.multi_asset:
            # Use asyncio.gather to check all assets at the same time
            # This ensures we don't miss opportunities while checking other assets
            await asyncio.gather(
                *[self._check_signals_for_asset(asset) for asset in self.assets],
                return_exceptions=True  # Don't let one asset's error block others
            )
        else:
            # Legacy single-asset mode
            await self._check_signals_for_asset("BTC")
    
    async def _check_signals_for_asset(self, asset: str) -> None:
        """Check for trading signals for a specific asset."""
        now_ms = int(time.time() * 1000)
        
        # Set current asset context for session tracking
        self.signal_detector.set_asset(asset)
        
        # Get data for this asset
        if self.multi_asset and asset in self.multi_asset.asset_feeds:
            feeds = self.multi_asset.asset_feeds[asset]
            consensus = feeds.consensus_engine.compute_consensus() if feeds.consensus_engine else None
            oracle = feeds.chainlink.get_data() if feeds.chainlink else None
            pm_data = feeds.polymarket.get_data() if feeds.polymarket else None
        else:
            # Legacy single-asset mode
            consensus = self.consensus_engine.compute_consensus()
            oracle = self.chainlink_feed.get_data() if self.chainlink_feed else None
            pm_data = self.polymarket_feed.get_data() if self.polymarket_feed else None
        
        if not consensus:
            return
        
        # Oracle is optional for some assets (like XRP)
        # But we still need Polymarket data
        if not pm_data:
            return
        
        # STALE DATA FILTER: Skip if we haven't received PM data recently
        # Note: orderbook_age_seconds = time since PRICES changed (can be long during quiet periods)
        # We check timestamp_ms to see when we last got ANY data
        MAX_DATA_AGE_SECONDS = 120  # 2 minutes - if no data for 2 min, connection issue
        now_ms = int(time.time() * 1000)
        data_age_seconds = (now_ms - pm_data.timestamp_ms) / 1000.0
        
        if data_age_seconds > MAX_DATA_AGE_SECONDS:
            self.logger.warning(
                "⚠️ Skipping signal check - No PM data received recently",
                asset=asset,
                data_age=f"{data_age_seconds:.0f}s",
                max_age=f"{MAX_DATA_AGE_SECONDS}s",
                price_age=f"{pm_data.orderbook_age_seconds:.0f}s",
            )
            return
        
        # Price staleness is now handled in signal_detector (max_pm_staleness_seconds)
        
        # Periodic status log (every 30 seconds per asset)
        if not hasattr(self, '_last_status_log_ms_by_asset'):
            self._last_status_log_ms_by_asset = {}
        last_log = self._last_status_log_ms_by_asset.get(asset, 0)
        if now_ms - last_log > 30000:
            self._last_status_log_ms_by_asset[asset] = now_ms
            oracle_info = f"${oracle.current_value:.2f} (age: {oracle.oracle_age_seconds:.1f}s)" if oracle else "N/A"
            self.logger.info(
                "Signal check status",
                asset=asset,
                consensus_price=f"${consensus.consensus_price:.2f}",
                move_30s=f"{consensus.move_30s_pct:.3f}%",
                oracle=oracle_info,
                pm_yes_bid=f"{pm_data.yes_bid:.3f}",
                pm_spread=f"{pm_data.spread:.3f}",
            )
        
        # NOTE: Oracle is optional for divergence strategy (spot-PM divergence is primary signal)
        # We'll still pass it if available for logging/metrics
        
        # Detect signal (pass asset for asset-specific thresholds)
        signal = self.signal_detector.detect(consensus, oracle, pm_data, asset=asset)
        if not signal:
            return
        
        # Trigger high activity mode on PM feed for faster polling
        if self.multi_asset and asset in self.multi_asset.asset_feeds:
            pm_feed = self.multi_asset.asset_feeds[asset].polymarket
            if pm_feed:
                pm_feed.trigger_high_activity_mode(duration_seconds=30.0)
        elif self.polymarket_feed:
            self.polymarket_feed.trigger_high_activity_mode(duration_seconds=30.0)
        
        # Tag signal with asset
        signal.market_id = f"{asset}_{signal.market_id}"
        
        # Validate signal
        validation = self.validator.validate(signal)
        signal.validation = validation
        
        if not validation.passed:
            # Log rejection
            self.signal_logger.log_rejection(
                timestamp_ms=signal.timestamp_ms,
                reason=validation.rejection_reason.value if validation.rejection_reason else "unknown",
                details={
                    "signal_id": signal.signal_id,
                    "direction": signal.direction.value,
                    "move_pct": consensus.move_30s_pct,
                    "oracle_age": oracle.oracle_age_seconds,
                },
            )
            return
        
        # Score signal (pass asset for asset-specific scoring)
        scoring = self.confidence_scorer.score(signal, asset=asset)
        signal.scoring = scoring
        signal.is_valid = True
        
        # Record signal
        self.performance.record_signal(
            signal_type=signal.signal_type.value,
            direction=signal.direction.value,
        )
        
        # Process based on mode
        if self.mode and self.mode.should_process(signal):
            action, outcome = await self.mode.process_signal(signal, asset=asset)
            
            # Log complete signal
            log_entry = signal.to_log()
            log_entry.action.mode = action.mode
            log_entry.action.decision = action.decision.value
            log_entry.action.position_size_eur = action.position_size_eur
            log_entry.action.entry_price = action.entry_price
            log_entry.action.gas_cost_eur = action.gas_cost_eur
            
            if outcome:
                log_entry.outcome.filled = outcome.filled
                log_entry.outcome.exit_price = outcome.exit_price
                log_entry.outcome.net_profit_eur = outcome.net_profit_eur
                
                # Update time-of-day analyzer with outcome for continuous learning
                self.time_analyzer.add_signal_result(
                    timestamp_ms=signal.timestamp_ms,
                    won=outcome.net_profit_eur > 0,
                    profit_eur=outcome.net_profit_eur,
                )
            
            self.signal_logger.log_signal(log_entry)
            
            self.logger.info(
                "Signal processed",
                signal_id=signal.signal_id,
                confidence=scoring.confidence,
                action=action.decision.value,
            )
    
    async def _feed_health_monitor(self) -> None:
        """Monitor feed health and log metrics."""
        # Wait for feeds to connect before first status report
        self.logger.info("Health monitor waiting for feeds to connect...")
        try:
            await asyncio.sleep(10)  # Give feeds 10 seconds to connect
        except asyncio.CancelledError:
            self.logger.info("Health monitor cancelled during startup wait")
            return
        
        # Send first status immediately after wait
        last_market_status_time = 0  # Force immediate first report
        market_status_interval = 60  # Then report every 60 seconds
        
        while self._running:
            try:
                # Get feed health status - use multi-asset feeds if available
                if self.multi_asset and self.assets:
                    # Use first asset's exchange feeds for status
                    primary_asset = self.assets[0]
                    asset_feeds = self.multi_asset.asset_feeds.get(primary_asset)
                    if asset_feeds:
                        exchange_feeds = {
                            "binance": asset_feeds.binance,
                            "coinbase": asset_feeds.coinbase,
                            "kraken": asset_feeds.kraken,
                        }
                    else:
                        exchange_feeds = {}
                else:
                    # Legacy single-asset mode
                    exchange_feeds = {
                        "binance": self.binance_feed,
                        "coinbase": self.coinbase_feed,
                        "kraken": self.kraken_feed,
                    }
                
                # Build metrics dict for logging
                feeds_serializable = {}
                for name, feed in exchange_feeds.items():
                    if feed:
                        # Check for geo-blocked feeds
                        if hasattr(feed, 'is_disabled') and feed.is_disabled:
                            feeds_serializable[name] = {
                                "connected": False,
                                "price": 0,
                                "is_stale": False,  # Not stale, just unavailable
                                "geo_blocked": True,
                                "error_count": feed.health.error_count,
                            }
                            continue
                        
                        metrics = feed.get_metrics()
                        feeds_serializable[name] = {
                            "connected": feed.health.connected,
                            "price": metrics.current_price if hasattr(metrics, 'current_price') else 0,
                            "is_stale": feed.health.is_stale,
                            "error_count": feed.health.error_count,
                        }
                    else:
                        feeds_serializable[name] = {"connected": False, "price": 0, "is_stale": True}
                
                if self.chainlink_feed:
                    cl_metrics = self.chainlink_feed.get_metrics()
                    feeds_serializable["chainlink"] = cl_metrics if isinstance(cl_metrics, dict) else {
                        "connected": self.chainlink_feed.connected,
                        "price": cl_metrics.get("price", 0) if isinstance(cl_metrics, dict) else 0,
                        "oracle_age_seconds": cl_metrics.get("oracle_age_seconds", 0) if isinstance(cl_metrics, dict) else 0,
                    }
                
                if self.polymarket_feed:
                    pm_metrics = self.polymarket_feed.get_metrics()
                    feeds_serializable["polymarket"] = pm_metrics if isinstance(pm_metrics, dict) else {}
                
                self.metrics_logger.log_feed_health(feeds_serializable)
                
                # Check for stale feeds
                stale_feeds = [name for name, m in feeds_serializable.items() 
                              if m.get("is_stale", False)]
                
                if stale_feeds:
                    self.logger.warning("Stale feeds detected", feeds=stale_feeds)
                
                # Periodic market status update to Discord
                now = int(time.time())
                if self.alerter and now - last_market_status_time >= market_status_interval:
                    last_market_status_time = now
                    
                    # Build status message from feed health
                    exchange_status = []
                    for name, feed in exchange_feeds.items():
                        if feed:
                            # Check for geo-blocking first
                            if hasattr(feed, 'is_disabled') and feed.is_disabled:
                                exchange_status.append(f"{name.capitalize()}: 🚫 Geo-blocked")
                                continue
                            
                            metrics = feed.get_metrics()
                            connected = "✅" if feed.health.connected else "❌"
                            price = metrics.current_price if hasattr(metrics, 'current_price') else 0
                            exchange_status.append(f"{name.capitalize()}: {connected} ${price:,.2f}")
                        else:
                            exchange_status.append(f"{name.capitalize()}: ❌ Not initialized")
                    
                    # Build multi-asset status
                    asset_status_lines = []
                    if self.multi_asset:
                        for asset in self.assets:
                            asset_feeds = self.multi_asset.asset_feeds.get(asset)
                            if asset_feeds:
                                # Get consensus price from exchanges
                                consensus = asset_feeds.consensus_engine.compute_consensus() if asset_feeds.consensus_engine else None
                                if consensus and consensus.consensus_price > 0:
                                    price = f"${consensus.consensus_price:,.2f}"
                                    price_emoji = "✅"
                                else:
                                    price = "connecting..."
                                    price_emoji = "⏳"
                                
                                # Get PM status with divergence metrics
                                pm_info = "N/A"
                                divergence_info = ""
                                if asset_feeds.polymarket:
                                    pm_data = asset_feeds.polymarket.get_data()
                                    discovered = asset_feeds.polymarket._discovered_market
                                    if pm_data and pm_data.yes_bid > 0:
                                        pm_info = f"YES:{pm_data.yes_bid:.2f} NO:{pm_data.no_bid:.2f}"
                                        
                                        # Calculate divergence (the actual edge signal)
                                        if consensus and consensus.move_30s_pct != 0:
                                            import math
                                            spot_implied = 1 / (1 + math.exp(-consensus.move_30s_pct * 100))
                                            divergence = abs(spot_implied - pm_data.yes_bid)
                                            pm_age = pm_data.orderbook_age_seconds
                                            
                                            # Show divergence status
                                            if divergence >= 0.08 and pm_age >= 8:
                                                divergence_info = f" 🎯 DIV:{divergence:.0%} AGE:{pm_age:.0f}s"
                                            elif pm_age >= 8:
                                                divergence_info = f" ⏳ AGE:{pm_age:.0f}s"
                                            else:
                                                divergence_info = f" ({pm_age:.0f}s)"
                                        else:
                                            pm_age = pm_data.orderbook_age_seconds
                                            divergence_info = f" ({pm_age:.0f}s)"
                                    elif discovered:
                                        pm_info = f"Market found (loading...)"
                                    else:
                                        pm_info = "No market"
                                
                                # Build status line (no Oracle - we use divergence now)
                                asset_status_lines.append(f"  **{asset}**: {price_emoji} {price}")
                                asset_status_lines.append(f"      PM: {pm_info}{divergence_info}")
                            else:
                                asset_status_lines.append(f"  **{asset}**: ❌ Not initialized")
                    
                    # Fallback to single-asset status
                    pm_status = "Not initialized"
                    if self.polymarket_feed:
                        pm_metrics = self.polymarket_feed.get_metrics()
                        if self.polymarket_feed.health.connected:
                            has_data = pm_metrics.get("has_orderbook_data", False) if isinstance(pm_metrics, dict) else False
                            yes_bid = pm_metrics.get("yes_bid", 0) if isinstance(pm_metrics, dict) else 0
                            pm_status = f"✅ Yes bid: {yes_bid:.2f}" if has_data else "⚠️ Connected, no data"
                        else:
                            pm_status = "❌ Disconnected"
                    
                    oracle_status = "Not initialized"
                    if self.chainlink_feed:
                        cl_metrics = self.chainlink_feed.get_metrics()
                        if self.chainlink_feed.connected:
                            oracle_price = cl_metrics.get("current_price", 0) if isinstance(cl_metrics, dict) else 0
                            oracle_age = cl_metrics.get("oracle_age_seconds", 0) if isinstance(cl_metrics, dict) else 0
                            oracle_status = f"✅ ${oracle_price:,.2f} (age: {oracle_age:.0f}s)"
                        else:
                            oracle_status = "❌ Disconnected"
                    
                    # Get mode stats including virtual trading
                    mode_status = ""
                    if self.mode:
                        mode_metrics = self.mode.get_metrics()
                        if isinstance(self.mode, AlertMode):
                            alerts_sent = mode_metrics.get("alerts_sent", 0)
                            virtual_stats = mode_metrics.get("virtual_trading", {})
                            if virtual_stats:
                                vt_trades = virtual_stats.get("total_trades", 0)
                                vt_wr = virtual_stats.get("win_rate", 0)
                                vt_pnl = virtual_stats.get("total_pnl", 0)
                                vt_open = virtual_stats.get("open_positions", 0)
                                mode_status = (
                                    f"\n**Alert Mode:**\n"
                                    f"  Alerts: {alerts_sent} | Virtual: {vt_trades} trades\n"
                                    f"  WR: {vt_wr:.0%} | P/L: €{vt_pnl:+.2f} | Open: {vt_open}"
                                )
                            else:
                                mode_status = f"\n**Alert Mode:** {alerts_sent} alerts sent"
                        elif isinstance(self.mode, ShadowMode):
                            wr = mode_metrics.get("win_rate", 0)
                            wins = mode_metrics.get("would_be_wins", 0)
                            losses = mode_metrics.get("would_be_losses", 0)
                            mode_status = f"\n**Shadow Mode:** {wins}W/{losses}L ({wr:.0%} WR)"
                    
                    # Get signal detector stats
                    signal_stats = ""
                    if hasattr(self, 'performance'):
                        summary = self.performance.get_summary()
                        total_signals = summary.get("signals", {}).get("total", 0)
                        signal_stats = f"\n**Signals:** {total_signals} detected this session"
                    
                    # Build final status message (NON-BLOCKING - don't let Discord slow down bot)
                    if asset_status_lines:
                        # Multi-asset format
                        asyncio.create_task(self.alerter.send_message(
                            f"📊 **Status Update**\n"
                            f"**Assets:**\n" + "\n".join(asset_status_lines) +
                            f"{mode_status}"
                            f"{signal_stats}"
                        ))
                    else:
                        # Legacy single-asset format
                        asyncio.create_task(self.alerter.send_message(
                            f"📊 **Status Update**\n"
                            f"**Exchanges:**\n" + "\n".join(f"  {s}" for s in exchange_status) + "\n"
                            f"**Polymarket:** {pm_status}\n"
                            f"**Oracle:** {oracle_status}"
                            f"{mode_status}"
                            f"{signal_stats}"
                        ))
                
                await asyncio.sleep(10)  # Check health every 10 seconds
                
            except asyncio.CancelledError:
                self.logger.info("Health monitor cancelled")
                break
            except Exception as e:
                self.logger.error("Health monitor error", error=str(e))
                await asyncio.sleep(10)
    
    async def _signal_loop(self) -> None:
        """Main signal detection loop."""
        while self._running:
            try:
                await self._check_signals()
                await asyncio.sleep(0.5)  # 500ms tick (matches signal check interval)
            except asyncio.CancelledError:
                self.logger.info("Signal loop cancelled")
                break
            except Exception as e:
                self.logger.error("Signal loop error", error=str(e))
                await asyncio.sleep(1)
    
    async def start(self) -> None:
        """Start the trading bot."""
        self.logger.info(
            "Starting Polymarket Oracle-Lag Trading Bot",
            mode=settings.mode.value,
            assets=self.assets,
        )
        
        self._running = True
        
        # Always use multi-asset manager (even for single BTC)
        self.logger.info("Initializing multi-asset manager", assets=self.assets)
        self.multi_asset = MultiAssetManager()
        await self.multi_asset.initialize()
        
        # Use first asset's feeds as primary for mode initialization
        primary_asset = self.assets[0] if self.assets else "BTC"
        self.logger.info(f"Setting primary asset feeds: {primary_asset}")
        
        if primary_asset in self.multi_asset.asset_feeds:
            feeds = self.multi_asset.asset_feeds[primary_asset]
            self.polymarket_feed = feeds.polymarket
            self.chainlink_feed = feeds.chainlink
            self.consensus_engine = feeds.consensus_engine
            self.logger.info(
                "Primary feeds assigned",
                has_polymarket=self.polymarket_feed is not None,
                has_chainlink=self.chainlink_feed is not None,
                has_consensus=self.consensus_engine is not None,
            )
        else:
            self.logger.error(f"Primary asset {primary_asset} not found in multi_asset.asset_feeds")
            self.logger.info(f"Available assets: {list(self.multi_asset.asset_feeds.keys())}")
        
        # Initialize execution engine
        await self._initialize_execution()
        
        # Initialize mode
        self._initialize_mode()
        
        # Send startup notification
        if self.alerter:
            assets_str = ", ".join(self.assets)
            mode_name = settings.mode.value.upper()
            
            # Build mode-specific status
            if isinstance(self.mode, AlertMode):
                virtual_status = "✅ Enabled" if self.mode._virtual_trader else "❌ Disabled"
                mode_info = (
                    f"**Virtual Trading:** {virtual_status}\n"
                    f"**Alert Threshold:** {settings.alerts.alert_confidence_threshold:.0%} confidence"
                )
            elif isinstance(self.mode, ShadowMode):
                mode_info = "**Virtual Trading:** Simulates ALL trades (no threshold)"
            else:
                mode_info = f"**Mode:** {mode_name}"
            
            await self.alerter.send_message(
                f"🚀 **Bot Started**\n"
                f"**Mode:** {mode_name}\n"
                f"**Assets:** {assets_str}\n"
                f"{mode_info}"
            )
        
        # Start all tasks
        self._tasks = []
        
        # Start multi-asset feeds if enabled
        if self.multi_asset:
            await self.multi_asset.start()
            self._tasks.extend(self.multi_asset._tasks)
        else:
            # Legacy single-asset mode
            self._tasks.extend([
                asyncio.create_task(self.binance_feed.start(), name="binance_feed"),
                asyncio.create_task(self.coinbase_feed.start(), name="coinbase_feed"),
                asyncio.create_task(self.kraken_feed.start(), name="kraken_feed"),
            ])
            
            if self.chainlink_feed:
                self._tasks.append(asyncio.create_task(self.chainlink_feed.start(), name="chainlink_feed"))
            
            if self.polymarket_feed:
                self._tasks.append(asyncio.create_task(self.polymarket_feed.start(), name="polymarket_feed"))
        
        # Add health monitor and signal loop for all modes
        self._tasks.append(asyncio.create_task(self._feed_health_monitor(), name="health_monitor"))
        self._tasks.append(asyncio.create_task(self._signal_loop(), name="signal_loop"))
        
        self.logger.info("All feeds started", task_count=len(self._tasks))
        
        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except Exception as e:
            self.logger.error("Error waiting for shutdown", error=str(e))
        
        # Cancel all tasks gracefully
        self.logger.info("Cancelling all tasks...")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        # Wait for tasks to finish cancelling with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=5.0  # 5 second timeout for cleanup
            )
        except asyncio.TimeoutError:
            self.logger.warning("Some tasks didn't finish in time, forcing shutdown")
        except Exception as e:
            self.logger.error("Error cancelling tasks", error=str(e))
        
        # Call stop to generate reports (this is the main shutdown)
        await self.stop()
    
    async def stop(self) -> None:
        """Stop the trading bot."""
        self.logger.info("Stopping bot...")
        self._running = False
        
        # Stop multi-asset manager if enabled
        if self.multi_asset:
            try:
                await self.multi_asset.stop()
            except Exception as e:
                self.logger.error("Error stopping multi-asset manager", error=str(e))
        else:
            # Stop legacy single-asset feeds
            try:
                await self.binance_feed.stop()
            except Exception as e:
                self.logger.error("Error stopping binance feed", error=str(e))
            
            try:
                await self.coinbase_feed.stop()
            except Exception as e:
                self.logger.error("Error stopping coinbase feed", error=str(e))
            
            try:
                await self.kraken_feed.stop()
            except Exception as e:
                self.logger.error("Error stopping kraken feed", error=str(e))
            
            if self.chainlink_feed:
                try:
                    await self.chainlink_feed.stop()
                except Exception as e:
                    self.logger.error("Error stopping chainlink feed", error=str(e))
            
            if self.polymarket_feed:
                try:
                    await self.polymarket_feed.stop()
                except Exception as e:
                    self.logger.error("Error stopping polymarket feed", error=str(e))
        
        # Deactivate mode (may be async for AlertMode)
        if self.mode:
            try:
                if hasattr(self.mode, 'deactivate'):
                    result = self.mode.deactivate()
                    # If deactivate returns a coroutine, await it
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                self.logger.error("Error deactivating mode", error=str(e))
        
        # Close Discord alerter
        if self.alerter:
            try:
                await self.alerter.close()
            except Exception as e:
                self.logger.error("Error closing alerter", error=str(e))
        
        # Close loggers
        try:
            self.signal_logger.close()
        except Exception as e:
            self.logger.error("Error closing signal logger", error=str(e))
        
        # Print performance report to stdout (ensures it shows in terminal)
        print("\n" + "="*70)
        print("SHUTDOWN REPORT")
        print("="*70)
        
        try:
            if isinstance(self.mode, ShadowMode):
                print(self.mode.generate_report())
        except Exception as e:
            print(f"Error generating shadow mode report: {e}")
        
        try:
            self.performance.print_report()
        except Exception as e:
            print(f"Error generating performance report: {e}")
        
        # Print time-of-day analysis report
        try:
            print(self.time_analyzer.generate_report())
        except Exception as e:
            print(f"Error generating time analysis report: {e}")
        
        # Generate and print session summary
        try:
            session_summary = session_tracker.generate_summary()
            print("\n" + "="*60)
            print("SESSION SUMMARY")
            print("="*60)
            print(f"Duration: {session_summary['session']['duration_human']}")
            print(f"Signals Detected: {session_summary['signals']['detected']}")
            print(f"Signals Rejected: {session_summary['signals']['rejected']}")
            
            if session_summary['trades']['total'] > 0:
                print(f"\nVirtual Trades: {session_summary['trades']['total']}")
                print(f"  Win Rate: {session_summary['trades']['win_rate']*100:.1f}%")
                print(f"  Gross P&L: €{session_summary['pnl']['gross']:.2f}")
                print(f"  Fees Paid: €{session_summary['pnl']['fees']:.3f}")
                print(f"  Net P&L: €{session_summary['pnl']['net']:.2f}")
            
            if session_summary['signals']['rejection_breakdown']:
                print("\nTop Rejection Reasons:")
                sorted_rejections = sorted(
                    session_summary['signals']['rejection_breakdown'].items(),
                    key=lambda x: x[1], reverse=True
                )[:5]
                for reason, count in sorted_rejections:
                    print(f"  • {reason}: {count}")
            
            if session_summary['missed_opportunities']['count'] > 0:
                print(f"\nMissed High-Divergence Opportunities: {session_summary['missed_opportunities']['count']}")
                print(f"  Max Divergence Seen: {session_summary['missed_opportunities']['max_divergence_seen']:.1%}")
            
            print("\nConnection Health:")
            for feed, stats in session_summary['connections'].items():
                print(f"  • {feed}: {stats['uptime_pct']:.1f}% uptime, {stats['reconnects']} reconnects")
            
            if session_summary['trade_details']:
                winning_trades = [t for t in session_summary['trade_details'] if t['result'] == '✅']
                losing_trades = [t for t in session_summary['trade_details'] if t['result'] == '❌']
                
                # Show winning trades summary
                if winning_trades:
                    print(f"\n✅ Winning Trades: {len(winning_trades)}")
                    for trade in winning_trades[-5:]:  # Last 5 wins
                        print(f"  {trade['asset']} {trade['direction']}: "
                              f"{trade['entry']}→{trade['exit']} | {trade['net']} ({trade['exit_reason']})")
                
                # Show losing trades with analysis
                if losing_trades:
                    print(f"\n❌ Losing Trades Analysis: {len(losing_trades)} total")
                    
                    # Group losses by asset
                    losses_by_asset = {}
                    for trade in losing_trades:
                        asset = trade['asset']
                        if asset not in losses_by_asset:
                            losses_by_asset[asset] = {'count': 0, 'total_loss': 0.0, 'reasons': {}}
                        losses_by_asset[asset]['count'] += 1
                        try:
                            net_val = float(trade['net'].replace('€', '').replace('+', ''))
                            losses_by_asset[asset]['total_loss'] += net_val
                        except:
                            pass
                        reason = trade['exit_reason']
                        losses_by_asset[asset]['reasons'][reason] = losses_by_asset[asset]['reasons'].get(reason, 0) + 1
                    
                    # Summary by asset
                    print("  Losses by Asset:")
                    for asset, data in sorted(losses_by_asset.items(), key=lambda x: x[1]['total_loss']):
                        reasons_str = ", ".join([f"{r}: {c}" for r, c in data['reasons'].items()])
                        print(f"    {asset}: {data['count']} losses = €{data['total_loss']:.2f} ({reasons_str})")
                    
                    # Show last 5 losing trades
                    print("  Recent Losses:")
                    for trade in losing_trades[-5:]:
                        print(f"    {trade['asset']} {trade['direction']}: "
                              f"{trade['entry']}→{trade['exit']} | {trade['net']} ({trade['exit_reason']})")
            
            print("="*60 + "\n")
        except Exception as e:
            print(f"Error generating session summary: {e}")
            import traceback
            traceback.print_exc()
        
        # Send detailed session report to Discord
        if self.alerter:
            try:
                # Send compact summary first
                compact_report = session_tracker.generate_compact_discord_report()
                await self.alerter.send_message(f"🛑 **Bot Stopped**\n\n{compact_report}")
                
                # Send detailed report if trades occurred
                if session_summary['trades']['total'] > 0:
                    detailed_report = session_tracker.generate_discord_report()
                    # Split if too long (Discord has 2000 char limit)
                    if len(detailed_report) > 1900:
                        # Send in chunks
                        lines = detailed_report.split("\n")
                        chunk = ""
                        for line in lines:
                            if len(chunk) + len(line) + 1 > 1900:
                                await self.alerter.send_message(chunk)
                                chunk = line
                            else:
                                chunk += "\n" + line if chunk else line
                        if chunk:
                            await self.alerter.send_message(chunk)
                    else:
                        await self.alerter.send_message(detailed_report)
            except Exception as e:
                self.logger.error("Error sending shutdown notification", error=str(e))
        
        self._shutdown_event.set()
        print("\n✅ Bot stopped successfully\n")
        self.logger.info("Bot stopped")
    
    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()
            self.logger.info("Shutdown signal set")


def main():
    """Main entry point."""
    # Create bot
    bot = TradingBot()
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        print("\n\n🛑 Shutdown requested (Ctrl+C)...")
        print("Stopping bot and generating report...\n")
        bot.shutdown()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run bot
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        # stop() is already called from bot.start() after shutdown
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        # Try to stop gracefully even on error
        try:
            asyncio.run(bot.stop())
        except:
            pass


if __name__ == "__main__":
    main()

