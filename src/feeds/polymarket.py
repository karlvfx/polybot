"""
Polymarket orderbook WebSocket feed.
Monitors YES/NO token orderbooks for 15-minute up/down markets.
Includes automatic market discovery for BTC 15-min markets.

Enhanced features:
- Multi-market discovery and tracking
- Market quality scoring
- Best market selection based on quality + mispricing
- Proper orderbook imbalance detection
"""

import asyncio
import orjson  # 2-3x faster than stdlib json
import re
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import certifi
import httpx
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from src.feeds.base import FeedHealth
from src.models.schemas import PolymarketData, OrderbookLevel

logger = structlog.get_logger()


# --- Market Discovery ---

@dataclass
class DiscoveredMarket:
    """A discovered Polymarket market."""
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    tokens: list[dict]
    outcome: str  # "up" or "down"
    # NEW: Enhanced market metadata
    created_at_ms: int = 0
    liquidity: float = 0.0
    spread: float = 0.0
    volume_24h: float = 0.0
    
    @property
    def age_seconds(self) -> float:
        """Get market age in seconds."""
        if self.created_at_ms == 0:
            return 0.0
        return (int(time.time() * 1000) - self.created_at_ms) / 1000
    
    @property
    def time_to_close_seconds(self) -> float:
        """Get time until market closes."""
        if not self.end_date_iso:
            return 900.0  # Default 15 minutes
        try:
            end_dt = datetime.fromisoformat(self.end_date_iso.replace('Z', '+00:00'))
            now = datetime.now(end_dt.tzinfo)
            return max(0, (end_dt - now).total_seconds())
        except:
            return 900.0


@dataclass
class MarketQualityScore:
    """Quality score breakdown for a market."""
    total_score: float
    liquidity_score: float
    age_score: float
    spread_score: float
    time_to_close_score: float
    
    def __str__(self) -> str:
        return f"Quality: {self.total_score:.2f} (liq={self.liquidity_score:.2f}, age={self.age_score:.2f}, spread={self.spread_score:.2f}, ttc={self.time_to_close_score:.2f})"


@dataclass
class CachedMarket:
    """A cached market with metadata."""
    market: DiscoveredMarket
    token_ids: dict  # {yes_token_id, no_token_id}
    fetched_at_ms: int
    window_end_ts: int  # Unix timestamp of window end
    
    @property
    def age_seconds(self) -> float:
        """Get cache entry age in seconds."""
        return (int(time.time() * 1000) - self.fetched_at_ms) / 1000
    
    @property
    def is_stale(self) -> bool:
        """Check if cache entry is stale (>5 minutes old)."""
        return self.age_seconds > 300


class MarketCache:
    """
    Caches discovered markets to eliminate discovery latency spikes.
    
    Pre-fetches:
    - Current window
    - Next window (+15 min)
    - Window after (+30 min)
    
    Benefits:
    - Instant market switching at window boundaries
    - Eliminates discovery API call latency during signals
    - Handles API failures gracefully with cached fallback
    """
    
    def __init__(self, asset: str = "BTC"):
        self.asset = asset.upper()
        self.logger = logger.bind(component="market_cache", asset=self.asset)
        self._cache: dict[int, CachedMarket] = {}  # window_end_ts -> CachedMarket
        self._discovery = MarketDiscovery(asset=asset)
        self._priming_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self) -> None:
        """Start the cache priming background task."""
        self._running = True
        self._priming_task = asyncio.create_task(self._prime_loop())
        self.logger.info("Market cache started")
    
    async def stop(self) -> None:
        """Stop the cache priming task."""
        self._running = False
        if self._priming_task:
            self._priming_task.cancel()
            try:
                await self._priming_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Market cache stopped")
    
    async def _prime_loop(self) -> None:
        """Background task that keeps cache primed with upcoming windows."""
        while self._running:
            try:
                await self._prime_upcoming_windows()
                # Re-prime every 60 seconds
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Cache priming error", error=str(e))
                await asyncio.sleep(30)
    
    async def _prime_upcoming_windows(self) -> None:
        """Pre-fetch markets for current and upcoming windows."""
        now = int(time.time())
        interval = 900  # 15 minutes
        
        # Calculate window timestamps to prime
        current_window_end = ((now // interval) + 1) * interval
        windows_to_prime = [
            current_window_end,                  # Current
            current_window_end + interval,       # Next
            current_window_end + (2 * interval), # +30 min
        ]
        
        # Clean up old entries
        self._cleanup_old_entries(current_window_end - interval)
        
        # Fetch any missing windows
        for window_ts in windows_to_prime:
            if window_ts not in self._cache or self._cache[window_ts].is_stale:
                await self._fetch_and_cache(window_ts)
    
    async def _fetch_and_cache(self, window_ts: int) -> Optional[CachedMarket]:
        """Fetch market for a specific window and cache it."""
        try:
            # Generate slug for this specific window
            slugs = [f"{slug_base}-{window_ts}" for slug_base in self._discovery._patterns["slugs"]]
            
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            async with httpx.AsyncClient(verify=ssl_context, timeout=10.0) as client:
                for slug in slugs:
                    try:
                        response = await client.get(
                            f"{self._discovery.GAMMA_API_URL}/markets",
                            params={"slug": slug},
                        )
                        
                        if response.status_code == 200:
                            data = response.json()
                            markets_data = data if isinstance(data, list) else [data] if data else []
                            
                            for market_data in markets_data:
                                if not market_data or market_data.get("closed", False):
                                    continue
                                
                                # Parse tokens
                                tokens_raw = market_data.get("clobTokenIds", [])
                                if isinstance(tokens_raw, str):
                                    tokens = orjson.loads(tokens_raw)
                                else:
                                    tokens = tokens_raw
                                
                                if len(tokens) >= 2:
                                    cached = CachedMarket(
                                        market=DiscoveredMarket(
                                            condition_id=market_data.get("conditionId", ""),
                                            question=market_data.get("question", ""),
                                            description=market_data.get("description", ""),
                                            end_date_iso=market_data.get("endDate", ""),
                                            tokens=[{"token_id": t} for t in tokens],
                                            outcome="up",
                                        ),
                                        token_ids={
                                            "yes": tokens[0],
                                            "no": tokens[1],
                                        },
                                        fetched_at_ms=int(time.time() * 1000),
                                        window_end_ts=window_ts,
                                    )
                                    self._cache[window_ts] = cached
                                    self.logger.debug(
                                        "Cached market",
                                        window_ts=window_ts,
                                        condition_id=cached.market.condition_id[:20] + "...",
                                    )
                                    return cached
                    except Exception as e:
                        self.logger.debug("Slug fetch failed", slug=slug, error=str(e))
                        continue
            
            return None
            
        except Exception as e:
            self.logger.warning("Failed to fetch market for cache", window_ts=window_ts, error=str(e))
            return None
    
    def _cleanup_old_entries(self, cutoff_ts: int) -> None:
        """Remove cache entries older than cutoff."""
        old_keys = [ts for ts in self._cache if ts < cutoff_ts]
        for key in old_keys:
            del self._cache[key]
    
    def get_cached_market(self, window_ts: int) -> Optional[CachedMarket]:
        """Get a cached market for a specific window."""
        return self._cache.get(window_ts)
    
    def get_current_cached_market(self) -> Optional[CachedMarket]:
        """Get the cached market for the current window."""
        now = int(time.time())
        interval = 900
        current_window_end = ((now // interval) + 1) * interval
        return self._cache.get(current_window_end)
    
    @property
    def cache_size(self) -> int:
        """Get number of cached markets."""
        return len(self._cache)
    
    @property
    def cached_windows(self) -> list[int]:
        """Get list of cached window timestamps."""
        return sorted(self._cache.keys())
    

class MarketDiscovery:
    """
    Discovers active BTC 15-minute up/down markets from Polymarket.
    
    Enhanced features:
    - Discovers ALL active markets, not just one
    - Calculates quality scores for market selection
    - Supports pre-warming period tracking
    
    Markets follow the slug pattern: btc-up-or-down-15m-[TIMESTAMP]
    where TIMESTAMP is the Unix timestamp of the 15-minute window end.
    """
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    # 15-minute interval in seconds
    INTERVAL_SECONDS = 15 * 60  # 900 seconds
    
    # Quality scoring thresholds
    TARGET_LIQUIDITY = 10000  # €10k target liquidity
    MIN_MARKET_AGE = 30  # seconds - markets need time to build liquidity
    MAX_MARKET_AGE = 300  # seconds - older markets might have stale odds
    MIN_TIME_TO_CLOSE = 600  # seconds - need at least 10 mins to trade safely
    
    # Asset-specific slug patterns and keywords
    # Primary format: {asset}-updown-15m-{timestamp} (confirmed from Polymarket URLs)
    # Fallback: {asset}-up-or-down-15m-{timestamp}
    ASSET_PATTERNS = {
        "BTC": {
            "slugs": [
                "btc-updown-15m",           # Primary format (from polymarket.com/event/btc-updown-15m-*)
                "btc-up-or-down-15m",       # Alternative format
                "bitcoin-updown-15m",       # Full name variant
            ],
            "keywords": ["bitcoin", "btc"],
            "name": "Bitcoin",
            "tag_id": 235,  # Polymarket tag ID for Bitcoin
        },
        "ETH": {
            "slugs": [
                "eth-updown-15m",           # Primary format
                "eth-up-or-down-15m",       # Alternative format
                "ethereum-updown-15m",      # Full name variant
            ],
            "keywords": ["ethereum", "eth"],
            "name": "Ethereum",
            "tag_id": 236,  # Polymarket tag ID for Ethereum
        },
        "SOL": {
            "slugs": [
                "sol-updown-15m",           # Primary format
                "sol-up-or-down-15m",       # Alternative format
                "solana-updown-15m",        # Full name variant
            ],
            "keywords": ["solana", "sol"],
            "name": "Solana",
            "tag_id": None,  # No dedicated tag, use keyword search
        },
    }
    
    def __init__(self, asset: str = "BTC"):
        self.asset = asset.upper()
        self.logger = logger.bind(component="market_discovery", asset=self.asset)
        
        # Get asset-specific patterns
        if self.asset not in self.ASSET_PATTERNS:
            self.logger.warning(f"Unknown asset {self.asset}, using BTC patterns")
            self.asset = "BTC"
        self._patterns = self.ASSET_PATTERNS[self.asset]
    
    def _get_current_window_timestamps(self) -> list[int]:
        """
        Get timestamps for current and upcoming 15-minute windows.
        
        Polymarket markets use the window END time as the timestamp.
        Example: btc-updown-15m-1767161700 resolves at Unix time 1767161700
        
        Returns list of possible window end timestamps to try (current, next, previous).
        """
        now = int(time.time())
        
        # Calculate current window end (round up to next 15-min boundary)
        # Example: if now=1767161500, current_window_end=1767161700
        current_window_end = ((now // self.INTERVAL_SECONDS) + 1) * self.INTERVAL_SECONDS
        
        # Try multiple windows to handle timing edge cases
        timestamps = [
            current_window_end,                              # Current window (most likely active)
            current_window_end + self.INTERVAL_SECONDS,      # Next window (pre-market)
            current_window_end - self.INTERVAL_SECONDS,      # Previous window (might still be open)
            current_window_end + (2 * self.INTERVAL_SECONDS), # Two ahead
        ]
        
        self.logger.debug(
            "Generated window timestamps",
            now=now,
            current_window_end=current_window_end,
            timestamps=timestamps,
            human_readable=[
                datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC") 
                for ts in timestamps[:3]
            ]
        )
        
        return timestamps
    
    def _generate_market_slugs(self) -> list[str]:
        """
        Generate possible market slugs based on current time and asset.
        
        Format: {slug_pattern}-{unix_timestamp}
        Example: btc-updown-15m-1767161700
        """
        timestamps = self._get_current_window_timestamps()
        slugs = []
        
        # Prioritize current window with primary slug format
        for ts in timestamps:
            for slug_base in self._patterns["slugs"]:
                slugs.append(f"{slug_base}-{ts}")
        
        self.logger.debug(
            "Generated market slugs",
            asset=self.asset,
            slug_count=len(slugs),
            first_slugs=slugs[:3],  # Show first 3 (primary format for each timestamp)
        )
        
        return slugs
    
    def get_market_url(self, timestamp: int) -> str:
        """
        Get the Polymarket URL for a market.
        
        Example: https://polymarket.com/event/btc-updown-15m-1767161700
        """
        primary_slug = self._patterns["slugs"][0]  # Use primary slug format
        return f"https://polymarket.com/event/{primary_slug}-{timestamp}"
    
    async def find_15min_markets(self) -> list[DiscoveredMarket]:
        """
        Find ALL active 15-minute up/down markets for the configured asset.
        
        Uses time-based slug pattern: [asset]-up-or-down-15m-[TIMESTAMP]
        Returns all discovered markets (not just one) for quality-based selection.
        """
        markets = []
        
        try:
            markets = await self._fetch_all_current_markets()
        except Exception as e:
            self.logger.warning("Market discovery failed", error=str(e), asset=self.asset)
        
        if markets:
            self.logger.info("Market discovery complete", found=len(markets), asset=self.asset)
        else:
            self.logger.warning("No 15-minute markets found", asset=self.asset)
        
        return markets
    
    # Alias for backwards compatibility
    async def find_btc_15min_markets(self) -> list[DiscoveredMarket]:
        """Backwards compatible alias for find_15min_markets."""
        return await self.find_15min_markets()
    
    def assess_market_quality(self, market: DiscoveredMarket) -> MarketQualityScore:
        """
        Calculate comprehensive quality score for a market (0-1).
        
        Scoring weights:
        - Liquidity: 40% - Higher liquidity = better fills
        - Age: 30% - 30-300s old is optimal (has liquidity, not stale)
        - Spread: 20% - Tighter spread = lower slippage
        - Time-to-close: 10% - Need enough time to trade safely
        """
        # Liquidity score (40%)
        liq_score = min(1.0, market.liquidity / self.TARGET_LIQUIDITY)
        
        # Age score (30%) - prefer markets 30-300s old
        age_s = market.age_seconds
        if self.MIN_MARKET_AGE <= age_s <= self.MAX_MARKET_AGE:
            age_score = 1.0
        elif age_s < self.MIN_MARKET_AGE:
            age_score = age_s / self.MIN_MARKET_AGE
        else:
            # Decay after MAX_MARKET_AGE
            age_score = max(0.0, 1.0 - (age_s - self.MAX_MARKET_AGE) / self.MAX_MARKET_AGE)
        
        # Spread score (20%) - tighter is better (target <5%)
        if market.spread > 0:
            spread_score = max(0.0, 1.0 - market.spread / 0.10)
        else:
            spread_score = 0.5  # Unknown spread
        
        # Time-to-close score (10%) - need at least 10 mins
        ttc_s = market.time_to_close_seconds
        if ttc_s > self.MIN_TIME_TO_CLOSE:
            ttc_score = 1.0
        else:
            ttc_score = ttc_s / self.MIN_TIME_TO_CLOSE
        
        total_score = (
            0.40 * liq_score +
            0.30 * age_score +
            0.20 * spread_score +
            0.10 * ttc_score
        )
        
        return MarketQualityScore(
            total_score=total_score,
            liquidity_score=liq_score,
            age_score=age_score,
            spread_score=spread_score,
            time_to_close_score=ttc_score,
        )
    
    async def _fetch_all_current_markets(self) -> list[DiscoveredMarket]:
        """
        Fetch ALL active 15-min markets for the configured asset using time-based slug pattern.
        
        Returns all discovered markets for quality-based selection.
        """
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        discovered = []
        seen_condition_ids = set()  # Deduplicate
        
        async with httpx.AsyncClient(verify=ssl_context, timeout=15.0) as client:
            # Try time-based slugs first (primary method)
            slugs = self._generate_market_slugs()
            
            for slug in slugs:
                try:
                    self.logger.debug("Trying slug", slug=slug)
                    response = await client.get(
                        f"{self.GAMMA_API_URL}/markets",
                        params={"slug": slug},
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        
                        # Handle both list and single object responses
                        markets_data = data if isinstance(data, list) else [data] if data else []
                        
                        for market in markets_data:
                            if not market:
                                continue
                                
                            condition_id = market.get("conditionId") or market.get("condition_id", "")
                            if not condition_id or condition_id in seen_condition_ids:
                                continue
                            
                            # Check if market is active
                            if market.get("closed", False):
                                continue
                            
                            seen_condition_ids.add(condition_id)
                            question = market.get("question", f"{self.asset} 15-min ({slug})")
                            
                            # Parse clobTokenIds (may be JSON string)
                            tokens_raw = market.get("clobTokenIds", [])
                            if isinstance(tokens_raw, str):
                                tokens = orjson.loads(tokens_raw)
                            else:
                                tokens = tokens_raw
                            
                            # Extract additional metadata for quality scoring
                            liquidity = 0.0
                            spread = 0.0
                            created_at_ms = 0
                            
                            try:
                                # Try to get liquidity from outcomePrices or volume
                                outcome_prices = market.get("outcomePrices", "")
                                if isinstance(outcome_prices, str) and outcome_prices:
                                    prices = orjson.loads(outcome_prices)
                                    if len(prices) >= 2:
                                        spread = abs(float(prices[0]) - float(prices[1]))
                                
                                # Get volume as liquidity proxy
                                liquidity = float(market.get("volume", 0) or 0)
                                
                                # Get creation time
                                created_at = market.get("createdAt", "")
                                if created_at:
                                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                    created_at_ms = int(dt.timestamp() * 1000)
                            except:
                                pass
                            
                            discovered.append(DiscoveredMarket(
                                condition_id=condition_id,
                                question=question,
                                description=market.get("description", "")[:200],
                                end_date_iso=market.get("endDate", ""),
                                tokens=tokens,
                                outcome="up" if "up" in question.lower() else "down",
                                created_at_ms=created_at_ms,
                                liquidity=liquidity,
                                spread=spread,
                            ))
                            
                            self.logger.info(
                                f"Found {self.asset} 15-min market",
                                asset=self.asset,
                                slug=slug,
                                condition_id=condition_id[:40],
                                question=question[:50],
                                liquidity=liquidity,
                            )
                            
                            # DON'T return early - continue to find ALL markets
                            
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        self.logger.debug("HTTP error", slug=slug, status=e.response.status_code)
                except Exception as e:
                    self.logger.debug("Error fetching slug", slug=slug, error=str(e))
            
            # Fallback: Search events for 15-min markets
            if not discovered:
                discovered = await self._search_events_fallback(client)
        
        return discovered
    
    async def _search_events_fallback(self, client: httpx.AsyncClient) -> list[DiscoveredMarket]:
        """
        Fallback: Search events API for 15-minute crypto markets.
        
        Uses tag_id if available, otherwise searches by keywords.
        API: GET https://gamma-api.polymarket.com/events?active=true&tag_id=235
        """
        discovered = []
        
        # Get tag_id from asset patterns (if available)
        tag_id = self._patterns.get("tag_id")
        keywords = self._patterns["keywords"]
        
        self.logger.info(
            "Attempting events fallback search",
            asset=self.asset,
            tag_id=tag_id,
            keywords=keywords,
        )
        
        try:
            params = {
                "active": "true",
                "closed": "false",
                "limit": 100,
            }
            if tag_id:
                params["tag_id"] = tag_id
            
            response = await client.get(
                f"{self.GAMMA_API_URL}/events",
                params=params,
            )
            
            if response.status_code == 200:
                events = response.json()
                self.logger.debug("Events API returned", event_count=len(events))
                
                for event in events:
                    title = event.get("title", "").lower()
                    slug = event.get("slug", "").lower()
                    
                    # Look for 15-minute up/down markets
                    is_15min_market = (
                        ("15" in title and "min" in title) or 
                        "up or down" in title or
                        "updown-15m" in slug or
                        "up-or-down-15m" in slug
                    )
                    
                    if not is_15min_market:
                        continue
                    
                    # Check if title/slug contains any of our asset keywords
                    matches_asset = any(kw in title or kw in slug for kw in keywords)
                    
                    if matches_asset:
                        markets = event.get("markets", [])
                        for m in markets:
                            condition_id = m.get("conditionId") or m.get("condition_id", "")
                            if condition_id and not m.get("closed", False):
                                # Parse tokens
                                tokens_raw = m.get("clobTokenIds", [])
                                if isinstance(tokens_raw, str):
                                    try:
                                        tokens = orjson.loads(tokens_raw)
                                    except:
                                        tokens = []
                                else:
                                    tokens = tokens_raw
                                
                                discovered.append(DiscoveredMarket(
                                    condition_id=condition_id,
                                    question=m.get("question", event.get("title", "")),
                                    description=event.get("description", "")[:200],
                                    end_date_iso=event.get("endDate") or m.get("endDate", ""),
                                    tokens=tokens,
                                    outcome="up" if "up" in title else "down",
                                    liquidity=float(m.get("volume", 0) or 0),
                                ))
                                self.logger.info(
                                    "Found market via events fallback",
                                    asset=self.asset,
                                    question=event.get("title", "")[:50],
                                    condition_id=condition_id[:30],
                                )
        except Exception as e:
            self.logger.warning("Events fallback failed", error=str(e), asset=self.asset)
        
        return discovered
    
    async def get_current_market(self) -> Optional[DiscoveredMarket]:
        """Get the current active market (legacy method)."""
        return await self.get_best_market()
    
    async def get_best_market(self, min_quality: float = 0.5) -> Optional[DiscoveredMarket]:
        """
        Get the best quality 15-minute market for the configured asset.
        
        Args:
            min_quality: Minimum quality score (0-1) to consider
            
        Returns:
            Best quality market or None if none meet threshold
        """
        markets = await self.find_15min_markets()
        
        if not markets:
            return None
        
        # Score all markets
        scored_markets = []
        for market in markets:
            quality = self.assess_market_quality(market)
            if quality.total_score >= min_quality:
                scored_markets.append((market, quality))
                self.logger.debug(
                    "Market scored",
                    condition_id=market.condition_id[:30],
                    quality=quality.total_score,
                    outcome=market.outcome,
                )
        
        if not scored_markets:
            # Fall back to any market if none meet quality threshold
            self.logger.warning("No markets meet quality threshold, using best available")
            scored_markets = [(m, self.assess_market_quality(m)) for m in markets]
        
        # Sort by quality score (descending)
        scored_markets.sort(key=lambda x: x[1].total_score, reverse=True)
        
        best_market, best_quality = scored_markets[0]
        self.logger.info(
            "Selected best market",
            condition_id=best_market.condition_id[:30],
            question=best_market.question[:40],
            quality_score=best_quality.total_score,
        )
        
        return best_market
    
    async def get_all_markets_with_quality(self) -> list[tuple[DiscoveredMarket, MarketQualityScore]]:
        """
        Get all active markets with their quality scores.
        
        Returns:
            List of (market, quality_score) tuples sorted by quality
        """
        markets = await self.find_btc_15min_markets()
        
        scored = [(m, self.assess_market_quality(m)) for m in markets]
        scored.sort(key=lambda x: x[1].total_score, reverse=True)
        
        return scored
    
    async def get_market_prices(self, token_ids: list[str]) -> dict[str, float]:
        """
        Get current prices for market tokens from CLOB API.
        
        API: GET https://clob.polymarket.com/price?token_id={token_id}&side=buy
        
        Args:
            token_ids: List of clobTokenIds (YES/NO tokens)
            
        Returns:
            Dict mapping token_id to current price
        """
        prices = {}
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        async with httpx.AsyncClient(verify=ssl_context, timeout=10.0) as client:
            for token_id in token_ids:
                try:
                    response = await client.get(
                        f"{self.CLOB_API_URL}/price",
                        params={"token_id": token_id, "side": "buy"},
                    )
                    if response.status_code == 200:
                        data = response.json()
                        prices[token_id] = float(data.get("price", 0))
                except Exception as e:
                    self.logger.debug("Price fetch failed", token_id=token_id[:20], error=str(e))
        
        return prices
    
    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """
        Get full orderbook for a token from CLOB API.
        
        API: GET https://clob.polymarket.com/book?token_id={token_id}
        
        Args:
            token_id: The clobTokenId (YES or NO token)
            
        Returns:
            Orderbook dict with 'bids' and 'asks' lists, or None on error.
            Each level: {'price': float, 'size': float}
        """
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        try:
            async with httpx.AsyncClient(verify=ssl_context, timeout=10.0) as client:
                response = await client.get(
                    f"{self.CLOB_API_URL}/book",
                    params={"token_id": token_id},
                )
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Parse bids/asks
                    bids = [
                        {"price": float(b.get("price", 0)), "size": float(b.get("size", 0))}
                        for b in data.get("bids", [])
                    ]
                    asks = [
                        {"price": float(a.get("price", 0)), "size": float(a.get("size", 0))}
                        for a in data.get("asks", [])
                    ]
                    
                    return {
                        "token_id": token_id,
                        "bids": bids,
                        "asks": asks,
                        "best_bid": bids[0]["price"] if bids else 0,
                        "best_ask": asks[0]["price"] if asks else 0,
                        "spread": (asks[0]["price"] - bids[0]["price"]) if (bids and asks) else 0,
                    }
                    
        except Exception as e:
            self.logger.debug("Orderbook fetch failed", token_id=token_id[:20], error=str(e))
        
        return None


@dataclass
class LiquiditySnapshot:
    """Snapshot of liquidity at a point in time."""
    timestamp_ms: int
    yes_liquidity: float
    no_liquidity: float


class LiquidityTracker:
    """Tracks historical liquidity for collapse detection."""
    
    def __init__(self, max_age_seconds: int = 120):
        self.max_age_seconds = max_age_seconds
        self.snapshots: deque[LiquiditySnapshot] = deque()
    
    def add_snapshot(self, yes_liquidity: float, no_liquidity: float) -> None:
        """Add a new liquidity snapshot."""
        now_ms = int(time.time() * 1000)
        self.snapshots.append(LiquiditySnapshot(
            timestamp_ms=now_ms,
            yes_liquidity=yes_liquidity,
            no_liquidity=no_liquidity,
        ))
        self._cleanup()
    
    def _cleanup(self) -> None:
        """Remove old snapshots."""
        cutoff_ms = int(time.time() * 1000) - (self.max_age_seconds * 1000)
        while self.snapshots and self.snapshots[0].timestamp_ms < cutoff_ms:
            self.snapshots.popleft()
    
    def get_liquidity_at(self, seconds_ago: int) -> tuple[float, float]:
        """Get YES and NO liquidity from N seconds ago."""
        target_ms = int(time.time() * 1000) - (seconds_ago * 1000)
        
        # Find closest snapshot
        closest = None
        min_diff = float('inf')
        
        for snapshot in self.snapshots:
            diff = abs(snapshot.timestamp_ms - target_ms)
            if diff < min_diff:
                min_diff = diff
                closest = snapshot
        
        if closest and min_diff < 10000:  # Within 10 seconds
            return closest.yes_liquidity, closest.no_liquidity
        
        return 0.0, 0.0


@dataclass
class OrderbookSide:
    """One side of the orderbook (bids or asks)."""
    levels: list[OrderbookLevel] = field(default_factory=list)
    
    @property
    def best_price(self) -> float:
        """Get best price (highest bid or lowest ask)."""
        return self.levels[0].price if self.levels else 0.0
    
    @property
    def best_size(self) -> float:
        """Get size at best price."""
        return self.levels[0].size if self.levels else 0.0
    
    @property
    def total_depth(self) -> float:
        """Get total size across all levels."""
        return sum(level.size for level in self.levels)
    
    def depth_at_levels(self, n: int) -> list[float]:
        """Get sizes at first N levels."""
        return [level.size for level in self.levels[:n]]


class PolymarketFeed:
    """
    Polymarket CLOB feed for orderbook data.
    
    Uses REST API polling for reliable orderbook updates.
    Auto-discovers BTC 15-minute markets and refreshes when they expire.
    """
    
    CLOB_API_URL = "https://clob.polymarket.com"
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    
    def __init__(
        self,
        market_id: Optional[str] = None,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        snapshot_interval: float = 2.0,  # 2 second polling interval (reduce CPU)
        auto_discover: bool = True,
        asset: str = "BTC",  # Asset to trade (BTC, ETH, SOL, XRP)
    ):
        self.market_id = market_id
        self.ws_url = ws_url
        self.snapshot_interval = snapshot_interval
        self.auto_discover = auto_discover
        self.asset = asset.upper()
        self._discovery = MarketDiscovery(asset=self.asset)
        self._discovered_market: Optional[DiscoveredMarket] = None
        
        # Token IDs for YES/NO outcomes
        self._yes_token_id: Optional[str] = None
        self._no_token_id: Optional[str] = None
        
        # HTTP client for REST polling
        self._http_client: Optional[httpx.AsyncClient] = None
        
        self.logger = logger.bind(feed="polymarket", asset=self.asset, market_id=market_id or "auto")
        
        # Connection state
        self._running = False
        self.health = FeedHealth()
        
        # Orderbook state
        self._yes_bids = OrderbookSide()
        self._yes_asks = OrderbookSide()
        self._no_bids = OrderbookSide()
        self._no_asks = OrderbookSide()
        
        # Liquidity tracking
        self._liquidity_tracker = LiquidityTracker()
        
        # Callbacks
        self._callbacks: list[Callable[[PolymarketData], None]] = []
        
        # Last snapshot timestamp
        self._last_snapshot_ms: int = 0
        
        # NEW: Price change tracking for divergence strategy
        # Track when prices last changed (for PM staleness detection)
        self._last_yes_bid: float = 0.0
        self._last_yes_ask: float = 0.0
        self._last_no_bid: float = 0.0
        self._last_no_ask: float = 0.0
        self._last_price_change_ms: int = 0  # When any price last changed
        self._last_data_received_ms: int = 0  # When we last received ANY orderbook update
        
        # NEW: Depth tracking for orderbook freeze detection
        # "Freeze" = prices static but depth changing = MM about to reprice
        self._last_yes_depth: float = 0.0
        self._last_no_depth: float = 0.0
        self._freeze_window_start_ms: int = 0  # When price freeze started
        self._depth_at_freeze_start_yes: float = 0.0
        self._depth_at_freeze_start_no: float = 0.0
        
        # NEW: Fee tracking (Jan 2026 Polymarket fee update)
        self._yes_fee_rate_bps: int = 0
        self._no_fee_rate_bps: int = 0
        self._fee_rate_fetched_at: float = 0.0
        self._fee_rate_ttl: float = 60.0  # Refresh fee rates every 60 seconds
        
        # NEW: Adaptive snapshot frequency
        # Fast polling (200ms) during high activity, slow (1s) during quiet periods
        self._high_activity_mode: bool = False
        self._high_activity_until_ms: int = 0
        self._base_interval: float = snapshot_interval  # Store original interval
        self._fast_interval: float = 0.2  # 200ms during action
        self._slow_interval: float = 1.0  # 1s during quiet
    
    def add_callback(self, callback: Callable[[PolymarketData], None]) -> None:
        """Register a callback for orderbook updates."""
        self._callbacks.append(callback)
    
    def _notify_callbacks(self, data: PolymarketData) -> None:
        """Notify all registered callbacks."""
        for callback in self._callbacks:
            try:
                callback(data)
            except Exception as e:
                self.logger.error("Callback error", error=str(e))
    
    def trigger_high_activity_mode(self, duration_seconds: float = 30.0) -> None:
        """
        Trigger high activity mode for faster polling.
        
        Called when:
        - Divergence detected
        - Freeze detected
        - Signal generated
        
        Increases polling from 1s to 200ms for the specified duration.
        """
        now_ms = int(time.time() * 1000)
        self._high_activity_until_ms = now_ms + int(duration_seconds * 1000)
        self._high_activity_mode = True
        self.logger.debug(
            "High activity mode triggered",
            duration=f"{duration_seconds}s",
            interval="200ms",
        )
    
    def _get_current_interval(self) -> float:
        """Get the current polling interval based on activity mode."""
        now_ms = int(time.time() * 1000)
        
        if self._high_activity_mode:
            if now_ms < self._high_activity_until_ms:
                return self._fast_interval  # 200ms
            else:
                # High activity expired, switch back to slow
                self._high_activity_mode = False
                self.logger.debug("High activity mode expired, returning to slow polling")
        
        return self._slow_interval  # 1s
    
    async def _connect(self) -> bool:
        """Initialize HTTP client and fetch token IDs."""
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self._http_client = httpx.AsyncClient(verify=ssl_context, timeout=15.0)
            
            # Fetch market data to get token IDs
            if not await self._fetch_token_ids():
                return False
            
            self.health.connected = True
            self.logger.info(
                "Connected to Polymarket REST API",
                yes_token=self._yes_token_id[:20] + "..." if self._yes_token_id else None,
                no_token=self._no_token_id[:20] + "..." if self._no_token_id else None,
            )
            
            # Fetch initial fee rates
            await self._fetch_fee_rates()
            
            return True
        except Exception as e:
            self.health.connected = False
            self.health.error_count += 1
            self.logger.error("Connection failed", error=str(e))
            return False
    
    async def _fetch_fee_rate(self, token_id: str) -> int:
        """
        Fetch dynamic fee rate for a specific token.
        
        Returns: fee_rate_bps (e.g., 1000 = 0.1%)
        """
        if not self._http_client or not token_id:
            return 0
        
        try:
            response = await self._http_client.get(
                f"{self.CLOB_API_URL}/fee-rate",
                params={"token_id": token_id},
                timeout=5.0,
            )
            
            if response.status_code == 200:
                data = response.json()
                fee_rate_bps = data.get("fee_rate_bps", 0)
                return int(fee_rate_bps)
            else:
                self.logger.debug("Fee rate fetch failed", status=response.status_code)
                return 0
                
        except Exception as e:
            self.logger.debug("Fee rate fetch error", error=str(e))
            return 0
    
    async def _fetch_fee_rates(self) -> None:
        """
        Fetch fee rates for both YES and NO tokens.
        Caches results for _fee_rate_ttl seconds.
        """
        now = time.time()
        
        # Skip if recently fetched
        if now - self._fee_rate_fetched_at < self._fee_rate_ttl:
            return
        
        if not self._yes_token_id or not self._no_token_id:
            return
        
        try:
            # Fetch both fee rates in parallel
            yes_fee, no_fee = await asyncio.gather(
                self._fetch_fee_rate(self._yes_token_id),
                self._fetch_fee_rate(self._no_token_id),
            )
            
            self._yes_fee_rate_bps = yes_fee
            self._no_fee_rate_bps = no_fee
            self._fee_rate_fetched_at = now
            
            self.logger.info(
                "Fetched fee rates",
                yes_fee_bps=yes_fee,
                no_fee_bps=no_fee,
                yes_fee_pct=f"{yes_fee/100:.2f}%",
                no_fee_pct=f"{no_fee/100:.2f}%",
            )
            
        except Exception as e:
            self.logger.warning("Failed to fetch fee rates", error=str(e))
    
    async def _fetch_token_ids(self) -> bool:
        """Fetch clobTokenIds for the market."""
        try:
            # First check if we already have tokens from discovery
            if self._discovered_market and self._discovered_market.tokens:
                tokens = self._discovered_market.tokens
                # Parse if it's a JSON string
                if isinstance(tokens, str):
                    tokens = orjson.loads(tokens)
                if len(tokens) >= 2:
                    self._yes_token_id = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
                    self._no_token_id = tokens[1] if isinstance(tokens[1], str) else tokens[1].get("token_id")
                    return True
            
            # Use CLOB API directly - more reliable
            response = await self._http_client.get(
                f"{self.CLOB_API_URL}/markets/{self.market_id}",
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # CLOB API returns tokens array with token_id and outcome
                tokens = data.get("tokens", [])
                if len(tokens) >= 2:
                    # Find YES and NO/Down tokens by outcome
                    for token in tokens:
                        outcome = token.get("outcome", "").lower()
                        token_id = token.get("token_id", "")
                        if outcome in ("yes", "up"):
                            self._yes_token_id = token_id
                        elif outcome in ("no", "down"):
                            self._no_token_id = token_id
                    
                    if self._yes_token_id and self._no_token_id:
                        return True
            
            self.logger.error("Could not find token IDs for market")
            return False
            
        except Exception as e:
            self.logger.error("Failed to fetch token IDs", error=str(e))
            return False
    
    async def _subscribe(self) -> None:
        """No subscription needed for REST polling."""
        pass
    
    def _parse_orderbook_update(self, data: dict) -> None:
        """Parse orderbook update message."""
        try:
            # Log message type for debugging
            msg_type = data.get("type", "unknown")
            self.logger.debug("Parsing orderbook update", type=msg_type, keys=list(data.keys())[:5])
            
            # Handle different message formats based on Polymarket API
            # Direct bids/asks (for single-token markets)
            if "bids" in data:
                self._update_side(self._yes_bids, data["bids"], is_bid=True)
            if "asks" in data:
                self._update_side(self._yes_asks, data["asks"], is_bid=False)
            
            # Separate YES/NO structures (for conditional markets)
            if "yes" in data:
                yes_data = data["yes"]
                if "bids" in yes_data:
                    self._update_side(self._yes_bids, yes_data["bids"], is_bid=True)
                if "asks" in yes_data:
                    self._update_side(self._yes_asks, yes_data["asks"], is_bid=False)
            
            if "no" in data:
                no_data = data["no"]
                if "bids" in no_data:
                    self._update_side(self._no_bids, no_data["bids"], is_bid=True)
                if "asks" in no_data:
                    self._update_side(self._no_asks, no_data["asks"], is_bid=False)
            
            # Handle Polymarket CLOB format (outcomes array)
            if "outcomes" in data:
                for outcome in data["outcomes"]:
                    outcome_id = outcome.get("outcome", "").upper()
                    if "YES" in outcome_id or outcome_id == "1":
                        if "bids" in outcome:
                            self._update_side(self._yes_bids, outcome["bids"], is_bid=True)
                        if "asks" in outcome:
                            self._update_side(self._yes_asks, outcome["asks"], is_bid=False)
                    elif "NO" in outcome_id or outcome_id == "0":
                        if "bids" in outcome:
                            self._update_side(self._no_bids, outcome["bids"], is_bid=True)
                        if "asks" in outcome:
                            self._update_side(self._no_asks, outcome["asks"], is_bid=False)
                    
        except Exception as e:
            self.logger.error("Error parsing orderbook", error=str(e), data_keys=list(data.keys())[:10] if isinstance(data, dict) else "not_dict")
    
    def _update_side(self, side: OrderbookSide, updates: list, is_bid: bool) -> None:
        """Update one side of the orderbook."""
        # Convert updates to OrderbookLevel objects
        # Format: [[price, size], ...] or [{"price": x, "size": y}, ...]
        new_levels = []
        
        for update in updates:
            if isinstance(update, list) and len(update) >= 2:
                price = float(update[0])
                size = float(update[1])
            elif isinstance(update, dict):
                price = float(update.get("price", 0))
                size = float(update.get("size", 0))
            else:
                continue
            
            if size > 0:
                new_levels.append(OrderbookLevel(price=price, size=size))
        
        # Sort: bids descending, asks ascending
        if is_bid:
            new_levels.sort(key=lambda x: x.price, reverse=True)
        else:
            new_levels.sort(key=lambda x: x.price)
        
        side.levels = new_levels[:10]  # Keep top 10 levels
    
    def _should_snapshot(self) -> bool:
        """Check if enough time has passed for a new snapshot."""
        now_ms = int(time.time() * 1000)
        interval_ms = int(self.snapshot_interval * 1000)
        return now_ms - self._last_snapshot_ms >= interval_ms
    
    def _calculate_orderbook_imbalance(self) -> tuple[float, float, float]:
        """
        Calculate proper orderbook imbalance between YES and NO sides.
        
        Returns:
            (imbalance_ratio, yes_depth_total, no_depth_total)
            
        imbalance_ratio interpretation:
        - Positive (>0): YES-heavy (more YES liquidity)
        - Negative (<0): NO-heavy (more NO liquidity)
        - Zero: Balanced
        
        Range: -1.0 to +1.0
        """
        # Sum liquidity across top 5 levels for each side
        yes_depth = sum(level.size for level in self._yes_bids.levels[:5])
        no_depth = sum(level.size for level in self._no_bids.levels[:5])
        
        total_depth = yes_depth + no_depth
        
        if total_depth == 0:
            return 0.0, 0.0, 0.0
        
        # Normalized imbalance: (YES - NO) / (YES + NO)
        # Result is between -1 (all NO) and +1 (all YES)
        imbalance = (yes_depth - no_depth) / total_depth
        
        return imbalance, yes_depth, no_depth
    
    def _create_snapshot(self) -> PolymarketData:
        """Create a snapshot of current orderbook state."""
        now_ms = int(time.time() * 1000)
        
        # Get historical liquidity for collapse detection
        yes_liq_30s, no_liq_30s = self._liquidity_tracker.get_liquidity_at(30)
        yes_liq_60s, no_liq_60s = self._liquidity_tracker.get_liquidity_at(60)
        
        # Current liquidity
        current_yes_liq = self._yes_bids.best_size if self._yes_bids.levels else 0.0
        current_no_liq = self._no_bids.best_size if self._no_bids.levels else 0.0
        
        # Add to liquidity tracker
        self._liquidity_tracker.add_snapshot(current_yes_liq, current_no_liq)
        
        # Calculate spread
        yes_bid = self._yes_bids.best_price if self._yes_bids.levels else 0.0
        yes_ask = self._yes_asks.best_price if self._yes_asks.levels else 0.0
        spread = yes_ask - yes_bid if yes_ask > 0 and yes_bid > 0 else 0.0
        
        # Calculate implied probability (mid price)
        if yes_bid > 0 and yes_ask > 0:
            implied_prob = (yes_bid + yes_ask) / 2
        elif yes_bid > 0:
            implied_prob = yes_bid
        elif yes_ask > 0:
            implied_prob = yes_ask
        else:
            implied_prob = 0.5  # Default if no data
        
        # Check for liquidity collapse (IMPROVED)
        # Old logic was too sensitive for thin markets - flagged normal fluctuations
        # New logic: Only flag collapse if BOTH conditions are met:
        #   1. Percentage drop > 50% (was 40%)
        #   2. AND absolute liquidity is dangerously low (< €25)
        # This prevents false positives in naturally thin markets
        liquidity_collapsing = False
        MIN_ABSOLUTE_LIQUIDITY = 25.0  # €25 floor
        COLLAPSE_THRESHOLD_PCT = 0.50  # 50% drop (was 60%, i.e., < 0.6 * historical)
        
        if yes_liq_30s > 0 and current_yes_liq > 0:
            pct_of_historical = current_yes_liq / yes_liq_30s
            is_major_drop = pct_of_historical < COLLAPSE_THRESHOLD_PCT
            is_below_absolute_floor = current_yes_liq < MIN_ABSOLUTE_LIQUIDITY
            
            # Only flag as collapsing if it's both a major drop AND below safety floor
            liquidity_collapsing = is_major_drop and is_below_absolute_floor
        
        # Calculate proper orderbook imbalance (YES vs NO depth)
        # Positive = YES-heavy, Negative = NO-heavy
        imbalance_ratio, yes_depth_total, no_depth_total = self._calculate_orderbook_imbalance()
        
        # Get current NO prices
        no_bid = self._no_bids.best_price if self._no_bids.levels else 0.0
        no_ask = self._no_asks.best_price if self._no_asks.levels else 0.0
        
        # ====================================================================
        # NEW: Track when prices change (for PM staleness / divergence signal)
        # ====================================================================
        price_changed = (
            abs(yes_bid - self._last_yes_bid) > 0.001 or
            abs(yes_ask - self._last_yes_ask) > 0.001 or
            abs(no_bid - self._last_no_bid) > 0.001 or
            abs(no_ask - self._last_no_ask) > 0.001
        )
        
        # ====================================================================
        # NEW: Orderbook freeze detection
        # Freeze = prices static but depth changing by >10%
        # This indicates MMs are repositioning but haven't repriced yet
        # ====================================================================
        orderbook_freeze_detected = False
        depth_change_pct = 0.0
        
        if price_changed or self._last_price_change_ms == 0:
            # Prices changed - reset freeze tracking
            self._last_price_change_ms = now_ms
            self._last_yes_bid = yes_bid
            self._last_yes_ask = yes_ask
            self._last_no_bid = no_bid
            self._last_no_ask = no_ask
            # Reset freeze window
            self._freeze_window_start_ms = now_ms
            self._depth_at_freeze_start_yes = yes_depth_total
            self._depth_at_freeze_start_no = no_depth_total
        else:
            # Prices are static - check if depth is changing
            freeze_duration_ms = now_ms - self._freeze_window_start_ms
            
            # Only check freeze after 3+ seconds of static prices
            if freeze_duration_ms >= 3000:
                # Calculate depth change since freeze started
                depth_start = self._depth_at_freeze_start_yes + self._depth_at_freeze_start_no
                depth_now = yes_depth_total + no_depth_total
                
                if depth_start > 0:
                    depth_change_pct = abs(depth_now - depth_start) / depth_start
                    
                    # Freeze detected if depth changed >10% while prices static
                    if depth_change_pct > 0.10:
                        orderbook_freeze_detected = True
        
        # Track current depth for next comparison
        self._last_yes_depth = yes_depth_total
        self._last_no_depth = no_depth_total
        
        # Track when we received data (for connection health check)
        self._last_data_received_ms = now_ms
        
        # Calculate orderbook age (seconds since last price change)
        # This is for divergence strategy - stale prices = opportunity
        orderbook_age_seconds = (now_ms - self._last_price_change_ms) / 1000.0
        data_age_seconds = 0.0  # This is calculated at get_data() time
        
        self._last_snapshot_ms = now_ms
        
        return PolymarketData(
            market_id=self.market_id,
            timestamp_ms=now_ms,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            yes_liquidity_best=current_yes_liq,
            yes_depth_3=[OrderbookLevel(l.price, l.size) for l in self._yes_bids.levels[:3]],
            no_bid=no_bid,
            no_ask=no_ask,
            no_liquidity_best=current_no_liq,
            no_depth_3=[OrderbookLevel(l.price, l.size) for l in self._no_bids.levels[:3]],
            spread=spread,
            implied_probability=implied_prob,
            liquidity_30s_ago=yes_liq_30s,
            liquidity_60s_ago=yes_liq_60s,
            liquidity_collapsing=liquidity_collapsing,
            orderbook_imbalance_ratio=imbalance_ratio,
            yes_depth_total=yes_depth_total,
            no_depth_total=no_depth_total,
            # Staleness tracking for divergence strategy
            last_price_change_ms=self._last_price_change_ms,
            orderbook_age_seconds=orderbook_age_seconds,
            data_age_seconds=data_age_seconds,
            # Freeze detection (prices static but depth changing)
            orderbook_freeze_detected=orderbook_freeze_detected,
            depth_change_pct=depth_change_pct,
            # Fee tracking (Jan 2026 update)
            yes_token_id=self._yes_token_id or "",
            no_token_id=self._no_token_id or "",
            yes_fee_rate_bps=self._yes_fee_rate_bps,
            no_fee_rate_bps=self._no_fee_rate_bps,
        )
    
    async def _poll_orderbook(self) -> bool:
        """Poll REST API for orderbook data."""
        try:
            if not self._http_client or not self._yes_token_id:
                return False
            
            # Always update timestamp at start of poll to avoid stale warnings
            self.health.last_message_ms = int(time.time() * 1000)
            
            # Fetch YES and NO orderbooks concurrently
            yes_response, no_response = await asyncio.gather(
                self._http_client.get(
                    f"{self.CLOB_API_URL}/book",
                    params={"token_id": self._yes_token_id},
                ),
                self._http_client.get(
                    f"{self.CLOB_API_URL}/book",
                    params={"token_id": self._no_token_id},
                ),
            )
            
            if yes_response.status_code == 200:
                yes_book = yes_response.json()
                self._update_side(self._yes_bids, yes_book.get("bids", []), is_bid=True)
                self._update_side(self._yes_asks, yes_book.get("asks", []), is_bid=False)
            
            if no_response.status_code == 200:
                no_book = no_response.json()
                self._update_side(self._no_bids, no_book.get("bids", []), is_bid=True)
                self._update_side(self._no_asks, no_book.get("asks", []), is_bid=False)
            
            return True
            
        except Exception as e:
            self.logger.debug("Orderbook poll failed", error=str(e))
            return False
    
    async def _poll_loop(self) -> None:
        """Main polling loop for REST API."""
        self.logger.info("Poll loop started")
        poll_count = 0
        
        while self._running:
            try:
                # Always update heartbeat
                self.health.last_message_ms = int(time.time() * 1000)
                
                if not self._http_client:
                    if not await self._connect():
                        await asyncio.sleep(1)
                        continue
                
                # Refresh fee rates periodically (every 60s, handled by TTL in method)
                await self._fetch_fee_rates()
                
                # Poll orderbook
                success = await self._poll_orderbook()
                poll_count += 1
                
                # Log progress every 30 polls (30 seconds at 1s interval)
                if poll_count % 30 == 0:
                    self.logger.debug(
                        "Poll progress",
                        polls=poll_count,
                        has_data=self.has_orderbook_data(),
                        yes_bid=self._yes_bids.best_price if self._yes_bids.levels else 0,
                    )
                
                if success:
                    # Create snapshot
                    if self._should_snapshot():
                        snapshot = self._create_snapshot()
                        if snapshot:
                            self._notify_callbacks(snapshot)
                            
                            # Auto-trigger high activity on freeze detection
                            if snapshot.orderbook_freeze_detected:
                                self.trigger_high_activity_mode(duration_seconds=15.0)
                
                # Wait for next poll interval (adaptive)
                interval = self._get_current_interval()
                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                self.logger.info("Poll loop cancelled")
                break
            except Exception as e:
                self.logger.error("Poll error", error=str(e))
                self.health.error_count += 1
                await asyncio.sleep(1)
    
    async def _discover_market(self, force: bool = False) -> bool:
        """
        Discover and set the market ID.
        
        Args:
            force: If True, re-discover even if market_id is set
        """
        if self.market_id and not force:
            return True
        
        if not self.auto_discover and not force:
            self.logger.error("No market_id provided and auto_discover is disabled")
            return False
        
        self.logger.info(f"Discovering {self.asset} 15-minute market...")
        
        try:
            market = await self._discovery.get_current_market()
            
            if market:
                old_market_id = self.market_id
                self.market_id = market.condition_id
                self._discovered_market = market
                self.logger = logger.bind(
                    feed="polymarket", 
                    market_id=self.market_id[:30] + "..." if len(self.market_id) > 30 else self.market_id,
                    outcome=market.outcome,
                )
                
                # Clear orderbook if switching markets
                if old_market_id and old_market_id != self.market_id:
                    self._clear_orderbook()
                    self.logger.info(
                        "Switched to new market",
                        old=old_market_id[:30] if old_market_id else None,
                        new=self.market_id[:30],
                        question=market.question[:60],
                    )
                else:
                    self.logger.info(
                        "Discovered market",
                        question=market.question[:60],
                        outcome=market.outcome,
                    )
                return True
            else:
                self.logger.warning(f"No {self.asset} 15-minute markets found - will retry")
                return False
                
        except Exception as e:
            self.logger.error("Market discovery failed", error=str(e))
            return False
    
    def _clear_orderbook(self) -> None:
        """Clear orderbook data when switching markets."""
        self._yes_bids = OrderbookSide()
        self._yes_asks = OrderbookSide()
        self._no_bids = OrderbookSide()
        self._no_asks = OrderbookSide()
        self._liquidity_tracker = LiquidityTracker()
        self._last_snapshot_ms = 0
    
    async def _market_refresh_loop(self) -> None:
        """
        Background task to refresh market when current one expires.
        
        Checks every 30 seconds if market needs to be refreshed.
        Auto-discovers new market 60 seconds before current window ends.
        """
        # Wait for initial discovery
        await asyncio.sleep(5)
        
        while self._running:
            try:
                # Calculate time until next 15-min window
                now = int(time.time())
                current_window_end = ((now // 900) + 1) * 900
                time_until_end = current_window_end - now
                
                # If less than 60 seconds until end, start looking for next market
                if time_until_end < 60:
                    self.logger.info(
                        "Market window ending soon, preparing to refresh",
                        seconds_remaining=time_until_end,
                    )
                    
                    # Wait for window to end
                    await asyncio.sleep(time_until_end + 5)
                    
                    # Discover new market
                    if await self._discover_market(force=True):
                        # Reconnect with new token IDs
                        if not await self._fetch_token_ids():
                            self.logger.warning("Failed to fetch new token IDs")
                        else:
                            self.logger.info("Connected to new market")
                    else:
                        self.logger.warning("Failed to discover new market, will retry")
                
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Market refresh error", error=str(e))
                await asyncio.sleep(30)
    
    async def start(self) -> None:
        """Start the Polymarket feed."""
        self._running = True
        self.logger.info("Starting Polymarket feed (REST polling mode)")
        
        # Discover market if needed
        if not await self._discover_market():
            self.logger.error("Cannot start - no market ID")
            return
        
        # Connect and fetch token IDs
        if not await self._connect():
            self.logger.error("Cannot start - connection failed")
            return
        
        # Run poll loop and market refresh loop concurrently
        await asyncio.gather(
            self._poll_loop(),
            self._market_refresh_loop(),
            return_exceptions=True,
        )
    
    async def stop(self) -> None:
        """Stop the feed."""
        self._running = False
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self.health.connected = False
        self.logger.info("Stopped Polymarket feed")
    
    def get_data(self) -> Optional[PolymarketData]:
        """Get current orderbook snapshot."""
        # Always return a snapshot, even if orderbook is empty
        # This allows the bot to check market availability and connection status
        try:
            snapshot = self._create_snapshot()
            # Only return None if we have no connection and no data
            if not self.health.connected and not self._yes_bids.levels:
                return None
            return snapshot
        except Exception as e:
            self.logger.debug("Could not create snapshot", error=str(e))
            # Return None only if we can't create snapshot at all
            return None
    
    def has_orderbook_data(self) -> bool:
        """Check if we have any orderbook data."""
        return (
            len(self._yes_bids.levels) > 0 or
            len(self._yes_asks.levels) > 0 or
            len(self._no_bids.levels) > 0 or
            len(self._no_asks.levels) > 0
        )
    
    def get_metrics(self) -> dict:
        """Get feed health metrics."""
        return {
            "name": "polymarket",
            "market_id": self.market_id[:30] + "..." if self.market_id and len(self.market_id) > 30 else self.market_id,
            "connected": self.health.connected,
            "is_stale": self.health.is_stale,
            "age_ms": self.health.age_ms,
            "error_count": self.health.error_count,
            "has_orderbook_data": self.has_orderbook_data(),
            "yes_bid": self._yes_bids.best_price if self._yes_bids.levels else 0,
            "yes_ask": self._yes_asks.best_price if self._yes_asks.levels else 0,
            "spread": (self._yes_asks.best_price - self._yes_bids.best_price) if (self._yes_asks.levels and self._yes_bids.levels) else 0,
            "discovered_market": self._discovered_market.question[:50] if self._discovered_market else None,
            "market_outcome": self._discovered_market.outcome if self._discovered_market else None,
        }
    
    def get_discovered_market(self) -> Optional[DiscoveredMarket]:
        """Get the discovered market info."""
        return self._discovered_market
    
    async def get_live_orderbook(self, market_id: Optional[str] = None) -> dict:
        """
        Get FRESH orderbook snapshot (not cached) for pre-trade slippage simulation.
        
        Args:
            market_id: Optional market ID (uses current if not specified)
            
        Returns:
            Dict with 'bids' and 'asks' lists
        """
        target_market = market_id or self.market_id
        if not target_market or not self._http_client:
            return {'bids': [], 'asks': []}
        
        try:
            # Fetch fresh orderbook directly
            response = await self._http_client.get(
                f"{self.CLOB_API_URL}/book",
                params={"token_id": self._yes_token_id},
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'bids': data.get('bids', []),
                    'asks': data.get('asks', [])
                }
        except Exception as e:
            self.logger.debug("Live orderbook fetch failed", error=str(e))
        
        return {'bids': [], 'asks': []}
    
    def simulate_fill(self, side: str, size_eur: float) -> dict:
        """
        Simulate order fill using current orderbook state.
        
        Args:
            side: 'YES' or 'NO'
            size_eur: Position size in EUR
            
        Returns:
            Dict with fill simulation results
        """
        levels = self._yes_bids.levels if side.upper() == 'YES' else self._no_bids.levels
        
        if not levels:
            return {
                'avg_price': 0.0,
                'filled_shares': 0.0,
                'unfilled_size': size_eur,
                'slippage': 1.0,
                'can_fill': False,
            }
        
        remaining_eur = size_eur
        filled_shares = 0.0
        total_cost = 0.0
        entry_price = levels[0].price
        
        for level in levels:
            if remaining_eur <= 0:
                break
            
            level_price = level.price
            level_size_eur = level.size * level_price  # Convert shares to EUR value
            
            fill_eur = min(remaining_eur, level_size_eur)
            fill_shares = fill_eur / level_price if level_price > 0 else 0
            
            filled_shares += fill_shares
            total_cost += fill_eur
            remaining_eur -= fill_eur
        
        avg_price = total_cost / filled_shares if filled_shares > 0 else 0
        slippage = abs(avg_price - entry_price) / entry_price if entry_price > 0 else 1.0
        
        return {
            'avg_price': avg_price,
            'filled_shares': filled_shares,
            'unfilled_size': remaining_eur,
            'slippage': slippage,
            'can_fill': remaining_eur < 0.1 * size_eur,  # Can fill if <10% unfilled
        }
    
    def get_market_quality_score(self) -> float:
        """Get quality score for current market."""
        if not self._discovered_market:
            return 0.0
        
        quality = self._discovery.assess_market_quality(self._discovered_market)
        return quality.total_score

