"""
Signal Validation Engine.
Performs additional checks before accepting a signal.
"""

import time
from typing import Optional

import structlog

from src.models.schemas import (
    SignalCandidate,
    ValidationResult,
    RejectionReason,
    VolatilityRegime,
)
from config.settings import settings

logger = structlog.get_logger()


class SignalHistoryTracker:
    """Tracks historical signal performance for win rate calculation."""
    
    def __init__(self, max_entries: int = 200):
        self.max_entries = max_entries
        self.entries: list[dict] = []
    
    def add_result(
        self,
        signal_type: str,
        direction: str,
        oracle_age: float,
        volatility_regime: str,
        won: bool,
    ) -> None:
        """Record a signal outcome."""
        self.entries.append({
            "timestamp_ms": int(time.time() * 1000),
            "signal_type": signal_type,
            "direction": direction,
            "oracle_age": oracle_age,
            "volatility_regime": volatility_regime,
            "won": won,
        })
        
        # Trim old entries
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]
    
    def get_win_rate(
        self,
        signal_type: Optional[str] = None,
        direction: Optional[str] = None,
        volatility_regime: Optional[str] = None,
        min_entries: int = 10,
    ) -> float:
        """Get win rate for similar signals."""
        filtered = self.entries
        
        if signal_type:
            filtered = [e for e in filtered if e["signal_type"] == signal_type]
        if direction:
            filtered = [e for e in filtered if e["direction"] == direction]
        if volatility_regime:
            filtered = [e for e in filtered if e["volatility_regime"] == volatility_regime]
        
        if len(filtered) < min_entries:
            return 0.65  # Return default target win rate if insufficient data
        
        wins = sum(1 for e in filtered if e["won"])
        return wins / len(filtered)


class Validator:
    """
    Validates signal candidates with additional checks.
    
    Validation checks:
    1. Directional persistence (momentum hasn't reversed)
    2. Liquidity reality (can actually fill position)
    3. Oracle update risk (not too close to heartbeat)
    4. Spread convergence (market not already correcting)
    5. Historical performance (similar signals worked before)
    6. Spike authentication (not smooth drift)
    7. Volume confirmation
    """
    
    def __init__(self):
        self.logger = logger.bind(component="validator")
        self.history = SignalHistoryTracker()
    
    def _check_directional_persistence(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """
        Verify signal direction is consistent.
        
        UPDATED: For divergence strategy, we check divergence magnitude
        not spot movement. Divergence IS the signal!
        """
        if not signal.consensus:
            return False, RejectionReason.CONSENSUS_FAILURE
        
        # DIVERGENCE STRATEGY: Check divergence not spot movement
        # If there's significant divergence, that IS the signal
        if signal.polymarket:
            from src.engine.signal_detector import calculate_spot_implied_prob
            
            spot_implied = calculate_spot_implied_prob(
                signal.consensus.move_30s_pct,
                scale=settings.signals.spot_implied_scale,
            )
            pm_implied = signal.polymarket.yes_bid
            divergence = abs(spot_implied - pm_implied)
            
            # If divergence is significant, pass the check
            if divergence >= settings.signals.min_divergence_pct:
                return True, None
        
        # Fallback: Legacy spot movement check
        overall_move = signal.consensus.move_30s_pct
        if abs(overall_move) >= settings.signals.escape_clause_min_move * 0.5:
            return True, None
        
        # Only reject if BOTH divergence AND movement are tiny
        return False, RejectionReason.DIRECTION_REVERSED
    
    def _check_liquidity_reality(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """Check if position can actually be filled with acceptable slippage."""
        if not signal.polymarket:
            return False, RejectionReason.LIQUIDITY_LOW
        
        pm = signal.polymarket
        
        # Check minimum liquidity at best price
        if pm.yes_liquidity_best < settings.signals.min_liquidity_eur:
            return False, RejectionReason.LIQUIDITY_LOW
        
        # Check depth at top 3 levels for €25 position
        total_depth = sum(level.size for level in pm.yes_depth_3)
        if total_depth < 25:
            self.logger.debug(
                "Insufficient depth",
                total_depth=total_depth,
                required=25,
            )
            return False, RejectionReason.LIQUIDITY_LOW
        
        # Calculate slippage for €25 position
        remaining_size = 25.0
        total_cost = 0.0
        
        for level in pm.yes_depth_3:
            take = min(remaining_size, level.size)
            total_cost += take * level.price
            remaining_size -= take
            if remaining_size <= 0:
                break
        
        if remaining_size > 0:
            return False, RejectionReason.SLIPPAGE_TOO_HIGH
        
        avg_price = total_cost / 25.0
        slippage = (avg_price - pm.yes_bid) / pm.yes_bid if pm.yes_bid > 0 else 1.0
        
        if slippage > settings.execution.max_slippage_pct:
            self.logger.debug(
                "Slippage too high",
                slippage=slippage,
                max_allowed=settings.execution.max_slippage_pct,
            )
            return False, RejectionReason.SLIPPAGE_TOO_HIGH
        
        return True, None
    
    def _check_liquidity_collapse(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """Check for sudden liquidity drain."""
        if not signal.polymarket:
            return False, RejectionReason.LIQUIDITY_COLLAPSING
        
        pm = signal.polymarket
        
        if pm.liquidity_collapsing:
            self.logger.warning(
                "Liquidity collapse detected",
                current=pm.yes_liquidity_best,
                historical=pm.liquidity_30s_ago,
            )
            return False, RejectionReason.LIQUIDITY_COLLAPSING
        
        return True, None
    
    def _check_oracle_update_risk(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """Check oracle isn't about to update."""
        if not signal.oracle or not signal.consensus:
            return False, RejectionReason.ORACLE_TOO_FRESH
        
        oracle = signal.oracle
        regime = signal.consensus.volatility_regime
        
        # Get appropriate age window
        if regime == VolatilityRegime.LOW:
            min_age = settings.chainlink.oracle_min_age_low_vol
        else:
            min_age = settings.chainlink.oracle_min_age_normal_vol
        
        # Check oracle not too fresh
        if oracle.oracle_age_seconds < min_age:
            return False, RejectionReason.ORACLE_TOO_FRESH
        
        # Check oracle not too stale (update imminent)
        if oracle.oracle_age_seconds > 70:  # Stricter than primary check
            return False, RejectionReason.ORACLE_TOO_STALE
        
        # Check for fast heartbeat mode
        if oracle.is_fast_heartbeat_mode:
            recent_avg = sum(oracle.recent_heartbeat_intervals) / len(oracle.recent_heartbeat_intervals) if oracle.recent_heartbeat_intervals else 60
            if recent_avg < settings.chainlink.fast_heartbeat_threshold:
                return False, RejectionReason.FAST_HEARTBEAT_MODE
        
        return True, None
    
    def _check_spread_convergence(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """
        Check if there's room for profitable trade.
        
        UPDATED: Tight spread is GOOD for execution!
        Only reject if spread is unrealistically tight (0.1%)
        """
        if not signal.polymarket:
            return True, None  # No PM data = can't check, pass it
        
        pm = signal.polymarket
        
        # Only reject if spread is impossibly tight (likely stale data)
        if pm.spread < 0.001:  # 0.1% - almost no spread
            self.logger.debug(
                "Spread impossibly tight (stale data?)",
                spread=pm.spread,
            )
            return False, RejectionReason.SPREAD_CONVERGING
        
        # Tight spreads (1-2%) are GOOD for execution - don't reject!
        return True, None
    
    def _check_historical_performance(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, float, Optional[RejectionReason]]:
        """Check similar signals have worked before."""
        if not signal.consensus:
            return False, 0.0, RejectionReason.HISTORICAL_WIN_RATE_LOW
        
        # Get win rate for similar signals
        win_rate = self.history.get_win_rate(
            signal_type=signal.signal_type.value,
            direction=signal.direction.value,
            volatility_regime=signal.consensus.volatility_regime.value,
        )
        
        # Require 60% minimum win rate
        if win_rate < 0.60:
            self.logger.debug(
                "Historical win rate too low",
                win_rate=win_rate,
                required=0.60,
            )
            return False, win_rate, RejectionReason.HISTORICAL_WIN_RATE_LOW
        
        return True, win_rate, None
    
    def _check_volume_authentication(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """Verify volume surge authenticates the move."""
        if not signal.consensus:
            return False, RejectionReason.VOLUME_LOW
        
        if signal.consensus.volume_surge_ratio < settings.signals.volume_surge_threshold:
            return False, RejectionReason.VOLUME_LOW
        
        return True, None
    
    def _check_spike_concentration(
        self,
        signal: SignalCandidate,
    ) -> tuple[bool, Optional[RejectionReason]]:
        """Verify move is spike-like, not smooth drift."""
        if not signal.consensus:
            return False, RejectionReason.SMOOTH_DRIFT
        
        if signal.consensus.spike_concentration < settings.signals.spike_concentration_threshold:
            return False, RejectionReason.SMOOTH_DRIFT
        
        return True, None
    
    def validate(self, signal: SignalCandidate) -> ValidationResult:
        """
        Run all validation checks on a signal candidate.
        
        Args:
            signal: The signal candidate to validate
            
        Returns:
            ValidationResult with pass/fail status and details
        """
        self.logger.debug("Validating signal", signal_id=signal.signal_id)
        
        # Initialize result
        result = ValidationResult(passed=True)
        
        # Run all checks
        checks = [
            ("directional_persistence", self._check_directional_persistence),
            ("liquidity_sufficient", self._check_liquidity_reality),
            ("liquidity_not_collapsing", self._check_liquidity_collapse),
            ("oracle_window_safe", self._check_oracle_update_risk),
            ("spread_not_converging", self._check_spread_convergence),
            ("volume_authenticated", self._check_volume_authentication),
            ("spike_not_smooth_drift", self._check_spike_concentration),
        ]
        
        for check_name, check_func in checks:
            passed, rejection = check_func(signal)
            setattr(result, check_name, passed)
            
            if not passed:
                result.passed = False
                result.rejection_reason = rejection
                self.logger.info(
                    "Validation failed",
                    signal_id=signal.signal_id,
                    check=check_name,
                    reason=rejection.value if rejection else None,
                )
                # Continue checking to log all failures
        
        # Historical performance check (also returns win rate)
        hist_passed, win_rate, hist_rejection = self._check_historical_performance(signal)
        result.historical_win_rate = win_rate
        
        if not hist_passed:
            result.passed = False
            if not result.rejection_reason:
                result.rejection_reason = hist_rejection
        
        if result.passed:
            self.logger.info(
                "Validation passed",
                signal_id=signal.signal_id,
                historical_win_rate=win_rate,
            )
        
        return result
    
    def record_outcome(
        self,
        signal: SignalCandidate,
        won: bool,
    ) -> None:
        """Record signal outcome for historical tracking."""
        if not signal.oracle or not signal.consensus:
            return
        
        self.history.add_result(
            signal_type=signal.signal_type.value,
            direction=signal.direction.value,
            oracle_age=signal.oracle.oracle_age_seconds,
            volatility_regime=signal.consensus.volatility_regime.value,
            won=won,
        )
    
    def get_metrics(self) -> dict:
        """Get validator metrics."""
        return {
            "history_entries": len(self.history.entries),
            "overall_win_rate": self.history.get_win_rate(min_entries=5),
            "standard_win_rate": self.history.get_win_rate(signal_type="standard", min_entries=5),
            "escape_clause_win_rate": self.history.get_win_rate(signal_type="escape_clause", min_entries=5),
        }

