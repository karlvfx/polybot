"""
Kalshi ↔ Polymarket Cross-Platform Arbitrage Scanner.

This strategy exploits price differences for the SAME event across platforms:
- Polymarket: Crypto-native, more bullish bias
- Kalshi: US-regulated, more conservative bias

Example:
- Polymarket: "BTC > $100k by March" = 52¢ YES
- Kalshi: Same event = 48¢ YES

Arb opportunity:
- Buy YES on Kalshi (48¢) + Buy NO on Polymarket (48¢)
- Total cost: 96¢
- Guaranteed payout: $1.00 (one side wins)
- Profit: 4¢ (4.2% guaranteed)

Note: This scanner DETECTS opportunities. Actual execution across
both platforms requires separate capital and accounts on each.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from datetime import datetime
import ssl
import aiohttp
import certifi

import structlog

logger = structlog.get_logger()


@dataclass
class ArbOpportunity:
    """Represents a cross-platform arbitrage opportunity."""
    opportunity_id: str
    detected_at_ms: int
    
    # Event details
    event_description: str
    event_end_date: str
    
    # Polymarket side
    pm_market_id: str
    pm_yes_price: float
    pm_no_price: float
    pm_question: str
    
    # Kalshi side  
    kalshi_ticker: str
    kalshi_yes_price: float
    kalshi_no_price: float
    kalshi_title: str
    
    # Arb calculation
    best_strategy: str  # "buy_pm_yes_kalshi_no" or "buy_pm_no_kalshi_yes"
    total_cost: float
    guaranteed_payout: float = 1.0
    profit_usd: float = 0.0
    profit_pct: float = 0.0
    
    @property
    def is_profitable(self) -> bool:
        return self.profit_usd > 0


@dataclass
class ScannerStats:
    """Statistics for the cross-arb scanner."""
    scans_completed: int = 0
    opportunities_found: int = 0
    best_arb_pct: float = 0.0
    avg_arb_pct: float = 0.0
    last_scan_ms: int = 0


class CrossPlatformArbScanner:
    """
    Scans for arbitrage opportunities between Kalshi and Polymarket.
    
    Currently supports:
    - Crypto price prediction markets (BTC, ETH targets)
    - Election markets
    - Economic indicator markets
    
    The scanner finds matching events and calculates arb opportunities.
    Execution would need to happen manually or via separate integrations.
    """
    
    # Kalshi API (public, no auth needed for market data)
    KALSHI_API_URL = "https://trading-api.kalshi.com/trade-api/v2"
    
    # Minimum arb threshold (don't alert for <1% opportunities)
    MIN_ARB_PCT = 0.01  # 1%
    
    # Scan interval
    SCAN_INTERVAL_SECONDS = 30
    
    def __init__(
        self,
        min_arb_pct: float = 0.02,  # 2% minimum profit
        virtual_mode: bool = True,
    ):
        self.logger = logger.bind(component="cross_arb_scanner")
        self._min_arb_pct = min_arb_pct
        self._virtual_mode = virtual_mode
        
        # State
        self._running = False
        self._opportunities: list[ArbOpportunity] = []
        self._stats = ScannerStats()
        
        # Cached market data
        self._kalshi_markets: dict = {}
        self._pm_markets: dict = {}
        
        # Callbacks
        self._on_opportunity_found: Optional[Callable] = None
    
    def set_callbacks(
        self,
        on_opportunity_found: Optional[Callable] = None,
    ) -> None:
        """Set callback for when opportunities are found."""
        self._on_opportunity_found = on_opportunity_found
    
    async def start(self) -> None:
        """Start the scanner."""
        self._running = True
        self.logger.info(
            "🔍 Cross-Platform Arb Scanner started",
            min_arb=f"{self._min_arb_pct:.1%}",
            mode="VIRTUAL" if self._virtual_mode else "REAL",
        )
        
        # Start scan loop
        await self._scan_loop()
    
    async def stop(self) -> None:
        """Stop the scanner."""
        self._running = False
        self.logger.info(
            "Cross-Platform Arb Scanner stopped",
            stats=self.get_stats_summary(),
        )
    
    async def _scan_loop(self) -> None:
        """Main scanning loop."""
        while self._running:
            try:
                await self._scan_for_opportunities()
                await asyncio.sleep(self.SCAN_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Scan error", error=str(e))
                await asyncio.sleep(5)
    
    async def _scan_for_opportunities(self) -> list[ArbOpportunity]:
        """Scan both platforms and find arb opportunities."""
        self.logger.debug("Scanning for cross-platform opportunities...")
        
        # Fetch markets from both platforms
        kalshi_markets = await self._fetch_kalshi_crypto_markets()
        pm_markets = await self._fetch_pm_crypto_markets()
        
        opportunities = []
        
        # Match markets and calculate arbs
        for pm_market in pm_markets:
            # Try to find matching Kalshi market
            matching_kalshi = self._find_matching_kalshi_market(pm_market, kalshi_markets)
            
            if matching_kalshi:
                opp = self._calculate_arb(pm_market, matching_kalshi)
                if opp and opp.is_profitable and opp.profit_pct >= self._min_arb_pct:
                    opportunities.append(opp)
                    self._stats.opportunities_found += 1
                    
                    if opp.profit_pct > self._stats.best_arb_pct:
                        self._stats.best_arb_pct = opp.profit_pct
                    
                    self.logger.info(
                        "💰 ARB OPPORTUNITY FOUND",
                        event=opp.event_description[:50],
                        profit=f"{opp.profit_pct:.1%}",
                        profit_usd=f"${opp.profit_usd:.3f}",
                        strategy=opp.best_strategy,
                        pm_yes=f"${opp.pm_yes_price:.2f}",
                        kalshi_yes=f"${opp.kalshi_yes_price:.2f}",
                    )
                    
                    if self._on_opportunity_found:
                        self._on_opportunity_found(opp)
        
        self._stats.scans_completed += 1
        self._stats.last_scan_ms = int(time.time() * 1000)
        self._opportunities = opportunities
        
        if not opportunities:
            self.logger.debug(
                "No arb opportunities found",
                pm_markets=len(pm_markets),
                kalshi_markets=len(kalshi_markets),
            )
        
        return opportunities
    
    async def _fetch_kalshi_crypto_markets(self) -> list[dict]:
        """
        Fetch crypto-related markets from Kalshi.
        
        Note: Kalshi requires API authentication. For demo purposes,
        we return simulated markets based on known Kalshi offerings.
        To use real data, you'll need a Kalshi API key.
        """
        # Check for Kalshi API credentials
        import os
        kalshi_email = os.getenv("KALSHI_EMAIL", "")
        kalshi_password = os.getenv("KALSHI_PASSWORD", "")
        
        if not kalshi_email or not kalshi_password:
            # Return demo data for testing the logic
            self.logger.debug("Kalshi credentials not set - using demo markets")
            return self._get_demo_kalshi_markets()
        
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                # First, login to get token
                login_url = f"{self.KALSHI_API_URL}/login"
                login_data = {"email": kalshi_email, "password": kalshi_password}
                
                async with session.post(login_url, json=login_data, timeout=10) as resp:
                    if resp.status != 200:
                        self.logger.warning(f"Kalshi login failed: {resp.status}")
                        return self._get_demo_kalshi_markets()
                    
                    auth_data = await resp.json()
                    token = auth_data.get("token", "")
                
                # Fetch markets with auth
                url = f"{self.KALSHI_API_URL}/markets"
                params = {"status": "open", "limit": 100}
                headers = {"Authorization": f"Bearer {token}"}
                
                async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        self.logger.warning(f"Kalshi API returned {resp.status}")
                        return self._get_demo_kalshi_markets()
                    
                    data = await resp.json()
                    markets = data.get("markets", [])
                    
                    # Filter for crypto-related
                    crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"]
                    crypto_markets = [
                        m for m in markets
                        if any(kw in m.get("title", "").lower() for kw in crypto_keywords)
                    ]
                    
                    self.logger.debug(f"Found {len(crypto_markets)} Kalshi crypto markets")
                    return crypto_markets
                    
        except Exception as e:
            self.logger.error("Failed to fetch Kalshi markets", error=str(e))
            return self._get_demo_kalshi_markets()
    
    def _get_demo_kalshi_markets(self) -> list[dict]:
        """Return demo Kalshi markets for testing arbitrage logic."""
        # These are representative of typical Kalshi crypto markets
        return [
            {
                "ticker": "INXD-26JAN12-B100250",
                "title": "Bitcoin above $100,250 on January 12?",
                "yes_ask": 0.45,
                "no_ask": 0.57,
                "last_price": 0.44,
            },
            {
                "ticker": "INXD-26JAN31-B105000",
                "title": "Bitcoin above $105,000 on January 31?",
                "yes_ask": 0.38,
                "no_ask": 0.64,
                "last_price": 0.37,
            },
            {
                "ticker": "INXD-26MAR31-B150000",
                "title": "Bitcoin above $150,000 on March 31?",
                "yes_ask": 0.22,
                "no_ask": 0.80,
                "last_price": 0.21,
            },
            {
                "ticker": "INXE-26JAN31-E4000",
                "title": "Ethereum above $4,000 on January 31?",
                "yes_ask": 0.32,
                "no_ask": 0.70,
                "last_price": 0.31,
            },
        ]
    
    async def _fetch_pm_crypto_markets(self) -> list[dict]:
        """Fetch crypto-related markets from Polymarket."""
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                # Polymarket gamma API
                url = "https://gamma-api.polymarket.com/markets"
                params = {
                    "active": "true",
                    "limit": 100,
                }
                
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        self.logger.warning(f"Polymarket API returned {resp.status}")
                        return []
                    
                    markets = await resp.json()
                    
                    # Filter for crypto-related (excluding 15-min markets)
                    crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"]
                    crypto_markets = [
                        m for m in markets
                        if any(kw in m.get("question", "").lower() for kw in crypto_keywords)
                        and "15-min" not in m.get("question", "").lower()
                        and "15m" not in m.get("slug", "").lower()
                    ]
                    
                    self.logger.debug(f"Found {len(crypto_markets)} Polymarket crypto markets")
                    return crypto_markets
                    
        except Exception as e:
            self.logger.error("Failed to fetch Polymarket markets", error=str(e))
            return []
    
    def _find_matching_kalshi_market(
        self,
        pm_market: dict,
        kalshi_markets: list[dict],
    ) -> Optional[dict]:
        """
        Find a Kalshi market that matches a Polymarket market.
        
        Matching is based on:
        - Similar question/title
        - Same target price/date
        - Same underlying asset
        """
        pm_question = pm_market.get("question", "").lower()
        
        for km in kalshi_markets:
            kalshi_title = km.get("title", "").lower()
            
            # Simple keyword matching for now
            # Could be enhanced with NLP/fuzzy matching
            
            # Check for BTC price targets
            if "bitcoin" in pm_question or "btc" in pm_question:
                if "bitcoin" in kalshi_title or "btc" in kalshi_title:
                    # Check for similar price targets
                    # e.g., "BTC > $100k" on both
                    if self._similar_price_target(pm_question, kalshi_title):
                        return km
            
            # Check for ETH price targets
            if "ethereum" in pm_question or "eth" in pm_question:
                if "ethereum" in kalshi_title or "eth" in kalshi_title:
                    if self._similar_price_target(pm_question, kalshi_title):
                        return km
        
        return None
    
    def _similar_price_target(self, text1: str, text2: str) -> bool:
        """Check if two market descriptions have similar price targets."""
        import re
        
        # Extract price targets (e.g., "$100,000", "$100k", "100000")
        def extract_prices(text):
            # Match various price formats
            patterns = [
                r'\$[\d,]+(?:k)?',  # $100,000 or $100k
                r'[\d,]+(?:\s*(?:k|thousand|million))?',  # 100k, 100 thousand
            ]
            prices = []
            for p in patterns:
                matches = re.findall(p, text.lower())
                prices.extend(matches)
            return prices
        
        prices1 = extract_prices(text1)
        prices2 = extract_prices(text2)
        
        # Check for any overlap
        for p1 in prices1:
            for p2 in prices2:
                # Normalize and compare
                p1_clean = p1.replace("$", "").replace(",", "").replace("k", "000")
                p2_clean = p2.replace("$", "").replace(",", "").replace("k", "000")
                try:
                    if abs(float(p1_clean) - float(p2_clean)) < 1000:  # Within $1000
                        return True
                except ValueError:
                    continue
        
        return False
    
    def _calculate_arb(
        self,
        pm_market: dict,
        kalshi_market: dict,
    ) -> Optional[ArbOpportunity]:
        """Calculate the arbitrage opportunity between two matching markets."""
        try:
            # Get Polymarket prices
            pm_prices = pm_market.get("outcomePrices", "[0.5, 0.5]")
            if isinstance(pm_prices, str):
                import json
                pm_prices = json.loads(pm_prices)
            pm_yes = float(pm_prices[0]) if pm_prices else 0.5
            pm_no = float(pm_prices[1]) if len(pm_prices) > 1 else 1 - pm_yes
            
            # Get Kalshi prices
            kalshi_yes = kalshi_market.get("yes_ask", 0.5)
            kalshi_no = kalshi_market.get("no_ask", 0.5)
            
            # If Kalshi uses bid/ask differently
            if kalshi_yes == 0.5:
                kalshi_yes = kalshi_market.get("last_price", 0.5)
                kalshi_no = 1 - kalshi_yes
            
            # Calculate both arb strategies
            # Strategy 1: Buy YES on PM + NO on Kalshi
            cost_1 = pm_yes + kalshi_no
            profit_1 = 1.0 - cost_1
            
            # Strategy 2: Buy NO on PM + YES on Kalshi
            cost_2 = pm_no + kalshi_yes
            profit_2 = 1.0 - cost_2
            
            # Pick the better strategy
            if profit_1 >= profit_2:
                best_strategy = "buy_pm_yes_kalshi_no"
                total_cost = cost_1
                profit_usd = profit_1
            else:
                best_strategy = "buy_pm_no_kalshi_yes"
                total_cost = cost_2
                profit_usd = profit_2
            
            profit_pct = profit_usd / total_cost if total_cost > 0 else 0
            
            return ArbOpportunity(
                opportunity_id=f"arb_{int(time.time())}_{pm_market.get('id', '')[:8]}",
                detected_at_ms=int(time.time() * 1000),
                event_description=pm_market.get("question", "Unknown"),
                event_end_date=pm_market.get("endDate", ""),
                pm_market_id=pm_market.get("id", ""),
                pm_yes_price=pm_yes,
                pm_no_price=pm_no,
                pm_question=pm_market.get("question", ""),
                kalshi_ticker=kalshi_market.get("ticker", ""),
                kalshi_yes_price=kalshi_yes,
                kalshi_no_price=kalshi_no,
                kalshi_title=kalshi_market.get("title", ""),
                best_strategy=best_strategy,
                total_cost=total_cost,
                profit_usd=profit_usd,
                profit_pct=profit_pct,
            )
            
        except Exception as e:
            self.logger.error("Error calculating arb", error=str(e))
            return None
    
    def get_active_opportunities(self) -> list[ArbOpportunity]:
        """Get currently active arbitrage opportunities."""
        return [o for o in self._opportunities if o.is_profitable]
    
    def get_stats_summary(self) -> dict:
        """Get summary of scanner statistics."""
        return {
            "scans_completed": self._stats.scans_completed,
            "opportunities_found": self._stats.opportunities_found,
            "best_arb": f"{self._stats.best_arb_pct:.1%}",
            "active_opportunities": len(self.get_active_opportunities()),
        }


async def test_scanner():
    """Quick test of the scanner."""
    scanner = CrossPlatformArbScanner(min_arb_pct=0.01)
    
    def on_opportunity(opp):
        print(f"\n🎯 OPPORTUNITY: {opp.event_description[:50]}")
        print(f"   Profit: {opp.profit_pct:.1%} (${opp.profit_usd:.3f})")
        print(f"   Strategy: {opp.best_strategy}")
    
    scanner.set_callbacks(on_opportunity_found=on_opportunity)
    
    print("🔍 Testing cross-platform arb scanner...")
    print("   Fetching markets from Kalshi and Polymarket...")
    
    # Run one scan
    opportunities = await scanner._scan_for_opportunities()
    
    print(f"\n📊 Results:")
    print(f"   Opportunities found: {len(opportunities)}")
    
    for opp in opportunities[:5]:  # Show top 5
        print(f"\n   {opp.event_description[:60]}")
        print(f"   PM YES: ${opp.pm_yes_price:.2f} | Kalshi YES: ${opp.kalshi_yes_price:.2f}")
        print(f"   Profit: {opp.profit_pct:.1%}")


if __name__ == "__main__":
    asyncio.run(test_scanner())

