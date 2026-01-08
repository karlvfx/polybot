"""
Consensus Engine for multi-exchange price aggregation.
Implements weighted averaging with outlier rejection.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from statistics import median

import structlog

from src.models.schemas import (
    ConsensusData,
    ExchangeMetrics,
    VolatilityRegime,
)
from config.settings import settings

logger = structlog.get_logger()


@dataclass
class ATRHistory:
    """Tracks ATR values for percentile calculation."""
    values: list[float]
    max_size: int = 1000
    
    def add(self, atr: float) -> None:
        """Add new ATR value."""
        self.values.append(atr)
        if len(self.values) > self.max_size:
            self.values.pop(0)
    
    def get_percentile(self, p: float) -> float:
        """Get percentile value (0-100)."""
        if not self.values:
            return 0.0
        sorted_vals = sorted(self.values)
        idx = int(len(sorted_vals) * p / 100)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]


@dataclass
class VolumeZScoreTracker:
    """
    Tracks volume history for Z-score calculation.
    
    Z-score = (current - mean) / std_dev
    A Z-score > 2.0 indicates statistically significant volume surge.
    """
    history: deque = field(default_factory=lambda: deque(maxlen=300))  # 5 min at 1/sec
    
    def add(self, volume: float) -> None:
        """Add a volume observation."""
        self.history.append(volume)
    
    def get_zscore(self, current_volume: float) -> float:
        """
        Calculate Z-score for current volume.
        
        Returns:
            Z-score: positive = above average, negative = below
            Returns 0.0 if insufficient history (<30 samples)
        """
        if len(self.history) < 30:  # Need at least 30 samples for reliable stats
            return 0.0
        
        # Calculate mean and std dev
        values = list(self.history)
        mean = sum(values) / len(values)
        
        if mean == 0:
            return 0.0
        
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return 0.0
        
        return (current_volume - mean) / std_dev
    
    def get_surge_ratio(self, current_volume: float) -> float:
        """
        Convert Z-score to a surge ratio for compatibility.
        
        Z-score 0 = 1.0x (baseline)
        Z-score 2 = 2.5x (significant surge)
        Z-score 3+ = 3.5x+ (extreme surge)
        """
        zscore = self.get_zscore(current_volume)
        
        if zscore <= 0:
            return 1.0  # At or below average
        
        # Map Z-score to surge ratio: zscore * 0.75 + 1.0
        # Z=2 -> 2.5x, Z=3 -> 3.25x
        return 1.0 + (zscore * 0.75)


class ConsensusEngine:
    """
    Aggregates price data from multiple exchanges to form consensus.
    
    Logic:
    - If all prices within 0.15%: weighted average by volume
    - If one outlier beyond 0.15%: median of three
    - Otherwise: consensus failure (no signal)
    """
    
    def __init__(self):
        self.logger = logger.bind(component="consensus")
        
        # Exchange metrics cache
        self._binance_metrics: Optional[ExchangeMetrics] = None
        self._coinbase_metrics: Optional[ExchangeMetrics] = None
        self._kraken_metrics: Optional[ExchangeMetrics] = None
        
        # Historical ATR for percentile calculation
        self._atr_history = ATRHistory(values=[])
        
        # Volume tracking - NEW: Z-score based surge detection
        self._volume_zscore_tracker = VolumeZScoreTracker()
        self._last_volume_update_ms: int = 0
        
        # Current consensus
        self._current_consensus: Optional[ConsensusData] = None
    
    def update_exchange(self, exchange: str, metrics: ExchangeMetrics) -> None:
        """Update metrics from an exchange."""
        if exchange == "binance":
            self._binance_metrics = metrics
        elif exchange == "coinbase":
            self._coinbase_metrics = metrics
        elif exchange == "kraken":
            self._kraken_metrics = metrics
    
    def _get_all_metrics(self) -> list[ExchangeMetrics]:
        """Get list of all non-None exchange metrics."""
        metrics = []
        if self._binance_metrics:
            metrics.append(self._binance_metrics)
        if self._coinbase_metrics:
            metrics.append(self._coinbase_metrics)
        if self._kraken_metrics:
            metrics.append(self._kraken_metrics)
        return metrics
    
    def _check_staleness(self, metrics: list[ExchangeMetrics]) -> list[ExchangeMetrics]:
        """Filter out stale metrics (>10s old)."""
        now_ms = int(time.time() * 1000)
        fresh = []
        for m in metrics:
            age_ms = now_ms - m.local_timestamp_ms
            if age_ms < 10000:  # 10 seconds - more reasonable threshold
                fresh.append(m)
            else:
                self.logger.debug(  # Changed to debug to reduce noise
                    "Stale exchange data",
                    exchange=m.exchange,
                    age_ms=age_ms,
                )
        return fresh
    
    def _calculate_deviation(self, prices: list[float]) -> tuple[float, float]:
        """Calculate max deviation and average price."""
        if not prices:
            return 0.0, 0.0
        avg = sum(prices) / len(prices)
        max_dev = max(abs(p - avg) / avg for p in prices)
        return max_dev, avg
    
    def _weighted_average(self, metrics: list[ExchangeMetrics]) -> float:
        """Calculate volume-weighted average price."""
        total_volume = sum(m.volume_1m for m in metrics)
        if total_volume == 0:
            return sum(m.current_price for m in metrics) / len(metrics)
        
        weighted_sum = sum(m.current_price * m.volume_1m for m in metrics)
        return weighted_sum / total_volume
    
    def _median_price(self, metrics: list[ExchangeMetrics]) -> float:
        """Calculate median price."""
        prices = [m.current_price for m in metrics]
        return median(prices)
    
    def _identify_outlier(self, metrics: list[ExchangeMetrics]) -> Optional[str]:
        """Identify which exchange (if any) is the outlier."""
        if len(metrics) < 3:
            return None
        
        prices = [m.current_price for m in metrics]
        avg = sum(prices) / len(prices)
        
        deviations = [(m.exchange, abs(m.current_price - avg) / avg) for m in metrics]
        deviations.sort(key=lambda x: x[1], reverse=True)
        
        # If top deviation is significantly larger than others
        if deviations[0][1] > 0.0015 and deviations[0][1] > 2 * deviations[1][1]:
            return deviations[0][0]
        
        return None
    
    def get_volume_zscore(self) -> float:
        """Get current volume Z-score (for logging/debugging)."""
        if not self._current_consensus:
            return 0.0
        return self._volume_zscore_tracker.get_zscore(
            self._current_consensus.total_volume_1m
        )
    
    def _determine_volatility_regime(self, atr: float) -> VolatilityRegime:
        """Determine current volatility regime based on ATR percentile."""
        if not self._atr_history.values:
            return VolatilityRegime.NORMAL
        
        p25 = self._atr_history.get_percentile(25)
        p75 = self._atr_history.get_percentile(75)
        
        if atr < p25:
            return VolatilityRegime.LOW
        elif atr > p75:
            return VolatilityRegime.HIGH
        else:
            return VolatilityRegime.NORMAL
    
    def compute_consensus(self) -> Optional[ConsensusData]:
        """
        Compute consensus from all exchange data.
        Returns None if consensus cannot be formed.
        """
        all_metrics = self._get_all_metrics()
        
        if len(all_metrics) < 2:
            self.logger.debug("Insufficient exchanges for consensus", count=len(all_metrics))
            return None
        
        # Filter stale data
        fresh_metrics = self._check_staleness(all_metrics)
        if len(fresh_metrics) < 2:
            self.logger.debug("Too many stale exchanges, waiting for fresh data")
            return None
        
        # Calculate price deviation
        prices = [m.current_price for m in fresh_metrics]
        max_deviation, avg_price = self._calculate_deviation(prices)
        
        # Determine consensus price
        tolerance = settings.signals.consensus_price_tolerance
        
        # Calculate agreement_score (1.0 = perfect agreement, decreases with deviation)
        # At tolerance level, agreement_score = 0.85
        # At 2x tolerance, agreement_score ≈ 0.70
        if max_deviation > 0:
            agreement_score = 1.0 - (max_deviation / (2 * tolerance))
            agreement_score = max(0.0, min(1.0, agreement_score))
        else:
            agreement_score = 1.0
        
        if max_deviation <= tolerance:
            # All prices agree - use weighted average
            consensus_price = self._weighted_average(fresh_metrics)
            agreement = True
        elif max_deviation <= 2 * tolerance and len(fresh_metrics) >= 3:
            # One outlier - use median
            outlier = self._identify_outlier(fresh_metrics)
            if outlier:
                self.logger.info("Using median due to outlier", outlier=outlier)
            consensus_price = self._median_price(fresh_metrics)
            agreement = True
        else:
            # Too much disagreement
            self.logger.warning(
                "Consensus failure - high deviation",
                max_deviation=max_deviation,
                prices=prices,
                agreement_score=agreement_score,
            )
            return None
        
        # Aggregate metrics
        move_30s = sum(m.move_30s_pct for m in fresh_metrics) / len(fresh_metrics)
        volatility_30s = sum(m.volatility_30s for m in fresh_metrics) / len(fresh_metrics)
        atr_5m = sum(m.atr_5m for m in fresh_metrics) / len(fresh_metrics)
        max_10s_move = max(m.max_move_10s_pct for m in fresh_metrics)
        
        # Update ATR history
        if atr_5m > 0:
            self._atr_history.add(atr_5m)
        
        # Calculate spike concentration
        spike_concentration = max_10s_move / abs(move_30s) if move_30s != 0 else 0.0
        
        # Volume metrics - FIXED: Use Z-score for proper surge detection
        total_volume = sum(m.volume_1m for m in fresh_metrics)
        
        # Update Z-score tracker (once per second max)
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_volume_update_ms >= 1000:
            self._volume_zscore_tracker.add(total_volume)
            self._last_volume_update_ms = now_ms
        
        # Calculate surge ratio from Z-score
        # Z-score > 2.0 = significant surge (mapped to ~2.5x ratio)
        volume_surge = self._volume_zscore_tracker.get_surge_ratio(total_volume)
        
        # Get average volume for reporting
        avg_volume_5m = sum(self._volume_zscore_tracker.history) / max(len(self._volume_zscore_tracker.history), 1)
        
        # Determine volatility regime
        vol_regime = self._determine_volatility_regime(atr_5m)
        
        now_ms = int(time.time() * 1000)
        
        consensus = ConsensusData(
            consensus_price=consensus_price,
            consensus_timestamp_ms=now_ms,
            binance=self._binance_metrics,
            coinbase=self._coinbase_metrics,
            kraken=self._kraken_metrics,
            move_30s_pct=move_30s,
            volatility_30s=volatility_30s,
            atr_5m=atr_5m,
            volatility_regime=vol_regime,
            max_10s_move_pct=max_10s_move,
            spike_concentration=spike_concentration,
            total_volume_1m=total_volume,
            avg_volume_5m=avg_volume_5m,
            volume_surge_ratio=volume_surge,
            agreement=agreement,
            max_deviation_pct=max_deviation,
            agreement_score=agreement_score,
            exchange_count=len(fresh_metrics),
        )
        
        self._current_consensus = consensus
        return consensus
    
    def get_current_consensus(self) -> Optional[ConsensusData]:
        """Get the most recent consensus data."""
        return self._current_consensus
    
    def get_volatility_regime(self) -> VolatilityRegime:
        """Get current volatility regime."""
        if self._current_consensus:
            return self._current_consensus.volatility_regime
        return VolatilityRegime.NORMAL
    
    def get_atr_percentile_25(self) -> float:
        """Get 25th percentile ATR for threshold calculation."""
        return self._atr_history.get_percentile(25)
    
    def get_metrics(self) -> dict:
        """Get consensus engine metrics."""
        return {
            "binance_connected": self._binance_metrics is not None,
            "coinbase_connected": self._coinbase_metrics is not None,
            "kraken_connected": self._kraken_metrics is not None,
            "consensus_price": self._current_consensus.consensus_price if self._current_consensus else None,
            "move_30s_pct": self._current_consensus.move_30s_pct if self._current_consensus else None,
            "volatility_regime": self._current_consensus.volatility_regime.value if self._current_consensus else None,
            "volume_surge_ratio": self._current_consensus.volume_surge_ratio if self._current_consensus else None,
            "volume_zscore": self.get_volume_zscore(),
            "volume_history_size": len(self._volume_zscore_tracker.history),
            "atr_history_size": len(self._atr_history.values),
        }

