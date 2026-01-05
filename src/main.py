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
from src.modes.shadow import ShadowMode
from src.modes.alert import AlertMode
from src.modes.night_auto import NightAutoMode
from src.utils.logging import setup_logging, SignalLogger, MetricsLogger, PerformanceTracker
from src.utils.alerts import DiscordAlerter
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
        
        # Initialize feeds
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
        
        # Initialize engine components
        self.consensus_engine = ConsensusEngine()
        self.signal_detector = SignalDetector()
        self.validator = Validator()
        self.confidence_scorer = ConfidenceScorer()
        
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
        """Initialize Polymarket feed."""
        if not settings.polymarket.btc_up_market_id:
            self.logger.warning("No Polymarket market ID configured - feed disabled")
            return False
        
        self.polymarket_feed = PolymarketFeed(
            market_id=settings.polymarket.btc_up_market_id,
            ws_url=settings.polymarket.ws_url,
        )
        return True
    
    async def _initialize_execution(self) -> bool:
        """Initialize execution engine."""
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
        )
        
        return await self.execution_engine.initialize()
    
    def _initialize_mode(self) -> None:
        """Initialize operating mode based on settings."""
        if settings.mode == OperatingMode.SHADOW:
            self.mode = ShadowMode()
            self.logger.info("Initialized SHADOW mode")
        
        elif settings.mode == OperatingMode.ALERT:
            self.mode = AlertMode(settings.alerts.discord_webhook_url)
            self.logger.info("Initialized ALERT mode")
        
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
        """Check for trading signals."""
        # Rate limit signal checking
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_signal_check_ms < self._signal_check_interval_ms:
            return
        self._last_signal_check_ms = now_ms
        
        # Get consensus data
        consensus = self.consensus_engine.compute_consensus()
        if not consensus:
            return
        
        # Get oracle data
        oracle = None
        if self.chainlink_feed:
            oracle = self.chainlink_feed.get_data()
        
        if not oracle:
            return
        
        # Get Polymarket data
        pm_data = None
        if self.polymarket_feed:
            pm_data = self.polymarket_feed.get_data()
        
        if not pm_data:
            return
        
        # Detect signal
        signal = self.signal_detector.detect(consensus, oracle, pm_data)
        if not signal:
            return
        
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
        
        # Score signal
        scoring = self.confidence_scorer.score(signal)
        signal.scoring = scoring
        signal.is_valid = True
        
        # Record signal
        self.performance.record_signal(
            signal_type=signal.signal_type.value,
            direction=signal.direction.value,
        )
        
        # Process based on mode
        if self.mode and self.mode.should_process(signal):
            action, outcome = await self.mode.process_signal(signal)
            
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
            
            self.signal_logger.log_signal(log_entry)
            
            self.logger.info(
                "Signal processed",
                signal_id=signal.signal_id,
                confidence=scoring.confidence,
                action=action.decision.value,
            )
    
    async def _feed_health_monitor(self) -> None:
        """Monitor feed health and log metrics."""
        while self._running:
            try:
                feeds = {
                    "binance": self.binance_feed.get_metrics(),
                    "coinbase": self.coinbase_feed.get_metrics(),
                    "kraken": self.kraken_feed.get_metrics(),
                }
                
                if self.chainlink_feed:
                    feeds["chainlink"] = self.chainlink_feed.get_metrics()
                
                if self.polymarket_feed:
                    feeds["polymarket"] = self.polymarket_feed.get_metrics()
                
                self.metrics_logger.log_feed_health(feeds)
                
                # Check for stale feeds
                stale_feeds = [name for name, metrics in feeds.items() 
                              if metrics.get("is_stale", True)]
                
                if stale_feeds:
                    self.logger.warning("Stale feeds detected", feeds=stale_feeds)
                
                await asyncio.sleep(5)
                
            except Exception as e:
                self.logger.error("Health monitor error", error=str(e))
                await asyncio.sleep(5)
    
    async def _signal_loop(self) -> None:
        """Main signal detection loop."""
        while self._running:
            try:
                await self._check_signals()
                await asyncio.sleep(0.1)  # 100ms tick
            except Exception as e:
                self.logger.error("Signal loop error", error=str(e))
                await asyncio.sleep(1)
    
    async def start(self) -> None:
        """Start the trading bot."""
        self.logger.info(
            "Starting Polymarket Oracle-Lag Trading Bot",
            mode=settings.mode.value,
        )
        
        self._running = True
        
        # Setup exchange callbacks
        self._setup_exchange_callbacks()
        
        # Initialize components
        await self._initialize_chainlink()
        await self._initialize_polymarket()
        await self._initialize_execution()
        
        # Initialize mode
        self._initialize_mode()
        
        # Send startup notification
        if self.alerter:
            await self.alerter.send_message(
                f"🚀 **Bot Started**\n"
                f"Mode: {settings.mode.value.upper()}\n"
                f"Target: BTC 15-min markets"
            )
        
        # Start all tasks
        tasks = [
            asyncio.create_task(self.binance_feed.start()),
            asyncio.create_task(self.coinbase_feed.start()),
            asyncio.create_task(self.kraken_feed.start()),
            asyncio.create_task(self._feed_health_monitor()),
            asyncio.create_task(self._signal_loop()),
        ]
        
        if self.chainlink_feed:
            tasks.append(asyncio.create_task(self.chainlink_feed.start()))
        
        if self.polymarket_feed:
            tasks.append(asyncio.create_task(self.polymarket_feed.start()))
        
        self.logger.info("All feeds started")
        
        # Wait for shutdown signal
        await self._shutdown_event.wait()
        
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def stop(self) -> None:
        """Stop the trading bot."""
        self.logger.info("Stopping bot...")
        self._running = False
        
        # Stop feeds
        await self.binance_feed.stop()
        await self.coinbase_feed.stop()
        await self.kraken_feed.stop()
        
        if self.chainlink_feed:
            await self.chainlink_feed.stop()
        
        if self.polymarket_feed:
            await self.polymarket_feed.stop()
        
        # Deactivate mode
        if self.mode:
            self.mode.deactivate()
        
        # Close loggers
        self.signal_logger.close()
        
        # Print performance report
        if isinstance(self.mode, ShadowMode):
            print(self.mode.generate_report())
        
        self.performance.print_report()
        
        # Send shutdown notification
        if self.alerter:
            summary = self.performance.get_summary()
            await self.alerter.send_message(
                f"🛑 **Bot Stopped**\n"
                f"Signals: {summary['signals']['total']}\n"
                f"Trades: {summary['trades']['total']}\n"
                f"Net Profit: €{summary['profit']['net']:.2f}"
            )
        
        self._shutdown_event.set()
        self.logger.info("Bot stopped")
    
    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self._shutdown_event.set()


def main():
    """Main entry point."""
    # Create bot
    bot = TradingBot()
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        print("\nShutdown requested...")
        bot.shutdown()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run bot
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        asyncio.run(bot.stop())


if __name__ == "__main__":
    main()

