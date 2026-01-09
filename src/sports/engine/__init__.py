"""
Sports signal detection and scoring engine.

Implements the Sharp-PM divergence strategy:
1. Get "fair" odds from sharp books (Pinnacle, Betfair)
2. Compare to Polymarket implied probability
3. Signal when divergence exceeds threshold (0.5-1.5% vs 8% for crypto!)
"""

from src.sports.engine.signal_detector import SportsSignalDetector
from src.sports.engine.confidence import SportsConfidenceScorer

__all__ = [
    "SportsSignalDetector",
    "SportsConfidenceScorer",
]

