"""
Confidence Scoring System v3 - Divergence-Based.

NEW STRATEGY: Scores signals based on spot-PM divergence, not oracle age.

The core insight: The edge is market maker lag, not oracle lag.
When spot price moves, there's an 8-12 second window before PM odds adjust.

Primary signals (60% weight):
- Divergence: How much spot-implied prob differs from PM odds
- PM Staleness: How long PM orderbook hasn't changed

Supporting factors (40% weight):
- Consensus strength, liquidity, volume surge, spike concentration
"""

import math
from datetime import datetime
from typing import Optional, TYPE_CHECKING

import structlog

from src.models.schemas import (
    SignalCandidate,
    ScoringData,
    ConfidenceBreakdown,
)
from config.settings import settings

if TYPE_CHECKING:
    from src.utils.time_filter import TimeOfDayAnalyzer

logger = structlog.get_logger()


def calculate_spot_implied_prob(momentum: float, scale: float = 100.0) -> float:
    """Convert spot momentum to implied probability."""
    # Logistic function: momentum in decimal (0.01 = 1%), scale adjusts sensitivity
    return 1 / (1 + math.exp(-momentum * scale))


class ConfidenceScorer:
    """
    Calculates confidence score for trading signals.
    
    NEW: Divergence-based scoring (v3).
    
    Primary Signals (60%):
    - Divergence Score: 0.40 - Spot-PM probability divergence
    - PM Staleness Score: 0.20 - How long orderbook hasn't updated
    
    Supporting Factors (40%):
    - Consensus Strength: 0.15 - Exchange agreement quality
    - Liquidity Score: 0.10 - Available depth + stability
    - Volume Surge: 0.08 - Move authentication
    - Spike Concentration: 0.07 - Move quality (spike vs drift)
    """
    
    def __init__(self, time_analyzer: Optional["TimeOfDayAnalyzer"] = None):
        self.logger = logger.bind(component="confidence_scorer")
        self.weights = settings.confidence
        self._time_analyzer = time_analyzer
    
    def set_time_analyzer(self, analyzer: "TimeOfDayAnalyzer") -> None:
        """Set or update the time-of-day analyzer."""
        self._time_analyzer = analyzer
    
    # ==========================================================================
    # Primary Signal Scores (60% total weight)
    # ==========================================================================
    
    def _score_divergence(
        self,
        spot_move_pct: float,
        pm_yes_price: float,
    ) -> float:
        """
        Score spot-PM divergence (0.0 - 1.0).
        
        This is the PRIMARY signal - how much spot movement implies
        a different probability than what PM is showing.
        
        Higher divergence = higher score.
        """
        # Calculate spot-implied probability
        spot_implied = calculate_spot_implied_prob(
            spot_move_pct,
            scale=settings.signals.spot_implied_scale,
        )
        
        # Calculate divergence
        divergence = abs(spot_implied - pm_yes_price)
        
        # Normalize: 15% divergence = perfect score
        # Anything below min_divergence gets 0
        min_div = settings.signals.min_divergence_pct
        max_div = 0.15
        
        if divergence < min_div:
            return 0.0
        
        return min(1.0, (divergence - min_div) / (max_div - min_div))
    
    def _score_pm_staleness(self, orderbook_age_seconds: float) -> float:
        """
        Score PM orderbook staleness (0.0 - 1.0).
        
        Stale orderbook = opportunity hasn't been arbed yet.
        
        Window: 15-45 seconds is optimal (based on actual MM behavior).
        - Under 15s: Too fresh, MM hasn't had time to lag
        - 15-25s: Optimal window (MM lagging, opportunity exists)
        - 25-45s: Good, edge may be closing
        - Over 60s: Opportunity likely passed
        """
        min_age = settings.signals.min_pm_staleness_seconds  # 15s
        optimal_age = settings.signals.optimal_pm_staleness_seconds  # 25s
        max_age = settings.signals.max_pm_staleness_seconds  # 60s
        
        if orderbook_age_seconds < min_age:
            # Too fresh - MM hasn't had time to lag
            return 0.0
        elif orderbook_age_seconds <= optimal_age:
            # Ramping up to optimal (15s → 25s)
            return (orderbook_age_seconds - min_age) / (optimal_age - min_age)
        elif orderbook_age_seconds <= max_age:
            # Ramping down from optimal
            return 1.0 - (orderbook_age_seconds - optimal_age) / (max_age - optimal_age)
        else:
            # Too stale
            return 0.0
    
    # ==========================================================================
    # Supporting Factor Scores (40% total weight)
    # ==========================================================================
    
    def _score_consensus_strength(
        self,
        agreement_score: float,
        move_consistency: float = 0.8,
    ) -> float:
        """
        Score exchange consensus quality (0.0 - 1.0).
        """
        return (agreement_score + move_consistency) / 2
    
    def _score_liquidity(
        self,
        available_liquidity: float,
        liquidity_30s_ago: float,
    ) -> float:
        """
        Score liquidity (0.0 - 1.0).
        Considers both absolute liquidity and stability.
        """
        # Base score: €100 liquidity = perfect
        base_score = min(1.0, available_liquidity / 100.0)
        
        # Stability factor: penalize collapsing books
        if liquidity_30s_ago > 0:
            stability_factor = min(1.0, available_liquidity / liquidity_30s_ago)
        else:
            stability_factor = 1.0
        
        return base_score * stability_factor
    
    def _score_volume_surge(self, volume_ratio: float) -> float:
        """
        Score volume surge (0.0 - 1.0).
        2.5x volume = perfect score.
        """
        if volume_ratio <= 1.0:
            return 0.0
        
        return min(1.0, (volume_ratio - 1.0) / 1.5)
    
    def _score_spike_concentration(self, concentration: float) -> float:
        """
        Score spike concentration (0.0 - 1.0).
        70% concentration = perfect score.
        """
        if concentration <= 0.4:
            return 0.0
        
        return min(1.0, (concentration - 0.4) / 0.3)
    
    def _score_maker_advantage(
        self,
        pm_data,  # PolymarketData
        direction: str,
    ) -> float:
        """
        Score based on maker order viability (0.0 - 1.0).
        
        Polymarket fee structure (Jan 2026):
        - Makers: 0% fee + daily rebate (~0.5-2% of volume)
        - Takers: 0.25% base rate squared by price
        
        Higher score = more favorable fee structure.
        """
        side = "YES" if direction == "UP" else "NO"
        current_price = pm_data.yes_bid if side == "YES" else pm_data.no_bid
        spread = abs(pm_data.yes_ask - pm_data.yes_bid)
        
        # Calculate taker fee (what we'd pay if we take)
        taker_fee = pm_data.calculate_effective_fee(side, current_price, is_maker=False)
        
        scores = []
        
        # 1. Low-fee zone bonus (20-80% odds = 0.2-1.1% fees)
        if 0.20 <= current_price <= 0.80:
            scores.append(1.0)  # Sweet spot
        elif 0.15 <= current_price <= 0.85:
            scores.append(0.7)
        elif 0.45 <= current_price <= 0.55:
            scores.append(0.2)  # 50% odds = worst fees (1.6-3%)
        else:
            scores.append(0.5)
        
        # 2. Spread tightness (tight spread = easy to make)
        if spread < 0.02:  # <2%
            scores.append(1.0)
        elif spread < 0.05:  # <5%
            scores.append(0.7)
        else:
            scores.append(0.3)
        
        # 3. Taker fee avoidance value (higher fees = more valuable to make)
        if taker_fee > 0.015:  # >1.5%
            scores.append(1.0)  # High value to avoid
        elif taker_fee > 0.010:  # >1.0%
            scores.append(0.7)
        else:
            scores.append(0.5)
        
        return sum(scores) / len(scores)
    
    # ==========================================================================
    # Main Scoring Method
    # ==========================================================================
    
    def score(self, signal: SignalCandidate) -> ScoringData:
        """
        Calculate confidence score using divergence-based strategy.
        
        Args:
            signal: The signal candidate to score
            
        Returns:
            ScoringData with total confidence and component breakdown
        """
        if not signal.consensus or not signal.polymarket:
            return ScoringData(
                confidence=0.0,
                breakdown=ConfidenceBreakdown(),
            )
        
        consensus = signal.consensus
        pm = signal.polymarket
        
        # ======================
        # Primary Signals (55%)
        # ======================
        
        # Divergence score (35%)
        divergence_score = self._score_divergence(
            consensus.move_30s_pct,
            pm.yes_bid,
        )
        
        # PM staleness score (20%)
        pm_staleness_score = self._score_pm_staleness(pm.orderbook_age_seconds)
        
        # Debug logging
        self.logger.debug(
            "Confidence calculation",
            spot_move=f"{consensus.move_30s_pct:.4%}",
            pm_yes=f"{pm.yes_bid:.2f}",
            pm_age=f"{pm.orderbook_age_seconds:.0f}s",
            div_score=f"{divergence_score:.2f}",
            staleness_score=f"{pm_staleness_score:.2f}",
        )
        
        # ======================
        # Supporting Factors (40%)
        # ======================
        
        # Consensus strength (15%)
        consensus_score = self._score_consensus_strength(
            consensus.agreement_score,
        )
        
        # Liquidity score (10%)
        liquidity_score = self._score_liquidity(
            pm.yes_liquidity_best,
            pm.liquidity_30s_ago,
        )
        
        # Volume surge score (8%)
        volume_score = self._score_volume_surge(consensus.volume_surge_ratio)
        
        # Spike concentration score (7%)
        spike_score = self._score_spike_concentration(consensus.spike_concentration)
        
        # Maker advantage score (5%) - NEW: Fee-aware scoring
        maker_score = self._score_maker_advantage(
            pm,
            signal.direction.value if signal.direction else "UP",
        )
        
        # Create breakdown
        breakdown = ConfidenceBreakdown(
            divergence=divergence_score,
            pm_staleness=pm_staleness_score,
            consensus_strength=consensus_score,
            liquidity=liquidity_score,
            volume_surge=volume_score,
            spike_concentration=spike_score,
            maker_advantage=maker_score,  # NEW
        )
        
        # Calculate weighted confidence
        confidence = (
            self.weights.divergence_weight * divergence_score +
            self.weights.pm_staleness_weight * pm_staleness_score +
            self.weights.consensus_strength_weight * consensus_score +
            self.weights.liquidity_weight * liquidity_score +
            self.weights.volume_surge_weight * volume_score +
            self.weights.spike_concentration_weight * spike_score +
            self.weights.maker_advantage_weight * maker_score  # Fee-aware bonus
        )
        
        # Apply probability normalization penalty (if YES + NO != 1.0)
        _, _, prob_penalty = pm.get_normalized_probabilities()
        if prob_penalty < 1.0:
            self.logger.debug(
                "Probability normalization penalty applied",
                yes_bid=f"{pm.yes_bid:.3f}",
                no_bid=f"{pm.no_bid:.3f}",
                sum=f"{pm.yes_bid + pm.no_bid:.3f}",
                penalty=f"{prob_penalty:.2f}",
            )
            confidence *= prob_penalty
        
        # Apply escape clause penalty (if applicable)
        escape_clause_used = signal.signal_type.value == "escape_clause"
        confidence_penalty = 0.0
        
        if escape_clause_used:
            confidence_penalty = settings.signals.escape_clause_confidence_penalty
            confidence *= (1 - confidence_penalty)
        
        # Time-of-day adjustment (optional)
        time_multiplier = 1.0
        current_hour = datetime.now().hour
        
        if self._time_analyzer:
            time_multiplier = self._time_analyzer.get_confidence_multiplier(current_hour)
            confidence *= time_multiplier
        
        # Clamp to valid range
        confidence = max(0.0, min(1.0, confidence))
        
        self.logger.debug(
            "Confidence scored (divergence strategy)",
            signal_id=signal.signal_id[:8] if signal.signal_id else "N/A",
            confidence=f"{confidence:.2%}",
            tier=self.get_confidence_tier(confidence),
            breakdown={
                "divergence": f"{divergence_score:.2f}",
                "pm_staleness": f"{pm_staleness_score:.2f}",
                "consensus": f"{consensus_score:.2f}",
                "liquidity": f"{liquidity_score:.2f}",
                "volume": f"{volume_score:.2f}",
                "spike": f"{spike_score:.2f}",
            },
        )
        
        return ScoringData(
            confidence=confidence,
            breakdown=breakdown,
            escape_clause_used=escape_clause_used,
            confidence_penalty=confidence_penalty,
        )
    
    def get_confidence_tier(self, confidence: float) -> str:
        """Get human-readable confidence tier."""
        if confidence >= 0.85:
            return "HIGH (★★★★★)"
        elif confidence >= 0.75:
            return "GOOD (★★★★☆)"
        elif confidence >= 0.65:
            return "MODERATE (★★★☆☆)"
        elif confidence >= 0.55:
            return "LOW (★★☆☆☆)"
        else:
            return "POOR (★☆☆☆☆)"
