"""
Market Intelligence System.

Advanced analytics for win rate optimization:
- Market Maker lag tracking
- Oracle update prediction
- Time-of-day analysis
- Order flow tracking
- Ensemble confidence filtering
"""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np

import structlog

from src.models.schemas import SignalCandidate, SignalDirection

logger = structlog.get_logger()


# --- Market Maker Lag Tracking ---

@dataclass
class MMResponseRecord:
    """Record of market maker response to oracle update."""
    oracle_update_time_ms: int
    odds_change_time_ms: int
    lag_ms: int
    hour_of_day: int


class MarketMakerTracker:
    """
    Track how quickly market makers respond to oracle updates.
    
    This helps us understand:
    - How much time we have after oracle updates
    - When competition is highest/lowest
    - Expected lag by time of day
    """
    
    def __init__(self, max_records: int = 200):
        self.logger = logger.bind(component="mm_tracker")
        self.response_history: deque[MMResponseRecord] = deque(maxlen=max_records)
        self._default_lag_ms = 8000  # 8 seconds default
    
    def record_response(
        self,
        oracle_update_time_ms: int,
        odds_change_time_ms: int,
    ) -> None:
        """Record time between oracle update and odds change."""
        lag = odds_change_time_ms - oracle_update_time_ms
        
        if lag < 0 or lag > 120_000:  # Sanity check: 0-120s
            return
        
        hour = datetime.fromtimestamp(odds_change_time_ms / 1000).hour
        
        self.response_history.append(MMResponseRecord(
            oracle_update_time_ms=oracle_update_time_ms,
            odds_change_time_ms=odds_change_time_ms,
            lag_ms=lag,
            hour_of_day=hour,
        ))
        
        self.logger.debug(
            "MM response recorded",
            lag_ms=lag,
            hour=hour,
        )
    
    def get_expected_lag_ms(self, current_hour: Optional[int] = None) -> float:
        """
        Get expected MM response lag for current time.
        
        Args:
            current_hour: Hour of day (0-23), uses current if not specified
            
        Returns:
            Expected lag in milliseconds
        """
        if current_hour is None:
            current_hour = datetime.now().hour
        
        # Filter to similar time window (±2 hours)
        relevant = [
            r for r in self.response_history
            if abs(r.hour_of_day - current_hour) <= 2 or 
               abs(r.hour_of_day - current_hour) >= 22  # Handle midnight wrap
        ]
        
        if len(relevant) < 10:
            return self._default_lag_ms
        
        lags = [r.lag_ms for r in relevant]
        return float(np.median(lags))
    
    def get_mm_lag_score(
        self,
        oracle_age_seconds: float,
        current_hour: Optional[int] = None,
    ) -> float:
        """
        Score based on whether we're ahead of expected MM response.
        
        Returns:
            Score 0-1:
            - 1.0: We're very early (oracle_age < 50% of expected lag)
            - 0.7: We're early (oracle_age < expected lag)
            - 0.4: We're borderline (oracle_age < 150% of expected lag)
            - 0.0: We're late (oracle_age > 150% of expected lag)
        """
        expected_lag_ms = self.get_expected_lag_ms(current_hour)
        oracle_age_ms = oracle_age_seconds * 1000
        
        if oracle_age_ms < 0.5 * expected_lag_ms:
            return 1.0  # Very early
        elif oracle_age_ms < expected_lag_ms:
            return 0.7  # Early
        elif oracle_age_ms < 1.5 * expected_lag_ms:
            return 0.4  # Borderline
        else:
            return 0.0  # Late
    
    def get_metrics(self) -> dict:
        """Get tracker metrics."""
        if not self.response_history:
            return {
                "records": 0,
                "avg_lag_ms": self._default_lag_ms,
                "median_lag_ms": self._default_lag_ms,
            }
        
        lags = [r.lag_ms for r in self.response_history]
        return {
            "records": len(self.response_history),
            "avg_lag_ms": np.mean(lags),
            "median_lag_ms": np.median(lags),
            "min_lag_ms": min(lags),
            "max_lag_ms": max(lags),
        }


# --- Oracle Update Prediction ---

@dataclass
class OracleUpdateRecord:
    """Record of an oracle update event."""
    timestamp_ms: int
    trigger_type: str  # 'heartbeat' or 'deviation'
    deviation_pct: float


class OracleUpdatePredictor:
    """
    Predict next oracle update time.
    
    Chainlink oracles update based on:
    1. Heartbeat interval (typically 60s for BTC/USD)
    2. Price deviation threshold (typically 0.5%)
    
    This helps optimize entry and exit timing.
    """
    
    def __init__(self, max_records: int = 100):
        self.logger = logger.bind(component="oracle_predictor")
        self.update_history: deque[OracleUpdateRecord] = deque(maxlen=max_records)
        self._default_heartbeat_s = 60.0
        self._deviation_threshold = 0.005  # 0.5%
    
    def record_update(
        self,
        timestamp_ms: int,
        trigger_type: str = "unknown",
        deviation_pct: float = 0.0,
    ) -> None:
        """Record oracle update."""
        self.update_history.append(OracleUpdateRecord(
            timestamp_ms=timestamp_ms,
            trigger_type=trigger_type,
            deviation_pct=deviation_pct,
        ))
    
    def get_typical_heartbeat_s(self) -> float:
        """Calculate typical heartbeat interval in seconds."""
        if len(self.update_history) < 5:
            return self._default_heartbeat_s
        
        intervals = []
        records = list(self.update_history)
        for i in range(1, len(records)):
            interval = (records[i].timestamp_ms - records[i-1].timestamp_ms) / 1000
            if 10 < interval < 300:  # Sanity check: 10s-5min
                intervals.append(interval)
        
        if not intervals:
            return self._default_heartbeat_s
        
        return float(np.median(intervals))
    
    def predict_next_update(
        self,
        current_oracle_age_s: float,
        current_deviation_pct: float,
    ) -> dict:
        """
        Predict when oracle will update next.
        
        Args:
            current_oracle_age_s: Current age of oracle data in seconds
            current_deviation_pct: Current price deviation from oracle (abs)
            
        Returns:
            Dict with prediction details
        """
        typical_heartbeat = self.get_typical_heartbeat_s()
        
        # Check if deviation-based trigger is likely
        if current_deviation_pct >= self._deviation_threshold:
            # Oracle will likely update within 10s due to deviation
            predicted_seconds = max(5, 10 - current_oracle_age_s)
            confidence = 0.9
            trigger = 'deviation'
        else:
            # Oracle will update on heartbeat
            time_since_last = current_oracle_age_s
            predicted_seconds = max(5, typical_heartbeat - time_since_last)
            confidence = 0.7
            trigger = 'heartbeat'
        
        return {
            'predicted_seconds_until_update': predicted_seconds,
            'confidence': confidence,
            'trigger_type': trigger,
            'typical_heartbeat_s': typical_heartbeat,
        }
    
    def is_update_imminent(
        self,
        current_oracle_age_s: float,
        current_deviation_pct: float,
        threshold_s: float = 15.0,
    ) -> bool:
        """
        Check if oracle update is imminent.
        
        Args:
            current_oracle_age_s: Current oracle age
            current_deviation_pct: Current deviation
            threshold_s: Time threshold in seconds
            
        Returns:
            True if update is likely within threshold_s
        """
        prediction = self.predict_next_update(current_oracle_age_s, current_deviation_pct)
        
        return (
            prediction['predicted_seconds_until_update'] < threshold_s and
            prediction['confidence'] > 0.6
        )
    
    def get_metrics(self) -> dict:
        """Get predictor metrics."""
        return {
            "records": len(self.update_history),
            "typical_heartbeat_s": self.get_typical_heartbeat_s(),
            "deviation_threshold": self._deviation_threshold,
        }


# --- Time-of-Day Analysis ---

@dataclass
class TimeOfDayStats:
    """Statistics for a specific hour."""
    hour: int
    wins: int
    losses: int
    total_profit: float
    
    @property
    def total(self) -> int:
        return self.wins + self.losses
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0


class TimeOfDayAnalyzer:
    """
    Analyze win rate by hour of day.
    
    Market maker activity varies by time:
    - 02:00-06:00: Low competition (night mode optimal)
    - 09:00-17:00: High competition (US/EU trading hours)
    - 17:00-22:00: Medium competition
    """
    
    def __init__(self):
        self.logger = logger.bind(component="time_analyzer")
        self.hour_stats: dict[int, TimeOfDayStats] = {
            h: TimeOfDayStats(hour=h, wins=0, losses=0, total_profit=0.0)
            for h in range(24)
        }
    
    def record_outcome(
        self,
        timestamp_ms: int,
        won: bool,
        profit_eur: float = 0.0,
    ) -> None:
        """Record signal outcome for time analysis."""
        hour = datetime.fromtimestamp(timestamp_ms / 1000).hour
        stats = self.hour_stats[hour]
        
        if won:
            stats.wins += 1
        else:
            stats.losses += 1
        stats.total_profit += profit_eur
    
    def get_favorable_hours(
        self,
        min_win_rate: float = 0.70,
        min_samples: int = 10,
    ) -> list[int]:
        """Get hours with win rate above threshold."""
        favorable = []
        
        for hour, stats in self.hour_stats.items():
            if stats.total >= min_samples and stats.win_rate >= min_win_rate:
                favorable.append(hour)
        
        return sorted(favorable)
    
    def get_hour_confidence_multiplier(
        self,
        hour: Optional[int] = None,
        min_samples: int = 5,
    ) -> float:
        """
        Get confidence multiplier for current hour.
        
        Returns:
            1.0 for favorable hours
            0.85 for neutral hours
            0.70 for unfavorable hours
        """
        if hour is None:
            hour = datetime.now().hour
        
        stats = self.hour_stats.get(hour)
        if not stats or stats.total < min_samples:
            return 0.90  # Neutral - not enough data
        
        if stats.win_rate >= 0.70:
            return 1.0
        elif stats.win_rate >= 0.55:
            return 0.85
        else:
            return 0.70
    
    def get_stats_summary(self) -> dict:
        """Get summary of time-of-day statistics."""
        summary = {}
        for hour, stats in self.hour_stats.items():
            if stats.total > 0:
                summary[hour] = {
                    "wins": stats.wins,
                    "losses": stats.losses,
                    "win_rate": stats.win_rate,
                    "total_profit": stats.total_profit,
                }
        return summary


# --- Order Flow Tracking ---

@dataclass
class LargeOrderRecord:
    """Record of a large order fill."""
    timestamp_ms: int
    side: str  # 'BUY' or 'SELL'
    size_eur: float
    price_impact_pct: float


class OrderFlowTracker:
    """
    Track large orders and their impact.
    
    Large orders hitting the book predict price continuation.
    This provides order flow confirmation for signals.
    """
    
    def __init__(self, max_records: int = 50, large_order_threshold: float = 1000.0):
        self.logger = logger.bind(component="order_flow")
        self.recent_orders: deque[LargeOrderRecord] = deque(maxlen=max_records)
        self.large_order_threshold = large_order_threshold  # EUR
    
    def record_order(
        self,
        side: str,
        size_eur: float,
        price_impact_pct: float = 0.0,
    ) -> None:
        """Record a large order fill."""
        if size_eur < self.large_order_threshold:
            return
        
        self.recent_orders.append(LargeOrderRecord(
            timestamp_ms=int(time.time() * 1000),
            side=side.upper(),
            size_eur=size_eur,
            price_impact_pct=price_impact_pct,
        ))
    
    def get_order_flow_signal(
        self,
        signal_direction: SignalDirection,
        lookback_seconds: int = 30,
    ) -> float:
        """
        Get order flow confirmation score (0-1).
        
        Args:
            signal_direction: Direction of the signal
            lookback_seconds: How far back to look
            
        Returns:
            Score 0-1 based on order flow alignment
        """
        cutoff = int(time.time() * 1000) - (lookback_seconds * 1000)
        recent = [o for o in self.recent_orders if o.timestamp_ms > cutoff]
        
        if not recent:
            return 0.5  # Neutral
        
        # Count orders in signal direction
        if signal_direction == SignalDirection.UP:
            favorable_orders = [o for o in recent if o.side == 'BUY']
        else:
            favorable_orders = [o for o in recent if o.side == 'SELL']
        
        favorable_volume = sum(o.size_eur for o in favorable_orders)
        total_volume = sum(o.size_eur for o in recent)
        
        if total_volume == 0:
            return 0.5
        
        return favorable_volume / total_volume
    
    def get_metrics(self) -> dict:
        """Get tracker metrics."""
        if not self.recent_orders:
            return {"records": 0}
        
        buy_volume = sum(o.size_eur for o in self.recent_orders if o.side == 'BUY')
        sell_volume = sum(o.size_eur for o in self.recent_orders if o.side == 'SELL')
        
        return {
            "records": len(self.recent_orders),
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "net_flow": buy_volume - sell_volume,
        }


# --- Ensemble Confidence Filter ---

class EnsembleFilter:
    """
    Multi-model confirmation system.
    
    Signals that pass MULTIPLE independent confirmation models
    have higher win rates.
    """
    
    def __init__(self):
        self.logger = logger.bind(component="ensemble_filter")
    
    def check_volume_momentum(self, signal: SignalCandidate) -> bool:
        """Model: volume surge + price move = continuation."""
        if not signal.consensus:
            return False
        
        return (
            signal.consensus.volume_surge_ratio >= 2.5 and
            abs(signal.consensus.move_30s_pct) >= 0.008
        )
    
    def check_orderbook_pressure(self, signal: SignalCandidate) -> bool:
        """Model: orderbook imbalance predicts direction."""
        if not signal.polymarket:
            return False
        
        imbalance = signal.polymarket.orderbook_imbalance_ratio
        
        if signal.direction == SignalDirection.UP:
            # Betting UP: want NO-heavy (negative imbalance = less competition)
            return imbalance < -0.15
        else:
            # Betting DOWN: want YES-heavy (positive imbalance = less competition)
            return imbalance > 0.15
    
    def check_price_velocity(self, signal: SignalCandidate) -> bool:
        """Model: accelerating price = strong move."""
        if not signal.consensus:
            return False
        
        # If spike concentration >65%, price is accelerating
        return signal.consensus.spike_concentration > 0.65
    
    def check_oracle_timing(self, signal: SignalCandidate) -> bool:
        """Model: optimal oracle window."""
        if not signal.oracle:
            return False
        
        return 20 <= signal.oracle.oracle_age_seconds <= 60
    
    def get_ensemble_confirmation(self, signal: SignalCandidate) -> dict:
        """
        Get confirmation from multiple models.
        
        Returns:
            Dict with model confirmations and overall pass/fail
        """
        confirmations = {
            'volume_momentum': self.check_volume_momentum(signal),
            'orderbook_pressure': self.check_orderbook_pressure(signal),
            'price_velocity': self.check_price_velocity(signal),
            'oracle_timing': self.check_oracle_timing(signal),
        }
        
        total_confirmations = sum(confirmations.values())
        ensemble_confidence = total_confirmations / len(confirmations)
        
        # Require at least 3/4 models to agree for full confidence
        passes = total_confirmations >= 3
        
        return {
            'confirmations': confirmations,
            'ensemble_confidence': ensemble_confidence,
            'passes': passes,
            'total_confirmations': total_confirmations,
        }
    
    def get_confidence_boost(self, signal: SignalCandidate) -> float:
        """
        Get confidence boost/penalty based on ensemble.
        
        Returns:
            Multiplier (0.8-1.1):
            - 1.1: All models agree
            - 1.0: 3/4 models agree
            - 0.9: 2/4 models agree
            - 0.8: <2 models agree
        """
        result = self.get_ensemble_confirmation(signal)
        total = result['total_confirmations']
        
        if total == 4:
            return 1.10  # 10% boost
        elif total == 3:
            return 1.0   # No change
        elif total == 2:
            return 0.90  # 10% penalty
        else:
            return 0.80  # 20% penalty


# --- Combined Market Intelligence ---

class MarketIntelligence:
    """
    Combined market intelligence system.
    
    Integrates:
    - Market maker lag tracking
    - Oracle update prediction
    - Time-of-day analysis
    - Order flow tracking
    - Ensemble filtering
    """
    
    def __init__(self):
        self.logger = logger.bind(component="market_intelligence")
        
        self.mm_tracker = MarketMakerTracker()
        self.oracle_predictor = OracleUpdatePredictor()
        self.time_analyzer = TimeOfDayAnalyzer()
        self.order_flow = OrderFlowTracker()
        self.ensemble = EnsembleFilter()
    
    def get_intelligence_score(self, signal: SignalCandidate) -> dict:
        """
        Get comprehensive market intelligence for a signal.
        
        Returns combined scores and recommendations.
        """
        if not signal.oracle:
            return {
                'mm_lag_score': 0.5,
                'time_multiplier': 0.9,
                'order_flow_score': 0.5,
                'ensemble_boost': 1.0,
                'combined_multiplier': 1.0,
                'oracle_update_imminent': False,
            }
        
        current_hour = datetime.now().hour
        
        # Market maker lag score
        mm_lag_score = self.mm_tracker.get_mm_lag_score(
            signal.oracle.oracle_age_seconds,
            current_hour,
        )
        
        # Time-of-day multiplier
        time_multiplier = self.time_analyzer.get_hour_confidence_multiplier(current_hour)
        
        # Order flow score
        order_flow_score = self.order_flow.get_order_flow_signal(signal.direction)
        
        # Ensemble boost
        ensemble_boost = self.ensemble.get_confidence_boost(signal)
        
        # Oracle update prediction
        divergence_pct = 0.0
        if signal.consensus and signal.oracle:
            divergence_pct = abs(
                signal.consensus.consensus_price - signal.oracle.current_value
            ) / signal.oracle.current_value
        
        oracle_update_imminent = self.oracle_predictor.is_update_imminent(
            signal.oracle.oracle_age_seconds,
            divergence_pct,
        )
        
        # Combined multiplier
        # Weight: MM lag 30%, time 20%, order flow 10%, ensemble 40%
        combined_multiplier = (
            0.30 * (0.8 + 0.4 * mm_lag_score) +  # 0.8-1.2 range
            0.20 * time_multiplier +               # 0.7-1.0 range
            0.10 * (0.9 + 0.2 * order_flow_score) +  # 0.9-1.1 range
            0.40 * ensemble_boost                  # 0.8-1.1 range
        )
        
        return {
            'mm_lag_score': mm_lag_score,
            'time_multiplier': time_multiplier,
            'order_flow_score': order_flow_score,
            'ensemble_boost': ensemble_boost,
            'combined_multiplier': combined_multiplier,
            'oracle_update_imminent': oracle_update_imminent,
            'oracle_prediction': self.oracle_predictor.predict_next_update(
                signal.oracle.oracle_age_seconds,
                divergence_pct,
            ),
        }
    
    def record_signal_outcome(
        self,
        signal: SignalCandidate,
        won: bool,
        profit_eur: float = 0.0,
    ) -> None:
        """Record signal outcome for learning."""
        self.time_analyzer.record_outcome(
            signal.timestamp_ms,
            won,
            profit_eur,
        )
    
    def record_mm_response(
        self,
        oracle_update_time_ms: int,
        odds_change_time_ms: int,
    ) -> None:
        """Record market maker response timing."""
        self.mm_tracker.record_response(oracle_update_time_ms, odds_change_time_ms)
    
    def record_oracle_update(
        self,
        timestamp_ms: int,
        trigger_type: str = "unknown",
        deviation_pct: float = 0.0,
    ) -> None:
        """Record oracle update for prediction learning."""
        self.oracle_predictor.record_update(timestamp_ms, trigger_type, deviation_pct)
    
    def get_metrics(self) -> dict:
        """Get all market intelligence metrics."""
        return {
            'mm_tracker': self.mm_tracker.get_metrics(),
            'oracle_predictor': self.oracle_predictor.get_metrics(),
            'time_analyzer': self.time_analyzer.get_stats_summary(),
            'order_flow': self.order_flow.get_metrics(),
        }

