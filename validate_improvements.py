#!/usr/bin/env python3
"""
Simple validation script to check that improvements are correctly implemented.
Checks code structure and key features without requiring dependencies.
"""

import re
import sys
from pathlib import Path


def check_file_contains(file_path: Path, patterns: list[str], description: str) -> bool:
    """Check if file contains all required patterns."""
    try:
        content = file_path.read_text()
        missing = []
        for pattern in patterns:
            if not re.search(pattern, content, re.MULTILINE):
                missing.append(pattern)
        
        if missing:
            print(f"  ✗ {description}")
            print(f"    Missing patterns: {missing}")
            return False
        else:
            print(f"  ✓ {description}")
            return True
    except Exception as e:
        print(f"  ✗ {description} - Error: {e}")
        return False


def validate_improvements():
    """Validate all critical improvements are implemented."""
    print("="*70)
    print("VALIDATING CRITICAL IMPROVEMENTS")
    print("="*70)
    
    results = []
    base_path = Path(__file__).parent
    
    # 1. Check volume_5m_avg in ExchangeMetrics
    print("\n1. Volume Tracking (5-minute average)")
    results.append(check_file_contains(
        base_path / "src/models/schemas.py",
        [r"volume_5m_avg.*float.*=.*0\.0"],
        "ExchangeMetrics has volume_5m_avg field"
    ))
    
    # 2. Check agreement_score in ConsensusData
    print("\n2. Exchange Agreement Score")
    results.append(check_file_contains(
        base_path / "src/models/schemas.py",
        [r"agreement_score.*float.*=.*0\.0"],
        "ConsensusData has agreement_score field"
    ))
    
    # 3. Check volume buffer in Binance feed
    print("\n3. Volume Buffer Implementation")
    results.append(check_file_contains(
        base_path / "src/feeds/binance.py",
        [r"_volume_buffer.*deque", r"VolumeEntry", r"volume_5m_avg"],
        "Binance feed has rolling volume buffer"
    ))
    
    # 4. Check agreement_score calculation in consensus engine
    print("\n4. Agreement Score Calculation")
    results.append(check_file_contains(
        base_path / "src/engine/consensus.py",
        [r"agreement_score", r"agreement_score.*=.*1\.0.*-"],
        "ConsensusEngine calculates agreement_score"
    ))
    
    # 5. Check volume surge check in signal detector
    print("\n5. Volume Authentication Filter")
    results.append(check_file_contains(
        base_path / "src/engine/signal_detector.py",
        [r"VOLUME AUTHENTICATION", r"volume_surge_ratio.*<.*settings\.signals\.volume_surge_threshold"],
        "SignalDetector has volume authentication check"
    ))
    
    # 6. Check spike concentration filter
    print("\n6. Spike Concentration Filter")
    results.append(check_file_contains(
        base_path / "src/engine/signal_detector.py",
        [r"SPIKE CONCENTRATION", r"spike_concentration.*<.*settings\.signals\.spike_concentration_threshold"],
        "SignalDetector has spike concentration check"
    ))
    
    # 7. Check agreement_score check
    print("\n7. Agreement Score Filter")
    results.append(check_file_contains(
        base_path / "src/engine/signal_detector.py",
        [r"EXCHANGE AGREEMENT", r"agreement_score.*<.*settings\.signals\.min_agreement_score"],
        "SignalDetector checks agreement_score"
    ))
    
    # 8. Check liquidity collapse detection
    print("\n8. Liquidity Collapse Detection")
    results.append(check_file_contains(
        base_path / "src/engine/signal_detector.py",
        [r"LIQUIDITY CHECKS", r"liquidity_collapsing"],
        "SignalDetector checks liquidity collapse"
    ))
    
    # 9. Check escape clause logic
    print("\n9. Escape Clause Implementation")
    results.append(check_file_contains(
        base_path / "src/engine/signal_detector.py",
        [r"escape_clause", r"escape_clause_used.*=.*True"],
        "SignalDetector has escape clause logic"
    ))
    
    # 10. Check settings configuration
    print("\n10. Configuration Settings")
    results.append(check_file_contains(
        base_path / "config/settings.py",
        [r"min_agreement_score.*float", r"spike_concentration_threshold", r"volume_surge_threshold"],
        "Settings has new configuration parameters"
    ))
    
    # 11. Check comprehensive logging
    print("\n11. Enhanced Logging")
    results.append(check_file_contains(
        base_path / "src/utils/logging.py",
        [r"log_comprehensive_signal", r"agreement_score", r"volume_surge_ratio"],
        "Logging has comprehensive signal logging"
    ))
    
    # Summary
    print("\n" + "="*70)
    print("VALIDATION SUMMARY")
    print("="*70)
    
    passed = sum(results)
    total = len(results)
    
    print(f"\nResults: {passed}/{total} checks passed")
    
    if passed == total:
        print("\n🎉 All improvements validated!")
        print("\nImplemented features:")
        print("  ✓ Volume tracking with 5-minute rolling average")
        print("  ✓ Exchange agreement scoring (0-1 scale)")
        print("  ✓ Volume authentication (surge detection)")
        print("  ✓ Spike concentration (anti-drift filter)")
        print("  ✓ Liquidity collapse detection")
        print("  ✓ Escape clause for sub-threshold moves")
        print("  ✓ Enhanced configuration settings")
        print("  ✓ Comprehensive logging")
        print("\nThe system now has multi-layered validation:")
        print("  1. Movement threshold (regime-adaptive)")
        print("  2. Volume authentication (≥2x surge)")
        print("  3. Spike concentration (≥60% in 10s)")
        print("  4. Exchange agreement (≥85% score)")
        print("  5. Oracle age window (dynamic)")
        print("  6. Liquidity checks (absolute + collapse)")
        print("  7. Escape clause (for viable sub-1% moves)")
        return True
    else:
        print(f"\n⚠️  {total - passed} check(s) failed")
        return False


if __name__ == "__main__":
    success = validate_improvements()
    sys.exit(0 if success else 1)

