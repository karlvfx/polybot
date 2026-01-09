"""
Sports Signal Detection Engine.

Detects arbitrage opportunities between sharp sportsbooks and Polymarket.

Key differences from crypto bot:
- 0% taker fees (vs 1.6-3%+ for crypto) → much smaller edge needed
- Min divergence: 0.5% (vs 8% for crypto)
- Signal window: 10-60 seconds (vs 8-12s for crypto)
- Data source: Sharp books (vs spot exchanges)

The core pattern is identical:
    Sharp Book Price = "Truth" (like spot price for crypto)
    Polymarket Price = "Slow" price to exploit
    Divergence = Opportunity
"""

import time
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import structlog

from src.sports.models.schemas import (
    Sport,
    SportEvent,
    SharpBookData,
    SportSignalCandidate,
    DivergenceType,
)
from src.models.schemas import PolymarketData

logger = structlog.get_logger()


@dataclass
class SportsSignalConfig:
    """Configuration for sports signal detection."""
    
    # Divergence thresholds (much lower than crypto due to 0% fees!)
    min_divergence_pct: float = 0.005  # 0.5% minimum divergence
    high_divergence_pct: float = 0.02  # 2% = high confidence signal
    
    # PM staleness thresholds
    min_pm_staleness_seconds: float = 5.0   # PM must be at least 5s stale
    max_pm_staleness_seconds: float = 300.0  # 5 minutes max (PM might be dead)
    
    # Timing
    min_time_to_event_seconds: float = 300.0   # 5 min before event (avoid court-siding)
    max_time_to_event_seconds: float = 86400.0  # 24 hours (farther = more model risk)
    
    # Quality thresholds
    min_pm_liquidity: float = 100.0   # $100 minimum liquidity
    max_pm_spread: float = 0.05       # 5% max spread
    min_sharp_agreement: float = 0.90  # 90% agreement between sharp books
    min_sharp_books: int = 1          # At least 1 sharp book (Pinnacle preferred)
    
    # Vig thresholds (helps identify true sharp lines)
    max_sharp_vig: float = 0.04  # Pinnacle typically 2-3%, DK/FD are 4-5%
    
    # Signal cooldown
    cooldown_per_event_ms: int = 60_000  # 1 minute between signals for same event


class SportsSignalDetector:
    """
    Detects sports arbitrage signals.
    
    Compares sharp sportsbook odds to Polymarket prices.
    Signals when PM is mispriced relative to sharp "truth".
    
    Unlike crypto:
    - Uses implied probability from odds (not spot price)
    - Divergence threshold is 0.5% (not 8%) due to 0% fees
    - Signals are less frequent but higher quality
    """
    
    def __init__(self, config: Optional[SportsSignalConfig] = None):
        self.config = config or SportsSignalConfig()
        self.logger = logger.bind(component="sports_signal_detector")
        
        # Signal history for deduplication
        self._recent_signals: dict[str, int] = {}  # event_id -> last_signal_ms
        
        # Rejection tracking
        self._rejection_counts: dict[str, int] = {}
        self._last_rejection_log_ms: int = 0
    
    # =========================================================================
    # Core Detection
    # =========================================================================
    
    def detect(
        self,
        sharp_data: SharpBookData,
        pm_data: PolymarketData,
        side: str = "home",  # "home", "away", "over", "under"
    ) -> Optional[SportSignalCandidate]:
        """
        Detect if there's an arbitrage opportunity.
        
        Args:
            sharp_data: Aggregated data from sharp books (Pinnacle, etc.)
            pm_data: Polymarket orderbook data
            side: Which side to check ("home", "away", etc.)
        
        Returns:
            SportSignalCandidate if opportunity exists, None otherwise
        """
        event = sharp_data.event
        now_ms = int(time.time() * 1000)
        
        # =================================================================
        # Early filters
        # =================================================================
        
        # Check for duplicate signal
        if self._is_duplicate_signal(event.event_id):
            return None
        
        # Check PM data validity
        if not self._validate_pm_data(pm_data):
            return None
        
        # Check sharp data validity
        if not self._validate_sharp_data(sharp_data):
            return None
        
        # Check timing (avoid court-siding window)
        if not self._validate_timing(event):
            return None
        
        # =================================================================
        # Calculate divergence
        # =================================================================
        
        # Get sharp fair probability for the selected side
        if side == "home":
            sharp_prob = sharp_data.fair_home_prob
        elif side == "away":
            sharp_prob = sharp_data.fair_away_prob
        elif side == "draw":
            sharp_prob = sharp_data.fair_draw_prob
        else:
            self.logger.debug("Unknown side", side=side)
            return None
        
        if sharp_prob <= 0:
            self.logger.debug("No sharp probability for side", side=side)
            return None
        
        # PM implied probability (YES price = probability of outcome)
        pm_prob = pm_data.yes_bid
        
        # Calculate divergence
        divergence = abs(sharp_prob - pm_prob)
        
        # Determine direction
        if sharp_prob > pm_prob:
            divergence_type = DivergenceType.SHARP_HIGHER
            # Sharp says MORE likely than PM → buy YES
        elif sharp_prob < pm_prob:
            divergence_type = DivergenceType.SHARP_LOWER
            # Sharp says LESS likely than PM → buy NO
        else:
            divergence_type = DivergenceType.NONE
        
        # =================================================================
        # Check thresholds
        # =================================================================
        
        # Minimum divergence check
        if divergence < self.config.min_divergence_pct:
            self._track_rejection("divergence_low", divergence)
            return None
        
        # PM staleness check (want stale but not dead)
        pm_staleness = pm_data.orderbook_age_seconds
        if pm_staleness < self.config.min_pm_staleness_seconds:
            self._track_rejection("pm_too_fresh", divergence)
            return None
        
        if pm_staleness > self.config.max_pm_staleness_seconds:
            self._track_rejection("pm_too_stale", divergence)
            self.logger.debug(
                "PM too stale",
                staleness=f"{pm_staleness:.0f}s",
                max=f"{self.config.max_pm_staleness_seconds:.0f}s",
            )
            return None
        
        # =================================================================
        # Signal detected!
        # =================================================================
        
        signal = SportSignalCandidate(
            signal_id=str(uuid4()),
            timestamp_ms=now_ms,
            event=event,
            sport=event.sport,
            divergence_type=divergence_type,
            side=side,
            sharp_fair_prob=sharp_prob,
            pm_implied_prob=pm_prob,
            divergence_pct=divergence,
            pm_market_id=pm_data.market_id,
            pm_yes_price=pm_data.yes_bid,
            pm_no_price=pm_data.no_bid,
            pm_liquidity=pm_data.yes_liquidity_best + pm_data.no_liquidity_best,
            pm_spread=pm_data.spread,
            pm_staleness_seconds=pm_staleness,
            time_to_event_seconds=event.time_until_start_seconds,
        )
        
        # Record signal
        self._recent_signals[event.event_id] = now_ms
        
        # Log detection
        self.logger.info(
            "🎯 SPORTS SIGNAL DETECTED",
            event=event.get_display_name(),
            sport=event.sport.name,
            side=side,
            direction=signal.direction,
            sharp_prob=f"{sharp_prob:.1%}",
            pm_prob=f"{pm_prob:.1%}",
            divergence=f"{divergence:.2%}",
            pm_staleness=f"{pm_staleness:.1f}s",
            sharp_book=sharp_data.primary_book,
            time_to_start=f"{event.time_until_start_seconds/60:.0f}min",
        )
        
        return signal
    
    def detect_all_sides(
        self,
        sharp_data: SharpBookData,
        pm_data_home: Optional[PolymarketData],
        pm_data_away: Optional[PolymarketData],
        pm_data_draw: Optional[PolymarketData] = None,
    ) -> list[SportSignalCandidate]:
        """
        Check all sides of an event for signals.
        
        Sports often have separate PM markets for each outcome:
        - "Chiefs to win" (home)
        - "Ravens to win" (away)
        - "Draw" (for soccer)
        
        Returns all valid signals found.
        """
        signals = []
        
        if pm_data_home:
            signal = self.detect(sharp_data, pm_data_home, side="home")
            if signal:
                signals.append(signal)
        
        if pm_data_away:
            signal = self.detect(sharp_data, pm_data_away, side="away")
            if signal:
                signals.append(signal)
        
        if pm_data_draw:
            signal = self.detect(sharp_data, pm_data_draw, side="draw")
            if signal:
                signals.append(signal)
        
        return signals
    
    # =========================================================================
    # Validation Helpers
    # =========================================================================
    
    def _validate_pm_data(self, pm_data: PolymarketData) -> bool:
        """Validate Polymarket data quality."""
        
        # Check for valid prices
        if pm_data.yes_bid <= 0.01 or pm_data.yes_bid >= 0.99:
            self._track_rejection("pm_price_extreme", 0)
            return False
        
        # Check liquidity
        if pm_data.yes_liquidity_best < self.config.min_pm_liquidity:
            self._track_rejection("pm_liquidity_low", 0)
            self.logger.debug(
                "PM liquidity too low",
                liquidity=f"${pm_data.yes_liquidity_best:.0f}",
                min=f"${self.config.min_pm_liquidity:.0f}",
            )
            return False
        
        # Check spread
        if pm_data.spread > self.config.max_pm_spread:
            self._track_rejection("pm_spread_wide", 0)
            self.logger.debug(
                "PM spread too wide",
                spread=f"{pm_data.spread:.1%}",
                max=f"{self.config.max_pm_spread:.1%}",
            )
            return False
        
        # Check for liquidity collapse
        if pm_data.liquidity_collapsing:
            self._track_rejection("pm_liquidity_collapsing", 0)
            return False
        
        return True
    
    def _validate_sharp_data(self, sharp_data: SharpBookData) -> bool:
        """Validate sharp book data quality."""
        
        # Check we have enough books
        if sharp_data.books_count < self.config.min_sharp_books:
            self._track_rejection("sharp_books_low", 0)
            return False
        
        # Check agreement across books
        if sharp_data.agreement_score < self.config.min_sharp_agreement:
            self._track_rejection("sharp_disagreement", 0)
            self.logger.debug(
                "Sharp books disagree",
                agreement=f"{sharp_data.agreement_score:.1%}",
                min=f"{self.config.min_sharp_agreement:.1%}",
            )
            return False
        
        # Check vig (high vig = soft book, not reliable)
        if sharp_data.vig > self.config.max_sharp_vig:
            self._track_rejection("sharp_vig_high", 0)
            self.logger.debug(
                "Sharp vig too high (soft book?)",
                vig=f"{sharp_data.vig:.1%}",
                max=f"{self.config.max_sharp_vig:.1%}",
                book=sharp_data.primary_book,
            )
            return False
        
        # Check staleness
        if sharp_data.is_stale:
            self._track_rejection("sharp_stale", 0)
            return False
        
        return True
    
    def _validate_timing(self, event: SportEvent) -> bool:
        """Validate event timing is within safe window."""
        
        time_to_start = event.time_until_start_seconds
        
        # Too close to start (court-siding risk)
        if time_to_start < self.config.min_time_to_event_seconds:
            self._track_rejection("too_close_to_start", 0)
            self.logger.debug(
                "Event starting too soon (court-siding risk)",
                time_to_start=f"{time_to_start/60:.1f}min",
                min=f"{self.config.min_time_to_event_seconds/60:.1f}min",
            )
            return False
        
        # Too far out (model uncertainty)
        if time_to_start > self.config.max_time_to_event_seconds:
            self._track_rejection("too_far_out", 0)
            return False
        
        # Avoid live events
        if event.is_live:
            self._track_rejection("event_live", 0)
            self.logger.debug("Skipping live event (court-siding risk)")
            return False
        
        return True
    
    def _is_duplicate_signal(self, event_id: str) -> bool:
        """Check if we recently signaled this event."""
        now_ms = int(time.time() * 1000)
        
        # Clean old signals
        cutoff_ms = now_ms - self.config.cooldown_per_event_ms
        self._recent_signals = {
            eid: ts for eid, ts in self._recent_signals.items()
            if ts > cutoff_ms
        }
        
        # Check for duplicate
        last_signal_ms = self._recent_signals.get(event_id, 0)
        if now_ms - last_signal_ms < self.config.cooldown_per_event_ms:
            return True
        
        return False
    
    def _track_rejection(self, reason: str, divergence: float) -> None:
        """Track rejection for metrics."""
        self._rejection_counts[reason] = self._rejection_counts.get(reason, 0) + 1
        
        # Log periodically
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_rejection_log_ms > 60_000:  # Every minute
            self._last_rejection_log_ms = now_ms
            if self._rejection_counts:
                self.logger.debug(
                    "Sports signal rejections (last 60s)",
                    rejections=dict(self._rejection_counts),
                )
                self._rejection_counts.clear()
    
    # =========================================================================
    # Metrics
    # =========================================================================
    
    def get_metrics(self) -> dict:
        """Get detector metrics."""
        return {
            "recent_signals": len(self._recent_signals),
            "min_divergence": f"{self.config.min_divergence_pct:.1%}",
            "cooldown_ms": self.config.cooldown_per_event_ms,
            "rejection_counts": dict(self._rejection_counts),
        }

