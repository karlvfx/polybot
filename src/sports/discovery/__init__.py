"""
Sports market discovery for Polymarket.

Unlike crypto 15-minute markets which have predictable slugs like:
    btc-updown-15m-{timestamp}

Sports markets have event-specific slugs like:
    chiefs-vs-ravens-afc-championship
    man-city-vs-liverpool-epl

Discovery strategies:
1. Search Polymarket events API with sport keywords
2. Match discovered PM markets to sharp book events
3. Track active sports markets for monitoring
"""

from src.sports.discovery.polymarket import SportsMarketDiscovery, MatchedMarket

__all__ = [
    "SportsMarketDiscovery",
    "MatchedMarket",
]

