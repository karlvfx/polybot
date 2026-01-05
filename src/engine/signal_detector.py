"""
Signal Detection Engine.
Identifies potential trading opportunities based on oracle lag.
"""

import time
from typing import Optional
from uuid import uuid4

import structlog

from src.models.schemas import (
    ConsensusData,
    OracleData,
    PolymarketData,
    SignalCandidate,
    SignalDirection,
    SignalType,
    VolatilityRegime,
    RejectionReason,
)
from config.settings import settings

logger = structlog.get_logger()


class SignalDetector:
    """
    Detects trading signals based on oracle-spot price divergence.
    
    Primary conditions:
    1. Spot price moved significantly (regime-adaptive threshold)
    2. Volume confirms authenticity
    3. Move is spike-like, not smooth drift
    4. Oracle is in optimal age window
    5. Polymarket odds are mispriced
    6. Liquidity is sufficient and stable
    """
    
    def __init__(self):
        self.logger = logger.bind(component="signal_detector")
        
        # Signal history for deduplication
        self._recent_signals: list[tuple[int, str]] = []  # (timestamp_ms, direction)
        self._signal_cooldown_ms = 10_000  # 10 second cooldown between signals
    
    def _calculate_move_threshold(self, consensus: ConsensusData) -> float:
        """Calculate dynamic move threshold based on ATR."""
        base_threshold = settings.signals.min_spot_move_pct
        atr_based = settings.signals.atr_multiplier * consensus.atr_5m
        return max(base_threshold, atr_based)
    
    def _get_oracle_age_window(self, regime: VolatilityRegime) -> tuple[int, int]:
        """Get oracle age window based on volatility regime."""
        if regime == VolatilityRegime.LOW:
            min_age = settings.chainlink.oracle_min_age_low_vol
        else:
            min_age = settings.chainlink.oracle_min_age_normal_vol
        
        max_age = settings.chainlink.oracle_max_age
        return min_age, max_age
    
    def _calculate_implied_probability(
        self,
        spot_price: float,
        oracle_price: float,
        move_pct: float,
    ) -> float:
        """
        Estimate implied probability that price will be up/down at settlement.
        Simplified model based on spot-oracle divergence.
        """
        # If spot moved up significantly vs oracle, probability of UP increases
        divergence = (spot_price - oracle_price) / oracle_price
        
        # Base probability + adjustment for divergence
        # This is simplified - real model would be more sophisticated
        base_prob = 0.5
        adjustment = divergence * 5  # Scale factor
        
        implied = base_prob + adjustment
        return max(0.0, min(1.0, implied))
    
    def _is_duplicate_signal(self, direction: SignalDirection) -> bool:
        """Check if we recently generated a similar signal."""
        now_ms = int(time.time() * 1000)
        
        # Clean old signals
        self._recent_signals = [
            (ts, d) for ts, d in self._recent_signals
            if now_ms - ts < self._signal_cooldown_ms
        ]
        
        # Check for duplicate
        for ts, d in self._recent_signals:
            if d == direction.value:
                return True
        
        return False
    
    def _check_primary_conditions(
        self,
        consensus: ConsensusData,
        oracle: OracleData,
        pm_data: PolymarketData,
    ) -> tuple[bool, Optional[RejectionReason], bool]:
        """
        Check primary trigger conditions.
        Returns: (passed, rejection_reason, escape_clause_used)
        """
        move_threshold = self._calculate_move_threshold(consensus)
        move_pct = abs(consensus.move_30s_pct)
        
        # Check spot movement (with escape clause)
        escape_clause_used = False
        if move_pct < settings.signals.escape_clause_min_move:
            return False, RejectionReason.INSUFFICIENT_MOVE, False
        
        if move_pct < move_threshold:
            # Check escape clause conditions
            escape_conditions = [
                oracle.oracle_age_seconds >= settings.signals.escape_clause_min_oracle_age,
                pm_data.orderbook_imbalance_ratio > (1 + settings.signals.escape_clause_min_imbalance)
                or pm_data.orderbook_imbalance_ratio < (1 - settings.signals.escape_clause_min_imbalance),
                pm_data.yes_liquidity_best >= settings.signals.escape_clause_min_liquidity,
                consensus.volume_surge_ratio >= settings.signals.escape_clause_min_volume_surge,
            ]
            
            if not all(escape_conditions):
                return False, RejectionReason.INSUFFICIENT_MOVE, False
            
            escape_clause_used = True
            self.logger.info(
                "Escape clause triggered",
                move_pct=move_pct,
                threshold=move_threshold,
            )
        
        # Check volume surge
        if consensus.volume_surge_ratio < settings.signals.volume_surge_threshold:
            return False, RejectionReason.VOLUME_LOW, escape_clause_used
        
        # Check spike concentration (avoid smooth drift)
        if consensus.spike_concentration < settings.signals.spike_concentration_threshold:
            return False, RejectionReason.SMOOTH_DRIFT, escape_clause_used
        
        # Check exchange agreement
        if not consensus.agreement:
            return False, RejectionReason.CONSENSUS_FAILURE, escape_clause_used
        
        # Check oracle age window
        min_age, max_age = self._get_oracle_age_window(consensus.volatility_regime)
        
        if oracle.oracle_age_seconds < min_age:
            return False, RejectionReason.ORACLE_TOO_FRESH, escape_clause_used
        
        if oracle.oracle_age_seconds > max_age:
            return False, RejectionReason.ORACLE_TOO_STALE, escape_clause_used
        
        # Check for fast heartbeat mode
        if oracle.is_fast_heartbeat_mode:
            return False, RejectionReason.FAST_HEARTBEAT_MODE, escape_clause_used
        
        # Check volatility filter
        if consensus.volatility_30s > settings.signals.max_volatility_30s:
            return False, RejectionReason.VOLATILITY_TOO_HIGH, escape_clause_used
        
        # Check liquidity
        if pm_data.yes_liquidity_best < settings.signals.min_liquidity_eur:
            return False, RejectionReason.LIQUIDITY_LOW, escape_clause_used
        
        # Check liquidity collapse
        if pm_data.liquidity_collapsing:
            return False, RejectionReason.LIQUIDITY_COLLAPSING, escape_clause_used
        
        return True, None, escape_clause_used
    
    def _check_mispricing(
        self,
        consensus: ConsensusData,
        oracle: OracleData,
        pm_data: PolymarketData,
        direction: SignalDirection,
    ) -> tuple[bool, float]:
        """
        Check if Polymarket odds are sufficiently mispriced.
        Returns: (is_mispriced, mispricing_magnitude)
        """
        # Calculate what probability "should" be based on spot movement
        spot_implied = self._calculate_implied_probability(
            consensus.consensus_price,
            oracle.current_value,
            consensus.move_30s_pct,
        )
        
        # Get current PM implied probability
        pm_implied = pm_data.implied_probability
        
        # Calculate mispricing
        if direction == SignalDirection.UP:
            # For UP signal, we want PM probability to be lower than it should be
            mispricing = spot_implied - pm_implied
        else:
            # For DOWN signal, we want PM probability to be higher than it should be
            mispricing = pm_implied - (1 - spot_implied)
        
        is_mispriced = mispricing >= settings.signals.min_mispricing_pct
        
        return is_mispriced, mispricing
    
    def detect(
        self,
        consensus: ConsensusData,
        oracle: OracleData,
        pm_data: PolymarketData,
    ) -> Optional[SignalCandidate]:
        """
        Detect if current market state presents a trading opportunity.
        
        Args:
            consensus: Aggregated spot price data
            oracle: Chainlink oracle data
            pm_data: Polymarket orderbook data
            
        Returns:
            SignalCandidate if opportunity detected, None otherwise
        """
        # Determine direction
        if consensus.move_30s_pct > 0:
            direction = SignalDirection.UP
        else:
            direction = SignalDirection.DOWN
        
        # Check for duplicate signal
        if self._is_duplicate_signal(direction):
            self.logger.debug("Duplicate signal suppressed", direction=direction.value)
            return None
        
        # Check primary conditions
        passed, rejection, escape_used = self._check_primary_conditions(
            consensus, oracle, pm_data
        )
        
        if not passed:
            self.logger.debug(
                "Primary conditions failed",
                rejection=rejection.value if rejection else None,
            )
            return None
        
        # Check mispricing
        is_mispriced, mispricing = self._check_mispricing(
            consensus, oracle, pm_data, direction
        )
        
        if not is_mispriced:
            self.logger.debug(
                "Insufficient mispricing",
                mispricing=mispricing,
                threshold=settings.signals.min_mispricing_pct,
            )
            return None
        
        # All checks passed - create signal candidate
        now_ms = int(time.time() * 1000)
        
        signal = SignalCandidate(
            signal_id=str(uuid4()),
            timestamp_ms=now_ms,
            market_id=pm_data.market_id,
            direction=direction,
            signal_type=SignalType.ESCAPE_CLAUSE if escape_used else SignalType.STANDARD,
            consensus=consensus,
            oracle=oracle,
            polymarket=pm_data,
        )
        
        # Record signal
        self._recent_signals.append((now_ms, direction.value))
        
        self.logger.info(
            "Signal candidate detected",
            signal_id=signal.signal_id,
            direction=direction.value,
            signal_type=signal.signal_type.value,
            move_pct=consensus.move_30s_pct,
            oracle_age=oracle.oracle_age_seconds,
            mispricing=mispricing,
        )
        
        return signal
    
    def get_metrics(self) -> dict:
        """Get detector metrics."""
        return {
            "recent_signals_count": len(self._recent_signals),
            "signal_cooldown_ms": self._signal_cooldown_ms,
        }

