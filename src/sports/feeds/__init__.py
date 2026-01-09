"""
Sports betting data feeds.

Provides real-time odds from sharp sportsbooks:
- The Odds API: Aggregates odds from 40+ bookmakers (free tier available)
- Pinnacle: The sharpest book, gold standard for fair odds
- Betfair Exchange: True market prices from betting exchange
"""

from src.sports.feeds.odds_api import OddsAPIFeed
# from src.sports.feeds.pinnacle import PinnacleFeed  # Coming soon
# from src.sports.feeds.betfair import BetfairFeed    # Coming soon

__all__ = [
    "OddsAPIFeed",
]

