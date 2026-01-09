"""
Sports Signal Confidence Scorer.

Calculates confidence score for sports arbitrage signals.
Similar to crypto confidence scorer but with sports-specific factors.

Key differences from crypto:
- No fee adjustment needed (0% taker fees!)
- Time-to-event is more important (court-siding risk)
- Sharp book quality matters more than exchange agreement
- Vig indicates line sharpness
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from src.sports.models.schemas import (
    SportSignalCandidate,
    SharpBookData,
    DivergenceType,
)

logger = structlog.get_logger()


@dataclass
class SportsConfidenceWeights:
    """Weights for confidence scoring components."""
    
    # Primary factors
    divergence: float = 0.35        # Size of the edge (most important)
    pm_staleness: float = 0.20      # PM hasn't updated = opportunity
    sharp_quality: float = 0.15     # How reliable is the sharp data
    
    # Secondary factors
    liquidity: float = 0.10         # Can we actually fill?
    timing: float = 0.10            # Time-to-event sweet spot
    agreement: float = 0.10         # Do sharp books agree?


@dataclass
class ConfidenceBreakdown:
    """Detailed breakdown of confidence score."""
    total: float
    tier: str
    
    # Component scores (0-1)
    divergence_score: float
    pm_staleness_score: float
    sharp_quality_score: float
    liquidity_score: float
    timing_score: float
    agreement_score: float
    
    # Penalties applied
    penalties: list[str]
    penalty_total: float


class SportsConfidenceScorer:
    """
    Scores sports arbitrage signals.
    
    Unlike crypto:
    - Lower divergence thresholds (0.5% vs 8%) due to 0% fees
    - Time-to-event scoring is critical (court-siding)
    - Sharp book quality matters more
    """
    
    # Divergence scoring thresholds
    MIN_DIVERGENCE = 0.005   # 0.5% minimum
    TARGET_DIVERGENCE = 0.02  # 2% = perfect score
    
    # PM staleness scoring
    OPTIMAL_PM_STALENESS_MIN = 10.0   # 10 seconds
    OPTIMAL_PM_STALENESS_MAX = 60.0   # 60 seconds
    
    # Timing scoring (time to event start)
    OPTIMAL_TIME_MIN = 300.0     # 5 minutes (avoid court-siding)
    OPTIMAL_TIME_MAX = 3600.0    # 1 hour (sweet spot)
    TOO_FAR_TIME = 86400.0       # 24 hours (too much model risk)
    
    # Liquidity thresholds
    TARGET_LIQUIDITY = 1000.0    # $1000 = perfect score
    MIN_LIQUIDITY = 100.0        # $100 minimum
    
    # Confidence tiers
    TIERS = {
        0.85: "★★★★★ EXCELLENT",
        0.75: "★★★★☆ VERY GOOD",
        0.65: "★★★☆☆ GOOD",
        0.55: "★★☆☆☆ MODERATE",
        0.00: "★☆☆☆☆ LOW",
    }
    
    def __init__(self, weights: Optional[SportsConfidenceWeights] = None):
        self.weights = weights or SportsConfidenceWeights()
        self.logger = logger.bind(component="sports_confidence")
    
    def score(
        self,
        signal: SportSignalCandidate,
        sharp_data: Optional[SharpBookData] = None,
    ) -> ConfidenceBreakdown:
        """
        Calculate confidence score for a sports signal.
        
        Args:
            signal: The signal candidate to score
            sharp_data: Optional additional sharp book data
        
        Returns:
            ConfidenceBreakdown with total score and component breakdown
        """
        penalties = []
        penalty_total = 0.0
        
        # =================================================================
        # Component Scores
        # =================================================================
        
        # 1. Divergence Score (35%)
        divergence_score = self._score_divergence(signal.divergence_pct)
        
        # 2. PM Staleness Score (20%)
        pm_staleness_score = self._score_pm_staleness(signal.pm_staleness_seconds)
        
        # 3. Sharp Quality Score (15%)
        sharp_quality_score = self._score_sharp_quality(sharp_data)
        
        # 4. Liquidity Score (10%)
        liquidity_score = self._score_liquidity(signal.pm_liquidity)
        
        # 5. Timing Score (10%)
        timing_score = self._score_timing(signal.time_to_event_seconds)
        
        # 6. Agreement Score (10%)
        agreement_score = self._score_agreement(sharp_data)
        
        # =================================================================
        # Calculate weighted total
        # =================================================================
        
        raw_score = (
            self.weights.divergence * divergence_score +
            self.weights.pm_staleness * pm_staleness_score +
            self.weights.sharp_quality * sharp_quality_score +
            self.weights.liquidity * liquidity_score +
            self.weights.timing * timing_score +
            self.weights.agreement * agreement_score
        )
        
        # =================================================================
        # Apply Penalties
        # =================================================================
        
        # Penalty: Wide spread
        if signal.pm_spread > 0.03:  # 3% spread
            penalty = 0.05
            penalties.append(f"wide_spread:{penalty:.0%}")
            penalty_total += penalty
        
        # Penalty: Low liquidity (below minimum)
        if signal.pm_liquidity < self.MIN_LIQUIDITY:
            penalty = 0.10
            penalties.append(f"low_liquidity:{penalty:.0%}")
            penalty_total += penalty
        
        # Penalty: Event very close to start
        if signal.time_to_event_seconds < 600:  # 10 minutes
            penalty = 0.05
            penalties.append(f"close_to_start:{penalty:.0%}")
            penalty_total += penalty
        
        # Penalty: No direction (edge case)
        if signal.divergence_type == DivergenceType.NONE:
            penalty = 0.15
            penalties.append(f"no_direction:{penalty:.0%}")
            penalty_total += penalty
        
        # Calculate final score
        final_score = max(0.0, min(1.0, raw_score - penalty_total))
        
        # Determine tier
        tier = "★☆☆☆☆ LOW"
        for threshold, tier_name in sorted(self.TIERS.items(), reverse=True):
            if final_score >= threshold:
                tier = tier_name
                break
        
        # Update signal confidence
        signal.confidence = final_score
        
        breakdown = ConfidenceBreakdown(
            total=final_score,
            tier=tier,
            divergence_score=divergence_score,
            pm_staleness_score=pm_staleness_score,
            sharp_quality_score=sharp_quality_score,
            liquidity_score=liquidity_score,
            timing_score=timing_score,
            agreement_score=agreement_score,
            penalties=penalties,
            penalty_total=penalty_total,
        )
        
        self.logger.debug(
            "Scored signal",
            signal_id=signal.signal_id[:8],
            total=f"{final_score:.1%}",
            tier=tier,
            divergence=f"{divergence_score:.2f}",
            pm_staleness=f"{pm_staleness_score:.2f}",
            penalties=len(penalties),
        )
        
        return breakdown
    
    # =========================================================================
    # Component Scoring
    # =========================================================================
    
    def _score_divergence(self, divergence: float) -> float:
        """
        Score the divergence (size of edge).
        
        0.5% divergence = 0.0 (minimum viable)
        2.0% divergence = 1.0 (excellent)
        """
        if divergence < self.MIN_DIVERGENCE:
            return 0.0
        
        # Linear scale from MIN to TARGET
        score = (divergence - self.MIN_DIVERGENCE) / (self.TARGET_DIVERGENCE - self.MIN_DIVERGENCE)
        return min(1.0, score)
    
    def _score_pm_staleness(self, staleness_seconds: float) -> float:
        """
        Score PM staleness.
        
        Optimal: 10-60 seconds (market makers haven't updated)
        Too fresh: <10s (might catch up)
        Too stale: >60s (might be stuck/broken)
        """
        if staleness_seconds < self.OPTIMAL_PM_STALENESS_MIN:
            # Too fresh - ramping up
            return staleness_seconds / self.OPTIMAL_PM_STALENESS_MIN
        elif staleness_seconds <= self.OPTIMAL_PM_STALENESS_MAX:
            # Sweet spot
            return 1.0
        else:
            # Getting stale - ramping down
            decay = (staleness_seconds - self.OPTIMAL_PM_STALENESS_MAX) / 120.0
            return max(0.0, 1.0 - decay)
    
    def _score_sharp_quality(self, sharp_data: Optional[SharpBookData]) -> float:
        """
        Score the quality of sharp book data.
        
        Factors:
        - Which book (Pinnacle = best)
        - Vig (lower = sharper)
        - Update freshness
        """
        if not sharp_data:
            return 0.5  # Neutral if no data
        
        score = 0.5  # Base score
        
        # Book quality bonus
        book = sharp_data.primary_book.lower()
        if "pinnacle" in book:
            score += 0.3
        elif "betfair" in book:
            score += 0.25
        elif "circa" in book:
            score += 0.2
        else:
            score += 0.1
        
        # Low vig bonus (Pinnacle ~2-3%, soft books ~5%)
        if sharp_data.vig < 0.025:
            score += 0.15
        elif sharp_data.vig < 0.035:
            score += 0.10
        elif sharp_data.vig < 0.045:
            score += 0.05
        
        # Freshness bonus
        age_seconds = sharp_data.update_age_seconds
        if age_seconds < 10:
            score += 0.05
        elif age_seconds > 60:
            score -= 0.10
        
        return min(1.0, max(0.0, score))
    
    def _score_liquidity(self, liquidity: float) -> float:
        """
        Score PM liquidity.
        
        $100 = minimum viable
        $1000 = perfect score
        """
        if liquidity < self.MIN_LIQUIDITY:
            return 0.0
        
        score = (liquidity - self.MIN_LIQUIDITY) / (self.TARGET_LIQUIDITY - self.MIN_LIQUIDITY)
        return min(1.0, score)
    
    def _score_timing(self, time_to_event: float) -> float:
        """
        Score time until event starts.
        
        5-60 minutes: Perfect (enough time, minimal model risk)
        <5 minutes: Risky (court-siding)
        >24 hours: Risky (too much uncertainty)
        """
        if time_to_event < self.OPTIMAL_TIME_MIN:
            # Too close - ramp down to 0
            return time_to_event / self.OPTIMAL_TIME_MIN
        elif time_to_event <= self.OPTIMAL_TIME_MAX:
            # Sweet spot
            return 1.0
        elif time_to_event <= self.TOO_FAR_TIME:
            # Ramping down
            progress = (time_to_event - self.OPTIMAL_TIME_MAX) / (self.TOO_FAR_TIME - self.OPTIMAL_TIME_MAX)
            return max(0.3, 1.0 - (progress * 0.5))
        else:
            # Too far out
            return 0.3
    
    def _score_agreement(self, sharp_data: Optional[SharpBookData]) -> float:
        """
        Score agreement between sharp books.
        
        High agreement = more reliable signal
        """
        if not sharp_data:
            return 0.5  # Neutral
        
        return sharp_data.agreement_score

