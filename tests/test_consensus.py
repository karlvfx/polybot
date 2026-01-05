"""Tests for the consensus engine."""

import pytest
import time
from src.engine.consensus import ConsensusEngine
from src.models.schemas import ExchangeMetrics, VolatilityRegime


@pytest.fixture
def consensus_engine():
    """Create a fresh consensus engine for each test."""
    return ConsensusEngine()


@pytest.fixture
def sample_binance_metrics():
    """Create sample Binance metrics."""
    return ExchangeMetrics(
        exchange="binance",
        current_price=43500.0,
        exchange_timestamp_ms=int(time.time() * 1000),
        local_timestamp_ms=int(time.time() * 1000),
        move_30s_pct=0.008,
        velocity_30s=0.0001,
        volatility_30s=0.003,
        volume_1m=1250.0,
        atr_5m=0.0075,
        max_move_10s_pct=0.006,
    )


@pytest.fixture
def sample_coinbase_metrics():
    """Create sample Coinbase metrics."""
    return ExchangeMetrics(
        exchange="coinbase",
        current_price=43505.0,
        exchange_timestamp_ms=int(time.time() * 1000),
        local_timestamp_ms=int(time.time() * 1000),
        move_30s_pct=0.0082,
        velocity_30s=0.00012,
        volatility_30s=0.0028,
        volume_1m=890.0,
        atr_5m=0.0073,
        max_move_10s_pct=0.0055,
    )


@pytest.fixture
def sample_kraken_metrics():
    """Create sample Kraken metrics."""
    return ExchangeMetrics(
        exchange="kraken",
        current_price=43498.0,
        exchange_timestamp_ms=int(time.time() * 1000),
        local_timestamp_ms=int(time.time() * 1000),
        move_30s_pct=0.0078,
        velocity_30s=0.0001,
        volatility_30s=0.0032,
        volume_1m=670.0,
        atr_5m=0.0077,
        max_move_10s_pct=0.005,
    )


class TestConsensusEngine:
    """Tests for ConsensusEngine class."""
    
    def test_needs_minimum_exchanges(self, consensus_engine, sample_binance_metrics):
        """Test that consensus requires at least 2 exchanges."""
        consensus_engine.update_exchange("binance", sample_binance_metrics)
        
        result = consensus_engine.compute_consensus()
        assert result is None  # Should fail with only 1 exchange
    
    def test_consensus_with_two_exchanges(
        self,
        consensus_engine,
        sample_binance_metrics,
        sample_coinbase_metrics,
    ):
        """Test consensus with two agreeing exchanges."""
        consensus_engine.update_exchange("binance", sample_binance_metrics)
        consensus_engine.update_exchange("coinbase", sample_coinbase_metrics)
        
        result = consensus_engine.compute_consensus()
        
        assert result is not None
        assert result.agreement is True
        assert 43498 <= result.consensus_price <= 43510
    
    def test_consensus_with_three_exchanges(
        self,
        consensus_engine,
        sample_binance_metrics,
        sample_coinbase_metrics,
        sample_kraken_metrics,
    ):
        """Test consensus with three agreeing exchanges."""
        consensus_engine.update_exchange("binance", sample_binance_metrics)
        consensus_engine.update_exchange("coinbase", sample_coinbase_metrics)
        consensus_engine.update_exchange("kraken", sample_kraken_metrics)
        
        result = consensus_engine.compute_consensus()
        
        assert result is not None
        assert result.agreement is True
        assert result.binance is not None
        assert result.coinbase is not None
        assert result.kraken is not None
    
    def test_outlier_detection(self, consensus_engine):
        """Test that outlier exchange is detected."""
        # Normal prices
        binance = ExchangeMetrics(
            exchange="binance",
            current_price=43500.0,
            exchange_timestamp_ms=int(time.time() * 1000),
            local_timestamp_ms=int(time.time() * 1000),
            volume_1m=1000.0,
        )
        coinbase = ExchangeMetrics(
            exchange="coinbase",
            current_price=43505.0,
            exchange_timestamp_ms=int(time.time() * 1000),
            local_timestamp_ms=int(time.time() * 1000),
            volume_1m=1000.0,
        )
        # Outlier price
        kraken = ExchangeMetrics(
            exchange="kraken",
            current_price=44000.0,  # ~1% off
            exchange_timestamp_ms=int(time.time() * 1000),
            local_timestamp_ms=int(time.time() * 1000),
            volume_1m=500.0,
        )
        
        consensus_engine.update_exchange("binance", binance)
        consensus_engine.update_exchange("coinbase", coinbase)
        consensus_engine.update_exchange("kraken", kraken)
        
        result = consensus_engine.compute_consensus()
        
        # With median fallback, should still get consensus
        assert result is not None
        # Median should be close to binance/coinbase, not kraken
        assert 43500 <= result.consensus_price <= 43510
    
    def test_volatility_regime_detection(self, consensus_engine):
        """Test volatility regime classification."""
        # Add some ATR history
        for _ in range(50):
            consensus_engine._atr_history.add(0.007)  # Normal ATR
        
        # Low volatility
        regime = consensus_engine._determine_volatility_regime(0.003)
        assert regime == VolatilityRegime.LOW
        
        # Normal volatility
        regime = consensus_engine._determine_volatility_regime(0.007)
        assert regime == VolatilityRegime.NORMAL
        
        # High volatility
        regime = consensus_engine._determine_volatility_regime(0.015)
        assert regime == VolatilityRegime.HIGH
    
    def test_volume_surge_calculation(
        self,
        consensus_engine,
        sample_binance_metrics,
        sample_coinbase_metrics,
    ):
        """Test volume surge ratio calculation."""
        consensus_engine.update_exchange("binance", sample_binance_metrics)
        consensus_engine.update_exchange("coinbase", sample_coinbase_metrics)
        
        # First consensus to establish volume history
        consensus_engine.compute_consensus()
        
        # Wait and compute again
        result = consensus_engine.compute_consensus()
        
        assert result is not None
        assert result.volume_surge_ratio >= 0


class TestPriceAgreement:
    """Tests for price agreement calculations."""
    
    def test_weighted_average(self, consensus_engine):
        """Test volume-weighted average calculation."""
        metrics = [
            ExchangeMetrics(exchange="a", current_price=100, volume_1m=1000,
                          exchange_timestamp_ms=0, local_timestamp_ms=0),
            ExchangeMetrics(exchange="b", current_price=102, volume_1m=2000,
                          exchange_timestamp_ms=0, local_timestamp_ms=0),
        ]
        
        # Expected: (100*1000 + 102*2000) / 3000 = 101.33
        result = consensus_engine._weighted_average(metrics)
        assert abs(result - 101.333) < 0.01
    
    def test_median_price(self, consensus_engine):
        """Test median price calculation."""
        metrics = [
            ExchangeMetrics(exchange="a", current_price=100,
                          exchange_timestamp_ms=0, local_timestamp_ms=0),
            ExchangeMetrics(exchange="b", current_price=102,
                          exchange_timestamp_ms=0, local_timestamp_ms=0),
            ExchangeMetrics(exchange="c", current_price=110,
                          exchange_timestamp_ms=0, local_timestamp_ms=0),
        ]
        
        result = consensus_engine._median_price(metrics)
        assert result == 102


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

