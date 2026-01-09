"""
Sports Arbitrage Bot - Main Entry Point.

Runs the sports arbitrage detection loop:
1. Fetch odds from sharp books (The Odds API → Pinnacle)
2. Discover matching markets on Polymarket
3. Detect divergences (Sharp price vs PM price)
4. Signal when edge exists (0.5%+ divergence, 0% fees!)

Usage:
    python -m src.sports.main

Environment Variables:
    ODDS_API_KEY          - Required: The Odds API key
    SPORTS_MODE           - shadow|alert|live (default: shadow)
    SPORTS_DISCORD_WEBHOOK - Discord webhook for alerts
    SPORTS_MIN_DIVERGENCE - Minimum divergence % (default: 0.005 = 0.5%)
"""

import asyncio
import signal
import sys
import time
from typing import Optional

import structlog

from src.sports.config import get_sports_settings, SportsOperatingMode
from src.sports.feeds.odds_api import OddsAPIFeed
from src.sports.discovery.polymarket import SportsMarketDiscovery
from src.sports.engine.signal_detector import SportsSignalDetector, SportsSignalConfig
from src.sports.engine.confidence import SportsConfidenceScorer
from src.sports.models.schemas import Sport, SportEvent, SharpBookData

# Reuse crypto bot's shared components
from src.feeds.polymarket import PolymarketFeed
from src.utils.alerts import DiscordAlerter

logger = structlog.get_logger()


class SportsBot:
    """
    Sports Arbitrage Bot.
    
    Compares sharp sportsbook odds to Polymarket prices.
    Signals when PM is mispriced vs sharp "truth".
    
    Key advantage over crypto bot:
    - 0% taker fees (vs 1.6-3%+ for crypto)
    - Can profit from 0.5% divergence (vs 8% for crypto)
    - Higher liquidity markets ($1M+ vs $44k)
    """
    
    def __init__(self):
        self.settings = get_sports_settings()
        self.logger = logger.bind(component="sports_bot")
        
        # Validate required settings
        if not self.settings.odds_api.api_key:
            self.logger.error("ODDS_API_KEY environment variable required")
            raise ValueError("Missing ODDS_API_KEY")
        
        # Initialize components
        self.odds_feed = OddsAPIFeed(
            api_key=self.settings.odds_api.api_key,
            poll_interval=self.settings.odds_api.poll_interval,
        )
        
        self.discovery = SportsMarketDiscovery()
        
        self.detector = SportsSignalDetector(
            config=SportsSignalConfig(
                min_divergence_pct=self.settings.signals.min_divergence_pct,
                high_divergence_pct=self.settings.signals.high_divergence_pct,
                min_pm_staleness_seconds=self.settings.signals.min_pm_staleness_seconds,
                max_pm_staleness_seconds=self.settings.signals.max_pm_staleness_seconds,
                min_time_to_event_seconds=self.settings.signals.min_time_to_event_seconds,
                max_time_to_event_seconds=self.settings.signals.max_time_to_event_seconds,
                min_pm_liquidity=self.settings.signals.min_pm_liquidity,
                max_pm_spread=self.settings.signals.max_pm_spread,
                min_sharp_agreement=self.settings.signals.min_sharp_agreement,
                min_sharp_books=self.settings.signals.min_sharp_books,
                max_sharp_vig=self.settings.signals.max_sharp_vig,
                cooldown_per_event_ms=self.settings.signals.signal_cooldown_ms,
            )
        )
        
        self.scorer = SportsConfidenceScorer()
        
        # Discord alerter (reuse from crypto bot)
        self.alerter: Optional[DiscordAlerter] = None
        if self.settings.alerts.discord_webhook_url:
            self.alerter = DiscordAlerter(self.settings.alerts.discord_webhook_url)
        
        # PM feeds for matched markets
        self._pm_feeds: dict[str, PolymarketFeed] = {}
        
        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # Stats
        self._signals_detected = 0
        self._start_time_ms = 0
    
    async def start(self) -> None:
        """Start the sports bot."""
        self.logger.info(
            "Starting Sports Arbitrage Bot",
            mode=self.settings.mode.value,
            sports=self.settings.odds_api.sports,
            min_divergence=f"{self.settings.signals.min_divergence_pct:.1%}",
        )
        
        self._running = True
        self._start_time_ms = int(time.time() * 1000)
        
        # Send startup notification
        if self.alerter:
            await self.alerter.send_message(
                f"🏈 **Sports Bot Started**\n"
                f"**Mode:** {self.settings.mode.value.upper()}\n"
                f"**Sports:** {', '.join(self.settings.odds_api.sports)}\n"
                f"**Min Divergence:** {self.settings.signals.min_divergence_pct:.1%}\n"
                f"**Advantage:** 0% taker fees! (vs 3% for crypto)"
            )
        
        # Start main loop
        try:
            await asyncio.gather(
                self._odds_polling_loop(),
                self._discovery_loop(),
                self._signal_loop(),
                self._status_loop(),
            )
        except asyncio.CancelledError:
            self.logger.info("Bot cancelled")
        
        await self.stop()
    
    async def stop(self) -> None:
        """Stop the sports bot."""
        self.logger.info("Stopping sports bot...")
        self._running = False
        
        # Stop odds feed
        await self.odds_feed.stop()
        
        # Stop PM feeds
        for feed in self._pm_feeds.values():
            await feed.stop()
        
        # Close alerter
        if self.alerter:
            runtime_seconds = (int(time.time() * 1000) - self._start_time_ms) / 1000
            await self.alerter.send_message(
                f"🛑 **Sports Bot Stopped**\n"
                f"**Runtime:** {runtime_seconds/60:.1f} minutes\n"
                f"**Signals Detected:** {self._signals_detected}"
            )
            await self.alerter.close()
        
        self.logger.info("Sports bot stopped")
    
    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self._shutdown_event.set()
        self._running = False
    
    # =========================================================================
    # Main Loops
    # =========================================================================
    
    async def _odds_polling_loop(self) -> None:
        """Poll sharp books for odds updates."""
        self.logger.info("Starting odds polling loop")
        
        # Initialize feed
        await self.odds_feed._connect() if hasattr(self.odds_feed, '_connect') else None
        
        while self._running:
            try:
                # Fetch odds for configured sports
                for sport_key in self.settings.odds_api.sports:
                    sport = Sport.from_string(sport_key)
                    if sport:
                        await self.odds_feed.get_upcoming_events(sport)
                    await asyncio.sleep(2)  # Spread requests
                
                # Wait for next poll
                await asyncio.sleep(self.settings.odds_api.poll_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Odds polling error", error=str(e))
                await asyncio.sleep(10)
    
    async def _discovery_loop(self) -> None:
        """Discover and match PM markets to sharp events."""
        self.logger.info("Starting discovery loop")
        
        # Wait for odds feed to have data
        await asyncio.sleep(10)
        
        while self._running:
            try:
                # Discover PM sports markets
                sports = [Sport.from_string(s) for s in self.settings.odds_api.sports]
                sports = [s for s in sports if s]  # Filter None
                
                pm_markets = await self.discovery.discover_sports_markets(sports)
                
                # Match to sharp events
                for event_id in list(self.odds_feed._events.keys()):
                    sharp_event = self.odds_feed.get_event(event_id)
                    if not sharp_event:
                        continue
                    
                    # Find matching PM market
                    for pm_market in pm_markets:
                        if pm_market.matched_event_id:
                            continue  # Already matched
                        
                        match = self.discovery.match_to_sharp_event(
                            pm_market,
                            [sharp_event]
                        )
                        
                        if match:
                            # Start PM feed for this market
                            await self._start_pm_feed(pm_market.condition_id, pm_market.yes_token_id)
                
                self.logger.info(
                    "Discovery cycle complete",
                    pm_markets=len(pm_markets),
                    matched=len(self.discovery._matched),
                    pm_feeds=len(self._pm_feeds),
                )
                
                # Wait before next discovery
                await asyncio.sleep(60)  # Discover every minute
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Discovery error", error=str(e))
                await asyncio.sleep(30)
    
    async def _start_pm_feed(self, condition_id: str, yes_token_id: str) -> None:
        """Start a PM feed for a matched market."""
        if condition_id in self._pm_feeds:
            return  # Already running
        
        try:
            feed = PolymarketFeed(
                market_id=condition_id,
                auto_discover=False,
            )
            feed._yes_token_id = yes_token_id
            
            # Start in background
            asyncio.create_task(feed.start())
            self._pm_feeds[condition_id] = feed
            
            self.logger.debug("Started PM feed", condition_id=condition_id[:30])
            
        except Exception as e:
            self.logger.warning("Failed to start PM feed", error=str(e))
    
    async def _signal_loop(self) -> None:
        """Check for arbitrage signals."""
        self.logger.info("Starting signal detection loop")
        
        # Wait for feeds to initialize
        await asyncio.sleep(15)
        
        while self._running:
            try:
                # Check each matched market
                for event_id, matched in self.discovery._matched.items():
                    pm_feed = self._pm_feeds.get(matched.pm_market.condition_id)
                    if not pm_feed:
                        continue
                    
                    # Get PM data
                    pm_data = pm_feed.get_data()
                    if not pm_data:
                        continue
                    
                    # Get sharp data
                    sharp_data = self.odds_feed.get_sharp_book_data(event_id)
                    if not sharp_data:
                        continue
                    
                    # Detect signal
                    signal = self.detector.detect(
                        sharp_data=sharp_data,
                        pm_data=pm_data,
                        side=matched.side,
                    )
                    
                    if signal:
                        self._signals_detected += 1
                        
                        # Score signal
                        breakdown = self.scorer.score(signal, sharp_data)
                        
                        # Process based on mode
                        await self._process_signal(signal, breakdown, sharp_data)
                
                # Check every second
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Signal loop error", error=str(e))
                await asyncio.sleep(5)
    
    async def _process_signal(self, signal, breakdown, sharp_data) -> None:
        """Process a detected signal based on mode."""
        
        # Log signal
        self.logger.info(
            "🎯 Signal detected",
            event=signal.event.get_display_name(),
            sport=signal.sport.name,
            direction=signal.direction,
            divergence=f"{signal.divergence_pct:.2%}",
            confidence=f"{signal.confidence:.1%}",
            tier=breakdown.tier,
        )
        
        # Send alert if meets threshold
        if (self.alerter and 
            signal.confidence >= self.settings.alerts.min_confidence_for_alert):
            
            await self.alerter.send_message(
                f"🎯 **SPORTS SIGNAL**\n\n"
                f"**{signal.sport.name}:** {signal.event.get_display_name()}\n"
                f"**Direction:** {signal.direction} ({signal.side})\n\n"
                f"**Divergence:** {signal.divergence_pct:.2%}\n"
                f"**Sharp Prob:** {signal.sharp_fair_prob:.1%}\n"
                f"**PM Prob:** {signal.pm_implied_prob:.1%}\n\n"
                f"**Confidence:** {breakdown.tier}\n"
                f"**Sharp Book:** {sharp_data.primary_book}\n"
                f"**Time to Event:** {signal.time_to_event_seconds/60:.0f} min\n\n"
                f"_0% taker fees = Edge is pure profit!_"
            )
    
    async def _status_loop(self) -> None:
        """Send periodic status updates."""
        while self._running:
            try:
                await asyncio.sleep(self.settings.alerts.status_update_interval_seconds)
                
                if not self.alerter:
                    continue
                
                # Build status
                odds_metrics = self.odds_feed.get_metrics()
                discovery_metrics = self.discovery.get_metrics()
                
                runtime_seconds = (int(time.time() * 1000) - self._start_time_ms) / 1000
                
                await self.alerter.send_message(
                    f"📊 **Sports Bot Status**\n\n"
                    f"**Runtime:** {runtime_seconds/60:.1f} min\n"
                    f"**Events Tracked:** {odds_metrics['events_tracked']}\n"
                    f"**PM Markets Matched:** {discovery_metrics['matched_markets']}\n"
                    f"**Signals Detected:** {self._signals_detected}\n"
                    f"**API Requests Left:** {odds_metrics['requests_remaining']}\n"
                )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Status loop error", error=str(e))


def main():
    """Main entry point."""
    # Setup logging
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Create bot
    try:
        bot = SportsBot()
    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        sys.exit(1)
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        print("\n🛑 Shutdown requested...")
        bot.shutdown()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

