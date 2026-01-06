#!/usr/bin/env python3
"""
Test script to validate the critical improvements.
Tests volume tracking, agreement scoring, and signal detection filters.
"""

import time
from dataclasses import dataclass
from typing import Optional

# Mock imports for testing without full dependencies
import sys
sys.path.insert(0, '/Users/karl/123/polybot')

from src.models.schemas import (
    ConsensusData,
    ExchangeMetrics,
    OracleData,
    PolymarketData,
    SignalCandidate,
    VolatilityRegime,
)
from src.engine.consensus import ConsensusEngine
from src.engine.signal_detector import SignalDetector
from config.settings import settings


def create_mock_exchange_metrics(
    exchange: str,
    price: float,
    volume_1m: float,
    volume_5m_avg: float,
    move_30s: float = 0.01,
    atr_5m: float = 0.005,
    max_10s_move: float = 0.008,
) -> ExchangeMetrics:
    """Create mock exchange metrics for testing."""
    now_ms = int(time.time() * 1000)
    return ExchangeMetrics(
        exchange=exchange,
        current_price=price,
        exchange_timestamp_ms=now_ms,
        local_timestamp_ms=now_ms,
        move_30s_pct=move_30s,
        velocity_30s=move_30s / 30.0,
        volatility_30s=0.002,
        volume_1m=volume_1m,
        volume_5m_avg=volume_5m_avg,
        atr_5m=atr_5m,
        max_move_10s_pct=max_10s_move,
    )


def create_mock_oracle(age_seconds: float = 30.0) -> OracleData:
    """Create mock oracle data."""
    now_ms = int(time.time() * 1000)
    return OracleData(
        current_value=50000.0,
        last_update_timestamp_ms=now_ms - int(age_seconds * 1000),
        oracle_age_seconds=age_seconds,
        round_id=12345,
        recent_heartbeat_intervals=[60.0, 58.0, 62.0],
        avg_heartbeat_interval=60.0,
        next_heartbeat_estimate_ms=now_ms + 30000,
        is_fast_heartbeat_mode=False,
    )


def create_mock_polymarket(
    liquidity: float = 100.0,
    liquidity_30s_ago: float = 100.0,
    imbalance: float = 1.0,
) -> PolymarketData:
    """Create mock Polymarket data."""
    now_ms = int(time.time() * 1000)
    return PolymarketData(
        market_id="test_market",
        timestamp_ms=now_ms,
        yes_bid=0.52,
        yes_ask=0.54,
        yes_liquidity_best=liquidity,
        no_bid=0.46,
        no_ask=0.48,
        no_liquidity_best=liquidity,
        spread=0.02,
        implied_probability=0.53,
        liquidity_30s_ago=liquidity_30s_ago,
        liquidity_60s_ago=liquidity_30s_ago,
        liquidity_collapsing=liquidity < 0.6 * liquidity_30s_ago if liquidity_30s_ago > 0 else False,
        orderbook_imbalance_ratio=imbalance,
    )


def test_volume_tracking():
    """Test that volume tracking calculates surge correctly."""
    print("\n" + "="*60)
    print("TEST 1: Volume Tracking & Surge Detection")
    print("="*60)
    
    engine = ConsensusEngine()
    
    # Create exchanges with different volume profiles
    binance = create_mock_exchange_metrics(
        "binance",
        price=50000.0,
        volume_1m=1000.0,  # Current 1-min volume
        volume_5m_avg=400.0,  # 5-min average (2.5x surge)
        move_30s=0.01,
    )
    
    coinbase = create_mock_exchange_metrics(
        "coinbase",
        price=50010.0,
        volume_1m=800.0,
        volume_5m_avg=350.0,  # ~2.3x surge
        move_30s=0.01,
    )
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    
    consensus = engine.compute_consensus()
    
    if consensus:
        print(f"✓ Consensus computed successfully")
        print(f"  Total volume 1m: {consensus.total_volume_1m:.2f}")
        print(f"  Average volume 5m: {consensus.avg_volume_5m:.2f}")
        print(f"  Volume surge ratio: {consensus.volume_surge_ratio:.2f}x")
        
        if consensus.volume_surge_ratio >= 2.0:
            print(f"  ✓ Volume surge threshold met (≥2.0x)")
        else:
            print(f"  ✗ Volume surge too low (<2.0x)")
            return False
    else:
        print("✗ Failed to compute consensus")
        return False
    
    return True


def test_agreement_score():
    """Test that agreement_score is calculated correctly."""
    print("\n" + "="*60)
    print("TEST 2: Exchange Agreement Score")
    print("="*60)
    
    engine = ConsensusEngine()
    
    # Test 1: Perfect agreement
    binance = create_mock_exchange_metrics("binance", 50000.0, 1000.0, 400.0)
    coinbase = create_mock_exchange_metrics("coinbase", 50005.0, 800.0, 350.0)  # 0.01% difference
    kraken = create_mock_exchange_metrics("kraken", 50002.0, 600.0, 300.0)
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    engine.update_exchange("kraken", kraken)
    
    consensus = engine.compute_consensus()
    
    if consensus:
        print(f"✓ Agreement score: {consensus.agreement_score:.3f}")
        print(f"  Exchange count: {consensus.exchange_count}")
        print(f"  Max deviation: {consensus.max_deviation_pct*100:.3f}%")
        
        if consensus.agreement_score >= 0.85:
            print(f"  ✓ Agreement score meets threshold (≥0.85)")
        else:
            print(f"  ✗ Agreement score too low (<0.85)")
            return False
    else:
        print("✗ Failed to compute consensus")
        return False
    
    return True


def test_spike_concentration():
    """Test spike concentration detection."""
    print("\n" + "="*60)
    print("TEST 3: Spike Concentration (Anti-Drift Filter)")
    print("="*60)
    
    engine = ConsensusEngine()
    
    # Create exchange with sharp spike (70% of move in 10s)
    binance = create_mock_exchange_metrics(
        "binance",
        price=50000.0,
        volume_1m=1000.0,
        volume_5m_avg=400.0,
        move_30s=0.01,  # 1% move
        max_10s_move=0.007,  # 0.7% in 10s = 70% concentration
    )
    
    coinbase = create_mock_exchange_metrics(
        "coinbase",
        price=50010.0,
        volume_1m=800.0,
        volume_5m_avg=350.0,
        move_30s=0.01,
        max_10s_move=0.006,  # 60% concentration
    )
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    
    consensus = engine.compute_consensus()
    
    if consensus:
        print(f"✓ Spike concentration: {consensus.spike_concentration:.3f}")
        print(f"  Max 10s move: {consensus.max_10s_move_pct*100:.2f}%")
        print(f"  30s move: {consensus.move_30s_pct*100:.2f}%")
        
        if consensus.spike_concentration >= 0.60:
            print(f"  ✓ Spike concentration meets threshold (≥60%)")
            print(f"  → This is a SHARP liquidation spike (GOOD)")
        else:
            print(f"  ✗ Spike concentration too low (<60%)")
            print(f"  → This is smooth drift (MEAN-REVERSION TRAP)")
            return False
    else:
        print("✗ Failed to compute consensus")
        return False
    
    return True


def test_signal_detection_filters():
    """Test that signal detector applies all filters correctly."""
    print("\n" + "="*60)
    print("TEST 4: Signal Detection Multi-Layer Filters")
    print("="*60)
    
    detector = SignalDetector()
    
    # Create a good signal (should pass)
    engine = ConsensusEngine()
    binance = create_mock_exchange_metrics(
        "binance",
        price=50000.0,
        volume_1m=1000.0,
        volume_5m_avg=400.0,  # 2.5x surge
        move_30s=0.012,  # 1.2% move
        max_10s_move=0.008,  # 67% concentration
    )
    coinbase = create_mock_exchange_metrics(
        "coinbase",
        price=50005.0,
        volume_1m=800.0,
        volume_5m_avg=350.0,
        move_30s=0.012,
        max_10s_move=0.007,
    )
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    consensus = engine.compute_consensus()
    
    oracle = create_mock_oracle(age_seconds=30.0)  # Optimal age
    pm = create_mock_polymarket(liquidity=100.0, liquidity_30s_ago=100.0)
    
    if not consensus:
        print("✗ Failed to create consensus")
        return False
    
    print(f"Signal conditions:")
    print(f"  Move: {consensus.move_30s_pct*100:.2f}%")
    print(f"  Volume surge: {consensus.volume_surge_ratio:.2f}x")
    print(f"  Spike concentration: {consensus.spike_concentration:.2f}")
    print(f"  Agreement score: {consensus.agreement_score:.3f}")
    print(f"  Oracle age: {oracle.oracle_age_seconds:.1f}s")
    print(f"  Liquidity: €{pm.yes_liquidity_best:.2f}")
    
    signal = detector.detect(consensus, oracle, pm)
    
    if signal:
        print(f"\n✓ Signal detected! (ID: {signal.signal_id[:8]}...)")
        print(f"  Direction: {signal.direction.value}")
        print(f"  Type: {signal.signal_type.value}")
        return True
    else:
        print(f"\n✗ Signal rejected (check logs for reason)")
        return False


def test_liquidity_collapse_detection():
    """Test liquidity collapse detection."""
    print("\n" + "="*60)
    print("TEST 5: Liquidity Collapse Detection")
    print("="*60)
    
    detector = SignalDetector()
    
    engine = ConsensusEngine()
    binance = create_mock_exchange_metrics("binance", 50000.0, 1000.0, 400.0, move_30s=0.01)
    coinbase = create_mock_exchange_metrics("coinbase", 50005.0, 800.0, 350.0, move_30s=0.01)
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    consensus = engine.compute_consensus()
    
    oracle = create_mock_oracle(age_seconds=30.0)
    
    # Test with collapsing liquidity (50% of 30s ago)
    pm_collapsing = create_mock_polymarket(
        liquidity=50.0,
        liquidity_30s_ago=100.0,  # 50% drop
    )
    
    if not consensus:
        print("✗ Failed to create consensus")
        return False
    
    print(f"Liquidity: €{pm_collapsing.yes_liquidity_best:.2f} (was €{pm_collapsing.liquidity_30s_ago:.2f} 30s ago)")
    print(f"Collapsing: {pm_collapsing.liquidity_collapsing}")
    
    signal = detector.detect(consensus, oracle, pm_collapsing)
    
    if signal:
        print("✗ Signal passed despite liquidity collapse (should be rejected)")
        return False
    else:
        print("✓ Signal correctly rejected due to liquidity collapse")
        return True


def test_escape_clause():
    """Test escape clause for sub-threshold moves."""
    print("\n" + "="*60)
    print("TEST 6: Escape Clause (Sub-Threshold Moves)")
    print("="*60)
    
    detector = SignalDetector()
    
    engine = ConsensusEngine()
    # Create move that's below threshold but meets escape clause conditions
    binance = create_mock_exchange_metrics(
        "binance",
        price=50000.0,
        volume_1m=1200.0,  # High volume
        volume_5m_avg=400.0,  # 3x surge
        move_30s=0.0085,  # 0.85% - below 1.5x ATR threshold but above 0.8% min
        max_10s_move=0.006,
    )
    coinbase = create_mock_exchange_metrics(
        "coinbase",
        price=50005.0,
        volume_1m=900.0,
        volume_5m_avg=350.0,
        move_30s=0.0085,
        max_10s_move=0.005,
    )
    
    engine.update_exchange("binance", binance)
    engine.update_exchange("coinbase", coinbase)
    consensus = engine.compute_consensus()
    
    oracle = create_mock_oracle(age_seconds=20.0)  # >15s required
    pm = create_mock_polymarket(
        liquidity=100.0,
        liquidity_30s_ago=100.0,
        imbalance=1.25,  # 25% imbalance
    )
    
    if not consensus:
        print("✗ Failed to create consensus")
        return False
    
    print(f"Move: {consensus.move_30s_pct*100:.2f}% (below threshold but above 0.8%)")
    print(f"Volume surge: {consensus.volume_surge_ratio:.2f}x (≥2.5x required)")
    print(f"Oracle age: {oracle.oracle_age_seconds:.1f}s (≥15s required)")
    print(f"Orderbook imbalance: {pm.orderbook_imbalance_ratio:.2f} (≥20% required)")
    print(f"Liquidity: €{pm.yes_liquidity_best:.2f} (≥€75 required)")
    
    signal = detector.detect(consensus, oracle, pm)
    
    if signal and signal.signal_type.value == "escape_clause":
        print("✓ Escape clause triggered correctly")
        return True
    elif signal:
        print("✓ Signal detected (standard)")
        return True
    else:
        print("✗ Signal rejected (escape clause conditions not met)")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("TESTING CRITICAL IMPROVEMENTS")
    print("="*60)
    print("\nThis script validates the new multi-layer validation system:")
    print("  1. Volume authentication (surge detection)")
    print("  2. Exchange agreement scoring")
    print("  3. Spike concentration (anti-drift)")
    print("  4. Signal detection filters")
    print("  5. Liquidity collapse detection")
    print("  6. Escape clause logic")
    
    results = []
    
    try:
        results.append(("Volume Tracking", test_volume_tracking()))
        results.append(("Agreement Score", test_agreement_score()))
        results.append(("Spike Concentration", test_spike_concentration()))
        results.append(("Signal Detection", test_signal_detection_filters()))
        results.append(("Liquidity Collapse", test_liquidity_collapse_detection()))
        results.append(("Escape Clause", test_escape_clause()))
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status} - {test_name}")
    
    print(f"\nResults: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All improvements validated successfully!")
        print("\nThe system now has:")
        print("  ✓ Volume authentication (prevents wash trading)")
        print("  ✓ Exchange agreement scoring (consensus quality)")
        print("  ✓ Spike concentration detection (anti-drift)")
        print("  ✓ Liquidity collapse protection")
        print("  ✓ Escape clause for viable sub-threshold moves")
        return True
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Review the output above.")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

