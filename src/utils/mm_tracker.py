"""
Market Maker Lag Tracker.

Tracks how quickly market makers respond to oracle updates
and provides confidence adjustments based on whether we're
ahead of or behind their typical response time.
"""

import time
from collections import deque
from datetime import datetime
from typing import Optional

import numpy as np
import structlog

logger = structlog.get_logger()


class MarketMakerTracker:
    """
    Track market maker response times to oracle updates.
    
    The key insight is that market makers (MMs) also watch the oracle
    and reprice when it updates. If we can predict their response time,
    we can:
    - Exit before they reprice (reduce slippage)
    - Only trade when we're faster than their typical lag
    
    Response patterns vary by:
    - Time of day (fewer MMs active at night)
    - Market volatility (faster response in vol spikes)
    - Day of week (weekends may be slower)
    
    Usage:
        tracker = MarketMakerTracker()
        
        # When oracle updates and we see odds change
        tracker.record_response(oracle_update_time_ms, odds_change_time_ms)
        
        # Get expected lag for current conditions
        expected_lag = tracker.get_expected_lag_ms()
        
        # Get score for confidence adjustment
        score = tracker.get_mm_lag_score(oracle_age_seconds)
    """
    
    # Default expected lag when insufficient data
    DEFAULT_LAG_MS = 8000  # 8 seconds
    
    # History size
    MAX_HISTORY = 200
    
    def __init__(self):
        self.logger = logger.bind(component="mm_tracker")
        
        # Response history: oracle_update -> odds_change lag
        self._response_history: deque[dict] = deque(maxlen=self.MAX_HISTORY)
        
        # Aggregated stats by hour
        self._hourly_stats: dict[int, list[float]] = {h: [] for h in range(24)}
        
        # Recent update detection
        self._last_oracle_price: float = 0.0
        self._last_oracle_update_ms: int = 0
        self._last_pm_yes_bid: float = 0.0
        self._last_pm_update_ms: int = 0
    
    def record_response(
        self,
        oracle_update_time_ms: int,
        odds_change_time_ms: int,
        change_magnitude: float = 0.0,
    ) -> None:
        """
        Record a market maker response to an oracle update.
        
        Args:
            oracle_update_time_ms: When the oracle price updated
            odds_change_time_ms: When Polymarket odds changed significantly
            change_magnitude: Size of the odds change (0-1)
        """
        lag_ms = odds_change_time_ms - oracle_update_time_ms
        
        if lag_ms < 0 or lag_ms > 60000:  # Sanity check: 0-60s
            return
        
        dt = datetime.fromtimestamp(odds_change_time_ms / 1000)
        hour = dt.hour
        day_of_week = dt.weekday()
        
        entry = {
            'lag_ms': lag_ms,
            'timestamp_ms': odds_change_time_ms,
            'hour': hour,
            'day_of_week': day_of_week,
            'change_magnitude': change_magnitude,
        }
        
        self._response_history.append(entry)
        self._hourly_stats[hour].append(lag_ms)
        
        # Trim hourly stats to last 50 entries per hour
        if len(self._hourly_stats[hour]) > 50:
            self._hourly_stats[hour] = self._hourly_stats[hour][-50:]
        
        self.logger.debug(
            "Recorded MM response",
            lag_ms=lag_ms,
            hour=hour,
            change_magnitude=change_magnitude,
        )
    
    def detect_response(
        self,
        oracle_price: float,
        oracle_update_ms: int,
        pm_yes_bid: float,
    ) -> bool:
        """
        Automatically detect and record MM responses.
        
        Call this periodically with current oracle and PM state.
        Returns True if a response was detected.
        
        Args:
            oracle_price: Current oracle price
            oracle_update_ms: Oracle last update timestamp
            pm_yes_bid: Current Polymarket YES bid price
        """
        now_ms = int(time.time() * 1000)
        detected = False
        
        # Detect oracle update (price changed)
        if (oracle_update_ms > self._last_oracle_update_ms and 
            abs(oracle_price - self._last_oracle_price) > 0):
            self._last_oracle_update_ms = oracle_update_ms
            self._last_oracle_price = oracle_price
        
        # Detect significant PM odds change (>1%)
        pm_change = abs(pm_yes_bid - self._last_pm_yes_bid)
        if pm_change > 0.01 and self._last_pm_yes_bid > 0:
            # Record the response if oracle recently updated
            time_since_oracle = now_ms - self._last_oracle_update_ms
            if 0 < time_since_oracle < 30000:  # Within 30s of oracle update
                self.record_response(
                    oracle_update_time_ms=self._last_oracle_update_ms,
                    odds_change_time_ms=now_ms,
                    change_magnitude=pm_change,
                )
                detected = True
        
        # Update PM tracking
        if pm_change > 0.01:
            self._last_pm_yes_bid = pm_yes_bid
            self._last_pm_update_ms = now_ms
        
        return detected
    
    def get_expected_lag_ms(
        self,
        hour: Optional[int] = None,
        percentile: int = 50,
    ) -> float:
        """
        Get expected MM response lag for given conditions.
        
        Args:
            hour: Hour of day (0-23), defaults to current
            percentile: Which percentile to return (50=median)
            
        Returns:
            Expected lag in milliseconds
        """
        if hour is None:
            hour = datetime.now().hour
        
        # Try hour-specific data first
        hour_data = self._hourly_stats.get(hour, [])
        
        # Include adjacent hours if insufficient data
        if len(hour_data) < 5:
            adjacent_hours = [(hour - 1) % 24, (hour + 1) % 24]
            for h in adjacent_hours:
                hour_data.extend(self._hourly_stats.get(h, []))
        
        if len(hour_data) < 5:
            # Use all data if still insufficient
            hour_data = [r['lag_ms'] for r in self._response_history]
        
        if not hour_data:
            return self.DEFAULT_LAG_MS
        
        return float(np.percentile(hour_data, percentile))
    
    def get_mm_lag_score(
        self,
        oracle_age_seconds: float,
        hour: Optional[int] = None,
    ) -> float:
        """
        Calculate score based on whether we're ahead of MMs.
        
        Returns 0.0 - 1.0 where:
        - 1.0 = We're very early (oracle age < 50% of expected MM lag)
        - 0.7 = We're early (oracle age < expected MM lag)
        - 0.4 = We're borderline (oracle age < 1.5x MM lag)
        - 0.0 = We're late (oracle age > 1.5x MM lag)
        
        Args:
            oracle_age_seconds: Current age of oracle data
            hour: Hour for context (defaults to current)
            
        Returns:
            Score from 0.0 to 1.0
        """
        expected_lag_ms = self.get_expected_lag_ms(hour)
        oracle_age_ms = oracle_age_seconds * 1000
        
        if oracle_age_ms < 0.5 * expected_lag_ms:
            return 1.0  # Very early - excellent
        elif oracle_age_ms < expected_lag_ms:
            return 0.7  # Early - good
        elif oracle_age_ms < 1.5 * expected_lag_ms:
            return 0.4  # Borderline
        else:
            return 0.0  # Too late - MMs likely already repriced
    
    def get_stats(self, hour: Optional[int] = None) -> dict:
        """
        Get statistics for debugging and monitoring.
        
        Args:
            hour: Hour to get stats for (defaults to current)
        """
        if hour is None:
            hour = datetime.now().hour
        
        hour_data = self._hourly_stats.get(hour, [])
        all_data = [r['lag_ms'] for r in self._response_history]
        
        def calc_stats(data: list) -> dict:
            if not data:
                return {
                    'count': 0,
                    'mean': 0,
                    'median': 0,
                    'p25': 0,
                    'p75': 0,
                    'min': 0,
                    'max': 0,
                }
            return {
                'count': len(data),
                'mean': float(np.mean(data)),
                'median': float(np.median(data)),
                'p25': float(np.percentile(data, 25)),
                'p75': float(np.percentile(data, 75)),
                'min': float(np.min(data)),
                'max': float(np.max(data)),
            }
        
        return {
            'current_hour': hour,
            'hour_stats': calc_stats(hour_data),
            'overall_stats': calc_stats(all_data),
            'expected_lag_ms': self.get_expected_lag_ms(hour),
            'total_observations': len(self._response_history),
        }
    
    def get_hourly_summary(self) -> dict[int, dict]:
        """Get summary statistics for all hours."""
        summary = {}
        for hour in range(24):
            data = self._hourly_stats.get(hour, [])
            if data:
                summary[hour] = {
                    'count': len(data),
                    'median_lag_ms': float(np.median(data)),
                    'mean_lag_ms': float(np.mean(data)),
                }
            else:
                summary[hour] = {
                    'count': 0,
                    'median_lag_ms': self.DEFAULT_LAG_MS,
                    'mean_lag_ms': self.DEFAULT_LAG_MS,
                }
        return summary
    
    def generate_report(self) -> str:
        """Generate human-readable report of MM lag patterns."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║           MARKET MAKER LAG ANALYSIS                          ║",
            "╠══════════════════════════════════════════════════════════════╣",
        ]
        
        stats = self.get_stats()
        lines.append(f"║ Total Observations: {stats['total_observations']:>6}".ljust(63) + "║")
        lines.append(f"║ Expected Lag (current hour): {stats['expected_lag_ms']/1000:.1f}s".ljust(63) + "║")
        lines.append("╠══════════════════════════════════════════════════════════════╣")
        lines.append("║ HOURLY BREAKDOWN (median lag in seconds)                     ║")
        lines.append("║──────────────────────────────────────────────────────────────║")
        
        summary = self.get_hourly_summary()
        for hour in range(24):
            data = summary[hour]
            if data['count'] > 0:
                line = f"║  {hour:02d}:00  │  {data['median_lag_ms']/1000:5.1f}s  │  n={data['count']:3d}".ljust(63) + "║"
            else:
                line = f"║  {hour:02d}:00  │    -    │  n=  0".ljust(63) + "║"
            lines.append(line)
        
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        
        return "\n".join(lines)
    
    def get_metrics(self) -> dict:
        """Get metrics for monitoring."""
        current_hour = datetime.now().hour
        return {
            'total_observations': len(self._response_history),
            'current_hour': current_hour,
            'expected_lag_ms': self.get_expected_lag_ms(current_hour),
            'hourly_summary': self.get_hourly_summary(),
        }

