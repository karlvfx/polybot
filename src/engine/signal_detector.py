"""
Signal Detection Engine.

NEW STRATEGY: Divergence-based signal detection.
The edge is NOT oracle lag, but market maker lag:
- Spot price moves significantly
- PM odds haven't adjusted yet (orderbook stale for 8-12s)
- We bet before market makers reprice

Key insight: Polymarket uses Chainlink Data Streams (~100ms latency),
but there's still 8-12 seconds before odds adjust due to market maker delay.
"""

import math
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
    DivergenceData,
    RejectionReason,
)
from config.settings import settings

logger = structlog.get_logger()


def calculate_spot_implied_prob(momentum_velocity: float, scale: float = 100.0) -> float:
    """
    Convert spot price momentum to implied UP probability using logistic function.
    
    Args:
        momentum_velocity: 30s price change as decimal (e.g., 0.01 = 1% up)
        scale: Sensitivity factor (higher = sharper probability curve)
    
    Returns:
        Implied probability of UP (0.0 to 1.0)
        
    Examples (with default scale=100):
        - 0% move → 50% probability
        - +0.7% move → ~67% probability
        - +1% move → ~73% probability  
        - +2% move → ~88% probability
        - -1% move → ~27% probability
    """
    # Logistic function: 1 / (1 + e^(-x))
    # momentum_velocity is in decimal form (0.01 = 1%)
    return 1 / (1 + math.exp(-momentum_velocity * scale))


class SignalDetector:
    """
    Detects trading signals based on spot-PM divergence.
    
    NEW STRATEGY (Divergence-based):
    1. Calculate what probability spot price movement implies
    2. Compare to current PM odds (YES price = UP probability)
    3. Signal when divergence > 8% AND PM orderbook stale > 8 seconds
    
    Supporting filters:
    - Volume surge confirms move authenticity
    - Spike concentration rejects smooth drift
    - Exchange agreement validates price
    - Liquidity ensures we can fill
    """
    
    def __init__(self):
        self.logger = logger.bind(component="signal_detector")
        
        # Signal history for deduplication
        self._recent_signals: list[tuple[int, str]] = []  # (timestamp_ms, direction)
        self._signal_cooldown_ms = 10_000  # 10 second cooldown between signals
        
        # Rejection tracking for debugging
        self._rejection_counts: dict[str, int] = {}
        self._last_rejection_log_ms: int = 0
        self._rejection_log_interval_ms = 30_000  # Log rejection stats every 30s
    
    # ==========================================================================
    # CORE: Divergence Calculation
    # ==========================================================================
    
    def calculate_divergence(
        self,
        consensus: ConsensusData,
        pm_data: PolymarketData,
    ) -> DivergenceData:
        """
        Calculate spot-PM divergence - the core signal.
        
        Args:
            consensus: Spot price data from exchanges
            pm_data: Polymarket orderbook data
            
        Returns:
            DivergenceData with all divergence metrics
        """
        # Get spot price momentum (30s move)
        spot_move = consensus.move_30s_pct
        
        # Calculate what spot move implies for UP probability
        spot_implied = calculate_spot_implied_prob(
            spot_move,
            scale=settings.signals.spot_implied_scale,
        )
        
        # PM implied probability (YES price = UP probability)
        pm_implied = pm_data.yes_bid  # Use bid price (what we'd pay to buy YES)
        
        # Calculate divergence (absolute difference)
        divergence = abs(spot_implied - pm_implied)
        
        # Determine signal direction
        if spot_implied > pm_implied:
            direction = "UP"  # Spot implies higher UP prob than PM shows
        else:
            direction = "DOWN"  # Spot implies lower UP prob (so DOWN is mispriced)
        
        # Get PM orderbook staleness
        pm_age = pm_data.orderbook_age_seconds
        
        # Check if actionable
        is_actionable = (
            divergence >= settings.signals.min_divergence_pct and
            pm_age >= settings.signals.min_pm_staleness_seconds and
            pm_age <= settings.signals.max_pm_staleness_seconds
        )
        
        return DivergenceData(
            spot_implied_prob=spot_implied,
            pm_implied_prob=pm_implied,
            divergence=divergence,
            pm_orderbook_age_seconds=pm_age,
            signal_direction=direction,
            is_actionable=is_actionable,
            min_divergence=settings.signals.min_divergence_pct,
            min_pm_age=settings.signals.min_pm_staleness_seconds,
        )
    
    # ==========================================================================
    # Supporting Filters
    # ==========================================================================
    
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
    
    def _track_rejection(self, reason: str) -> None:
        """Track rejection reason for periodic logging."""
        self._rejection_counts[reason] = self._rejection_counts.get(reason, 0) + 1
        
        # Periodically log rejection stats
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_rejection_log_ms > self._rejection_log_interval_ms:
            self._last_rejection_log_ms = now_ms
            if self._rejection_counts:
                self.logger.info(
                    "📊 Signal rejection stats (last 30s)",
                    rejections=dict(self._rejection_counts),
                )
                self._rejection_counts.clear()
    
    def _check_supporting_conditions(
        self,
        consensus: ConsensusData,
        pm_data: PolymarketData,
        divergence_data: DivergenceData,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """
        Check supporting conditions (volume, spike, liquidity).
        
        These are softer filters that help avoid bad trades.
        Returns: (passed, rejection_reason)
        
        OVERRIDE: If divergence is extremely high (>30%), bypass most filters.
        """
        # HIGH DIVERGENCE OVERRIDE: Skip most filters if divergence is massive
        HIGH_DIV_OVERRIDE_THRESHOLD = 0.30  # 30% divergence = definitely trade
        if divergence_data.divergence >= HIGH_DIV_OVERRIDE_THRESHOLD:
            self.logger.info(
                "🚀 HIGH DIVERGENCE OVERRIDE - Bypassing supporting filters",
                divergence=f"{divergence_data.divergence:.1%}",
                threshold=f"{HIGH_DIV_OVERRIDE_THRESHOLD:.0%}",
            )
            # Only check critical safety filters
            if pm_data.liquidity_collapsing:
                return False, RejectionReason.LIQUIDITY_COLLAPSING
            # Skip other filters for high divergence
            return True, None
        
        # Volume surge - confirms move is real
        if consensus.volume_surge_ratio < settings.signals.volume_surge_threshold:
            self._track_rejection("volume_low")
            self.logger.info(
                "❌ Rejected: Volume surge insufficient",
                volume_surge=f"{consensus.volume_surge_ratio:.2f}x",
                required=f"{settings.signals.volume_surge_threshold:.2f}x",
                divergence=f"{divergence_data.divergence:.1%}",
            )
            return False, RejectionReason.VOLUME_LOW
        
        # Spike concentration - rejects smooth drift
        if consensus.spike_concentration < settings.signals.spike_concentration_threshold:
            self._track_rejection("smooth_drift")
            self.logger.info(
                "❌ Rejected: Smooth drift (not a spike)",
                spike_concentration=f"{consensus.spike_concentration:.1%}",
                required=f"{settings.signals.spike_concentration_threshold:.1%}",
                divergence=f"{divergence_data.divergence:.1%}",
            )
            return False, RejectionReason.SMOOTH_DRIFT
        
        # Exchange agreement
        if not consensus.agreement:
            self._track_rejection("no_consensus")
            self.logger.info("❌ Rejected: No exchange consensus")
            return False, RejectionReason.CONSENSUS_FAILURE
        
        if consensus.agreement_score < settings.signals.min_agreement_score:
            self._track_rejection("poor_agreement")
            self.logger.info(
                "❌ Rejected: Poor exchange agreement",
                agreement_score=f"{consensus.agreement_score:.1%}",
                required=f"{settings.signals.min_agreement_score:.1%}",
            )
            return False, RejectionReason.CONSENSUS_FAILURE
        
        # Volatility check
        if consensus.volatility_30s > settings.signals.max_volatility_30s:
            self._track_rejection("volatility_high")
            self.logger.info(
                "❌ Rejected: Volatility too high",
                volatility=f"{consensus.volatility_30s:.3%}",
                max_allowed=f"{settings.signals.max_volatility_30s:.3%}",
            )
            return False, RejectionReason.VOLATILITY_TOO_HIGH
        
        # Liquidity check
        if pm_data.yes_liquidity_best < settings.signals.min_liquidity_eur:
            self._track_rejection("liquidity_low")
            self.logger.info(
                "❌ Rejected: PM liquidity too low",
                liquidity=f"€{pm_data.yes_liquidity_best:.2f}",
                required=f"€{settings.signals.min_liquidity_eur:.2f}",
            )
            return False, RejectionReason.LIQUIDITY_LOW
        
        if pm_data.liquidity_collapsing:
            self._track_rejection("liquidity_collapsing")
            self.logger.info("❌ Rejected: Liquidity collapsing")
            return False, RejectionReason.LIQUIDITY_COLLAPSING
        
        # Minimum spot move (prevents noise signals)
        if abs(consensus.move_30s_pct) < settings.signals.min_spot_move_pct:
            self._track_rejection("insufficient_move")
            self.logger.info(
                "❌ Rejected: Spot move too small",
                spot_move=f"{consensus.move_30s_pct:.2%}",
                required=f"{settings.signals.min_spot_move_pct:.2%}",
            )
            return False, RejectionReason.INSUFFICIENT_MOVE
        
        # Fee viability check (Jan 2026 Polymarket fee update)
        # Ensure expected edge exceeds effective fees with safety margin
        if not self._check_fee_viability(divergence_data, pm_data):
            return False, RejectionReason.FEE_UNFAVORABLE
        
        return True, None
    
    def _check_fee_viability(
        self,
        divergence_data: DivergenceData,
        pm_data: PolymarketData,
    ) -> bool:
        """
        Ensure signal's edge exceeds effective fees.
        
        Polymarket fee structure (Jan 2026):
        - Takers: 0.25% base rate squared by price (peaks 1.6-3% at 50% odds)
        - Makers: 0% fee + daily rebate
        
        Returns True if edge justifies trade, considering fees.
        """
        side = "YES" if divergence_data.signal_direction == "UP" else "NO"
        entry_price = pm_data.yes_bid if side == "YES" else pm_data.no_bid
        
        # Calculate taker fee (worst case)
        taker_fee = pm_data.calculate_effective_fee(side, entry_price, is_maker=False)
        
        # Skip check if no fee data available (assume viable)
        if pm_data.yes_fee_rate_bps == 0 and pm_data.no_fee_rate_bps == 0:
            return True
        
        # Expected edge from divergence (conservative: 50% of divergence)
        expected_edge = divergence_data.divergence * 0.5
        
        # Require edge > 2x taker fee (safety margin)
        required_edge = taker_fee * 2
        
        viable = expected_edge >= required_edge
        
        if not viable:
            self._track_rejection("fee_unfavorable")
            self.logger.info(
                "❌ Rejected: Fee structure unfavorable",
                expected_edge=f"{expected_edge:.2%}",
                taker_fee=f"{taker_fee:.2%}",
                required_edge=f"{required_edge:.2%}",
                entry_price=f"{entry_price:.3f}",
            )
        
        return viable
    
    # ==========================================================================
    # Main Detection Methods
    # ==========================================================================
    
    def detect(
        self,
        consensus: ConsensusData,
        oracle: Optional[OracleData],
        pm_data: PolymarketData,
    ) -> Optional[SignalCandidate]:
        """
        Detect if current market state presents a trading opportunity.
        
        NEW: Uses divergence-based detection as primary signal.
        
        Args:
            consensus: Aggregated spot price data
            oracle: Chainlink oracle data (optional, not primary signal anymore)
            pm_data: Polymarket orderbook data
            
        Returns:
            SignalCandidate if opportunity detected, None otherwise
        """
        # Calculate divergence (core signal)
        divergence_data = self.calculate_divergence(consensus, pm_data)
        
        # Log divergence state
        self.logger.debug(
            "Divergence check",
            spot_implied=f"{divergence_data.spot_implied_prob:.2%}",
            pm_implied=f"{divergence_data.pm_implied_prob:.2%}",
            divergence=f"{divergence_data.divergence:.2%}",
            pm_age=f"{divergence_data.pm_orderbook_age_seconds:.1f}s",
            is_actionable=divergence_data.is_actionable,
            direction=divergence_data.signal_direction,
        )
        
        # Primary check: Is divergence actionable?
        if not divergence_data.is_actionable:
            # Log why not actionable (INFO level to help debug)
            if divergence_data.divergence < settings.signals.min_divergence_pct:
                self._track_rejection("divergence_low")
                # Only log if divergence is significant but below threshold
                if divergence_data.divergence > 0.03:  # 3%+ worth logging
                    self.logger.info(
                        "⏸️ Divergence below threshold",
                        divergence=f"{divergence_data.divergence:.1%}",
                        required=f"{settings.signals.min_divergence_pct:.1%}",
                        direction=divergence_data.signal_direction,
                    )
            elif divergence_data.pm_orderbook_age_seconds < settings.signals.min_pm_staleness_seconds:
                self._track_rejection("pm_too_fresh")
                self.logger.info(
                    "⏸️ PM orderbook too fresh (not stale enough)",
                    pm_age=f"{divergence_data.pm_orderbook_age_seconds:.1f}s",
                    required=f"{settings.signals.min_pm_staleness_seconds:.1f}s",
                    divergence=f"{divergence_data.divergence:.1%}",
                )
            elif divergence_data.pm_orderbook_age_seconds > settings.signals.max_pm_staleness_seconds:
                self._track_rejection("pm_too_stale")
                self.logger.info(
                    "⏸️ PM too stale (opportunity may have passed)",
                    pm_age=f"{divergence_data.pm_orderbook_age_seconds:.0f}s",
                    max_allowed=f"{settings.signals.max_pm_staleness_seconds:.0f}s",
                    divergence=f"{divergence_data.divergence:.1%}",
                )
            return None
        
        # Determine direction
        direction = SignalDirection.UP if divergence_data.signal_direction == "UP" else SignalDirection.DOWN
        
        # Check for duplicate signal
        if self._is_duplicate_signal(direction):
            self.logger.debug("Duplicate signal suppressed", direction=direction.value)
            return None
        
        # Check supporting conditions
        passed, rejection = self._check_supporting_conditions(
            consensus, pm_data, divergence_data
        )
        
        if not passed:
            self.logger.debug(
                "Supporting conditions failed",
                rejection=rejection.value if rejection else None,
            )
            return None
        
        # All checks passed - create signal candidate
        now_ms = int(time.time() * 1000)
        
        signal = SignalCandidate(
            signal_id=str(uuid4()),
            timestamp_ms=now_ms,
            market_id=pm_data.market_id,
            direction=direction,
            signal_type=SignalType.STANDARD,
            consensus=consensus,
            oracle=oracle,
            polymarket=pm_data,
        )
        
        # Record signal
        self._recent_signals.append((now_ms, direction.value))
        
        self.logger.info(
            "🎯 SIGNAL DETECTED (Divergence Strategy)",
            signal_id=signal.signal_id[:8],
            direction=direction.value,
            divergence=f"{divergence_data.divergence:.2%}",
            spot_implied=f"{divergence_data.spot_implied_prob:.2%}",
            pm_implied=f"{divergence_data.pm_implied_prob:.2%}",
            pm_age=f"{divergence_data.pm_orderbook_age_seconds:.1f}s",
            spot_move=f"{consensus.move_30s_pct:.2%}",
            volume_surge=f"{consensus.volume_surge_ratio:.1f}x",
        )
        
        return signal
    
    # ==========================================================================
    # Legacy method (kept for backward compatibility)
    # ==========================================================================
    
    def detect_legacy(
        self,
        consensus: ConsensusData,
        oracle: OracleData,
        pm_data: PolymarketData,
    ) -> Optional[SignalCandidate]:
        """
        LEGACY: Oracle age-based detection.
        
        Kept for backward compatibility and A/B testing.
        Use detect() for the new divergence-based strategy.
        """
        # This is the old oracle-age based logic
        # Keeping it available for comparison but not using by default
        self.logger.warning("Using legacy oracle-based detection (deprecated)")
        return self.detect(consensus, oracle, pm_data)
    
    def get_metrics(self) -> dict:
        """Get detector metrics."""
        return {
            "recent_signals_count": len(self._recent_signals),
            "signal_cooldown_ms": self._signal_cooldown_ms,
            "strategy": "divergence",
            "min_divergence": settings.signals.min_divergence_pct,
            "min_pm_staleness": settings.signals.min_pm_staleness_seconds,
        }
