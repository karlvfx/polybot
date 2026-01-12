"""
Trading strategies for Polymarket.

These strategies use the existing infrastructure but with different trading logic.

Post-Jan 2026 Update:
- 15-min markets now have 3% taker fees (dynamic based on price)
- 1-hour/daily markets remain fee-free
- Maker rebates = 100% of taker fees redistributed
"""

from src.strategies.volatility_sniper import VolatilitySniper
from src.strategies.cross_arb import CrossPlatformArbScanner
from src.strategies.advanced_maker_arb import AdvancedMakerArb

__all__ = ["VolatilitySniper", "CrossPlatformArbScanner", "AdvancedMakerArb"]

