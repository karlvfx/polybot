"""
Sports Betting Arbitrage Module for Polymarket.

This module implements "Top-Down Arbitrage" between sharp sportsbooks
(Pinnacle, Betfair) and Polymarket's sports prediction markets.

Key insight: Sharp books (Pinnacle) have better models than the Polymarket
"crowd". When odds move on Pinnacle, Polymarket often lags 10-60 seconds.

Architecture mirrors the crypto bot:
- feeds/: Sharp book data sources (Pinnacle, Betfair, The Odds API)
- engine/: Signal detection (Sharp-PM divergence)
- models/: Sports-specific data schemas
- discovery/: Polymarket sports market discovery

Fee advantage: Sports markets on Polymarket are currently FEE-FREE for takers,
unlike crypto markets which have 1.6-3%+ dynamic fees at 50% odds.
"""

__version__ = "0.1.0"

