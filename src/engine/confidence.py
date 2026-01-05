"""
Confidence Scoring System v2.
Calculates a confidence score for trading signals.
"""

from typing import Optional

import structlog

from src.models.schemas import (
    SignalCandidate,
    ScoringData,
    ConfidenceBreakdown,
    VolatilityRegime,
)
from config.settings import settings

logger = structlog.get_logger()


class ConfidenceScorer:
    """
    Calculates confidence score for trading signals.
    
    Components (weighted):
    - Oracle Age Score: 0.35
    - Spot Consensus Strength: 0.25
    - Odds Misalignment Score: 0.15
    - Liquidity Score: 0.10
    - Spread Anomaly Score: 0.08
    - Volume Surge Score: 0.04
    - Spike Concentration Score: 0.03
    """
    
    def __init__(self):
        self.logger = logger.bind(component="confidence_scorer")
        self.weights = settings.confidence
    
    def _score_oracle_age(
        self,
        oracle_age: float,
        volatility_regime: VolatilityRegime,
    ) -> float:
        """
        Score oracle age (0.0 - 1.0).
        Higher score for optimal age window.
        """
        # Get regime-specific thresholds
        if volatility_regime == VolatilityRegime.LOW:
            min_age = settings.chainlink.oracle_min_age_low_vol
            optimal_age = settings.chainlink.oracle_optimal_age_low_vol
        else:
            min_age = settings.chainlink.oracle_min_age_normal_vol
            optimal_age = settings.chainlink.oracle_optimal_age_normal_vol
        
        if oracle_age < min_age:
            return 0.0
        elif oracle_age <= optimal_age:
            # Linear increase from min to optimal
            return (oracle_age - min_age) / (optimal_age - min_age)
        else:
            # Perfect score at or beyond optimal
            return 1.0
    
    def _score_consensus_strength(
        self,
        price_agreement: float,
        move_consistency: float,
    ) -> float:
        """
        Score spot consensus strength (0.0 - 1.0).
        Based on price agreement and momentum consistency.
        """
        # Price agreement: 1 - (max deviation / avg price)
        # Move consistency: min(abs_moves) / max(abs_moves)
        return (price_agreement + move_consistency) / 2
    
    def _score_misalignment(
        self,
        spot_implied: float,
        pm_implied: float,
    ) -> float:
        """
        Score odds misalignment (0.0 - 1.0).
        Higher score for larger mispricing.
        """
        misalignment = abs(pm_implied - spot_implied)
        # Normalize: 10% misalignment = perfect score
        return min(1.0, misalignment / 0.10)
    
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
    
    def _score_spread_anomaly(
        self,
        current_spread: float,
        normal_spread: float = 0.03,
    ) -> float:
        """
        Score spread anomaly (0.0 - 1.0).
        Wide spread = market confusion = opportunity.
        """
        if current_spread <= normal_spread:
            return 0.0
        
        # Score increases with spread above normal
        return min(1.0, (current_spread - normal_spread) / normal_spread)
    
    def _score_volume_surge(
        self,
        volume_ratio: float,
    ) -> float:
        """
        Score volume surge (0.0 - 1.0).
        3x volume = perfect score.
        """
        if volume_ratio <= 1.0:
            return 0.0
        
        # Linear scale from 1x to 3x
        return min(1.0, (volume_ratio - 1.0) / 2.0)
    
    def _score_spike_concentration(
        self,
        concentration: float,
    ) -> float:
        """
        Score spike concentration (0.0 - 1.0).
        80% concentration = perfect score.
        """
        if concentration <= 0.5:
            return 0.0
        
        # Linear scale from 50% to 80%
        return min(1.0, (concentration - 0.5) / 0.3)
    
    def score(self, signal: SignalCandidate) -> ScoringData:
        """
        Calculate full confidence score for a signal.
        
        Args:
            signal: The signal candidate to score
            
        Returns:
            ScoringData with total confidence and component breakdown
        """
        if not signal.consensus or not signal.oracle or not signal.polymarket:
            return ScoringData(
                confidence=0.0,
                breakdown=ConfidenceBreakdown(),
            )
        
        consensus = signal.consensus
        oracle = signal.oracle
        pm = signal.polymarket
        
        # Calculate individual scores
        oracle_age_score = self._score_oracle_age(
            oracle.oracle_age_seconds,
            consensus.volatility_regime,
        )
        
        # Consensus strength
        price_agreement = 1 - consensus.max_deviation_pct if consensus.max_deviation_pct < 1 else 0
        move_consistency = 0.8  # Simplified - would compare individual exchange moves
        consensus_score = self._score_consensus_strength(price_agreement, move_consistency)
        
        # Misalignment score
        # Estimate spot-implied probability
        spot_oracle_divergence = (consensus.consensus_price - oracle.current_value) / oracle.current_value
        spot_implied = 0.5 + (spot_oracle_divergence * 5)  # Simplified model
        spot_implied = max(0, min(1, spot_implied))
        misalignment_score = self._score_misalignment(spot_implied, pm.implied_probability)
        
        # Liquidity score
        liquidity_score = self._score_liquidity(
            pm.yes_liquidity_best,
            pm.liquidity_30s_ago,
        )
        
        # Spread anomaly score
        spread_score = self._score_spread_anomaly(pm.spread)
        
        # Volume surge score
        volume_score = self._score_volume_surge(consensus.volume_surge_ratio)
        
        # Spike concentration score
        spike_score = self._score_spike_concentration(consensus.spike_concentration)
        
        # Create breakdown
        breakdown = ConfidenceBreakdown(
            oracle_age=oracle_age_score,
            consensus_strength=consensus_score,
            misalignment=misalignment_score,
            liquidity=liquidity_score,
            spread_anomaly=spread_score,
            volume_surge=volume_score,
            spike_concentration=spike_score,
        )
        
        # Calculate weighted confidence
        confidence = (
            self.weights.oracle_age_weight * oracle_age_score +
            self.weights.consensus_strength_weight * consensus_score +
            self.weights.misalignment_weight * misalignment_score +
            self.weights.liquidity_weight * liquidity_score +
            self.weights.spread_anomaly_weight * spread_score +
            self.weights.volume_surge_weight * volume_score +
            self.weights.spike_concentration_weight * spike_score
        )
        
        # Apply escape clause penalty
        escape_clause_used = signal.signal_type.value == "escape_clause"
        confidence_penalty = 0.0
        
        if escape_clause_used:
            confidence_penalty = settings.signals.escape_clause_confidence_penalty
            confidence *= (1 - confidence_penalty)
        
        self.logger.debug(
            "Confidence scored",
            signal_id=signal.signal_id,
            confidence=confidence,
            escape_clause_used=escape_clause_used,
            breakdown={
                "oracle_age": oracle_age_score,
                "consensus": consensus_score,
                "misalignment": misalignment_score,
                "liquidity": liquidity_score,
                "spread": spread_score,
                "volume": volume_score,
                "spike": spike_score,
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

