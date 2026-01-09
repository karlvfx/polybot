"""
The Odds API Feed.

Aggregates odds from 40+ sportsbooks including Pinnacle, Betfair, DraftKings, etc.
Free tier: 500 requests/month. Paid: $20/mo for 10k requests.

API Docs: https://the-odds-api.com/liveapi/guides/v4/

Key endpoints:
- /sports: List available sports
- /sports/{sport}/odds: Get odds for events
- /sports/{sport}/events: Get events without odds (cheaper)

This is our starting point because:
1. Free tier available for testing
2. Includes Pinnacle (the sharpest book)
3. Simple REST API
4. Good documentation
"""

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

import certifi
import httpx
import structlog

from src.sports.models.schemas import (
    Sport,
    SportEvent,
    Outcome,
    SharpOdds,
    SharpBookData,
    OddsFormat,
)

logger = structlog.get_logger()


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class OddsAPIConfig:
    """Configuration for The Odds API."""
    api_key: str
    base_url: str = "https://api.the-odds-api.com/v4"
    
    # Rate limiting
    requests_per_minute: int = 10  # Conservative for free tier
    
    # Bookmakers to fetch (prioritize sharp books)
    # Sharp books first: pinnacle, betfair, circa
    bookmakers: list[str] = field(default_factory=lambda: [
        "pinnacle",
        "betfair_ex_eu",  # Betfair Exchange
        "betfair",
        "circa",  # Super sharp US book
        "draftkings",
        "fanduel",
        "bovada",
    ])
    
    # Markets to fetch
    markets: list[str] = field(default_factory=lambda: [
        "h2h",      # Moneyline / Match Winner
        "spreads",  # Point spread / Handicap
        "totals",   # Over/Under
    ])
    
    # Regions (affects available books)
    regions: list[str] = field(default_factory=lambda: ["us", "eu", "uk"])
    
    # Sports to monitor (The Odds API sport keys)
    sports: list[str] = field(default_factory=lambda: [
        "americanfootball_nfl",
        "basketball_nba",
        "soccer_epl",
        "soccer_uefa_champs_league",
        "icehockey_nhl",
        "baseball_mlb",
    ])


# =============================================================================
# Feed Implementation
# =============================================================================

class OddsAPIFeed:
    """
    Real-time odds feed from The Odds API.
    
    Fetches odds from multiple bookmakers, prioritizing sharp books
    (Pinnacle, Betfair) for accurate "fair" odds calculation.
    
    Usage:
        feed = OddsAPIFeed(api_key="your_key")
        await feed.start()
        
        # Get events with odds
        events = await feed.get_upcoming_events(Sport.NFL)
        
        # Get sharp consensus for an event
        sharp_data = feed.get_sharp_book_data(event_id)
    """
    
    def __init__(
        self,
        api_key: str,
        config: Optional[OddsAPIConfig] = None,
        poll_interval: float = 30.0,  # Poll every 30 seconds
    ):
        self.api_key = api_key
        self.config = config or OddsAPIConfig(api_key=api_key)
        self.poll_interval = poll_interval
        
        self.logger = logger.bind(feed="odds_api")
        
        # HTTP client
        self._http_client: Optional[httpx.AsyncClient] = None
        
        # State
        self._running = False
        self._events: dict[str, SportEvent] = {}  # event_id -> SportEvent
        self._last_poll_ms: int = 0
        
        # Rate limiting
        self._request_timestamps: list[float] = []
        self._requests_remaining: int = 500  # Free tier limit
        self._requests_used: int = 0
        
        # Callbacks
        self._callbacks: list[Callable[[SportEvent], None]] = []
        
        # Health
        self._connected: bool = False
        self._error_count: int = 0
        self._last_success_ms: int = 0
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def start(self) -> None:
        """Start the feed."""
        self.logger.info("Starting Odds API feed")
        self._running = True
        
        # Initialize HTTP client
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._http_client = httpx.AsyncClient(
            verify=ssl_context,
            timeout=15.0,
            headers={"Accept": "application/json"},
        )
        
        # Start polling loop
        await self._poll_loop()
    
    async def stop(self) -> None:
        """Stop the feed."""
        self.logger.info("Stopping Odds API feed")
        self._running = False
        
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        
        self._connected = False
    
    def add_callback(self, callback: Callable[[SportEvent], None]) -> None:
        """Register callback for event updates."""
        self._callbacks.append(callback)
    
    def _notify_callbacks(self, event: SportEvent) -> None:
        """Notify all registered callbacks."""
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                self.logger.error("Callback error", error=str(e))
    
    # =========================================================================
    # Rate Limiting
    # =========================================================================
    
    async def _wait_for_rate_limit(self) -> None:
        """Wait if we're hitting rate limits."""
        now = time.time()
        
        # Clean old timestamps (older than 1 minute)
        self._request_timestamps = [
            ts for ts in self._request_timestamps
            if now - ts < 60
        ]
        
        # Check if we're at the limit
        if len(self._request_timestamps) >= self.config.requests_per_minute:
            wait_time = 60 - (now - self._request_timestamps[0])
            if wait_time > 0:
                self.logger.debug("Rate limit reached, waiting", seconds=wait_time)
                await asyncio.sleep(wait_time)
    
    def _track_request(self, requests_used: int = 1) -> None:
        """Track API request for rate limiting."""
        self._request_timestamps.append(time.time())
        self._requests_used += requests_used
    
    # =========================================================================
    # API Calls
    # =========================================================================
    
    async def _make_request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
    ) -> Optional[dict | list]:
        """Make an API request with rate limiting."""
        if not self._http_client:
            return None
        
        await self._wait_for_rate_limit()
        
        url = f"{self.config.base_url}{endpoint}"
        full_params = {"apiKey": self.api_key}
        if params:
            full_params.update(params)
        
        try:
            response = await self._http_client.get(url, params=full_params)
            
            # Track usage from headers
            if "x-requests-remaining" in response.headers:
                self._requests_remaining = int(response.headers["x-requests-remaining"])
            if "x-requests-used" in response.headers:
                requests_used = int(response.headers["x-requests-used"])
                self._track_request(requests_used)
                self.logger.debug(
                    "API request",
                    endpoint=endpoint,
                    used=requests_used,
                    remaining=self._requests_remaining,
                )
            
            if response.status_code == 200:
                self._connected = True
                self._last_success_ms = int(time.time() * 1000)
                return response.json()
            elif response.status_code == 401:
                self.logger.error("Invalid API key")
                self._connected = False
            elif response.status_code == 429:
                self.logger.warning("Rate limited by API")
                await asyncio.sleep(60)
            else:
                self.logger.warning(
                    "API error",
                    status=response.status_code,
                    body=response.text[:200],
                )
                self._error_count += 1
            
            return None
            
        except Exception as e:
            self.logger.error("Request failed", error=str(e))
            self._error_count += 1
            return None
    
    async def get_available_sports(self) -> list[dict]:
        """
        Get list of available sports.
        
        Returns:
            List of sport dicts with keys: key, group, title, description, active
        """
        data = await self._make_request("/sports")
        return data if isinstance(data, list) else []
    
    async def get_upcoming_events(
        self,
        sport: Sport,
        include_odds: bool = True,
    ) -> list[SportEvent]:
        """
        Get upcoming events for a sport with odds from sharp books.
        
        Args:
            sport: The sport to fetch
            include_odds: Whether to include odds (uses more API quota)
        
        Returns:
            List of SportEvent objects with sharp odds attached
        """
        sport_key = sport.value
        
        if include_odds:
            # Get odds (more expensive but includes prices)
            params = {
                "regions": ",".join(self.config.regions),
                "markets": ",".join(self.config.markets),
                "oddsFormat": "american",
                "bookmakers": ",".join(self.config.bookmakers[:5]),  # Limit to save quota
            }
            data = await self._make_request(f"/sports/{sport_key}/odds", params)
        else:
            # Just get events (cheaper)
            data = await self._make_request(f"/sports/{sport_key}/events")
        
        if not data or not isinstance(data, list):
            return []
        
        events = []
        for event_data in data:
            event = self._parse_event(event_data, sport)
            if event:
                events.append(event)
                self._events[event.event_id] = event
        
        self.logger.info(
            "Fetched events",
            sport=sport.name,
            count=len(events),
            requests_remaining=self._requests_remaining,
        )
        
        return events
    
    def _parse_event(self, data: dict, sport: Sport) -> Optional[SportEvent]:
        """Parse API response into SportEvent."""
        try:
            # Parse commence time
            commence_str = data.get("commence_time", "")
            if commence_str:
                commence_time = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                commence_time_ms = int(commence_time.timestamp() * 1000)
            else:
                commence_time = datetime.utcnow()
                commence_time_ms = int(time.time() * 1000)
            
            event = SportEvent(
                event_id=data.get("id", ""),
                sport=sport,
                league=data.get("sport_key", sport.value),
                home_team=data.get("home_team", ""),
                away_team=data.get("away_team", ""),
                commence_time=commence_time,
                commence_time_ms=commence_time_ms,
                is_live=False,  # The Odds API doesn't provide live status directly
            )
            
            # Parse bookmaker odds
            bookmakers = data.get("bookmakers", [])
            for book_data in bookmakers:
                sharp_odds = self._parse_bookmaker_odds(book_data)
                if sharp_odds:
                    event.sharp_odds[sharp_odds.bookmaker] = sharp_odds
            
            return event
            
        except Exception as e:
            self.logger.debug("Failed to parse event", error=str(e))
            return None
    
    def _parse_bookmaker_odds(self, data: dict) -> Optional[SharpOdds]:
        """Parse bookmaker odds from API response."""
        try:
            bookmaker = data.get("key", "")
            last_update = data.get("last_update", "")
            
            # Parse last update time
            if last_update:
                update_dt = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
                last_update_ms = int(update_dt.timestamp() * 1000)
            else:
                last_update_ms = int(time.time() * 1000)
            
            # Parse markets
            markets = data.get("markets", [])
            
            for market in markets:
                market_type = market.get("key", "h2h")
                outcomes_data = market.get("outcomes", [])
                
                outcomes = []
                for outcome_data in outcomes_data:
                    name = outcome_data.get("name", "")
                    price = outcome_data.get("price", 0)
                    point = outcome_data.get("point")  # For spreads/totals
                    
                    outcome = Outcome.from_american(
                        name=name,
                        american_odds=price,
                        line=point,
                    )
                    outcomes.append(outcome)
                
                if outcomes:
                    sharp_odds = SharpOdds(
                        bookmaker=bookmaker,
                        market_type=market_type,
                        outcomes=outcomes,
                        last_update_ms=last_update_ms,
                    )
                    
                    # Calculate vig and fair probabilities
                    sharp_odds.calculate_vig_and_fair_odds()
                    
                    return sharp_odds
            
            return None
            
        except Exception as e:
            self.logger.debug("Failed to parse bookmaker odds", error=str(e))
            return None
    
    # =========================================================================
    # Sharp Book Data
    # =========================================================================
    
    def get_sharp_book_data(self, event_id: str) -> Optional[SharpBookData]:
        """
        Get aggregated sharp book data for an event.
        
        This is the "truth" we compare against Polymarket.
        Prioritizes Pinnacle > Betfair > other sharp books.
        """
        event = self._events.get(event_id)
        if not event:
            return None
        
        # Get best sharp odds
        sharp_odds = event.best_sharp_odds
        if not sharp_odds or len(sharp_odds.outcomes) < 2:
            return None
        
        # Extract fair probabilities
        # For 2-way markets (h2h): [home, away]
        # For 3-way markets (soccer): [home, draw, away]
        outcomes = sharp_odds.outcomes
        
        fair_home = 0.0
        fair_away = 0.0
        fair_draw = 0.0
        
        for outcome in outcomes:
            name_lower = outcome.name.lower()
            if outcome.name == event.home_team or "home" in name_lower:
                fair_home = outcome.fair_prob
            elif outcome.name == event.away_team or "away" in name_lower:
                fair_away = outcome.fair_prob
            elif "draw" in name_lower or "tie" in name_lower:
                fair_draw = outcome.fair_prob
        
        # If we couldn't match names, use position
        if fair_home == 0 and fair_away == 0 and len(outcomes) >= 2:
            fair_home = outcomes[0].fair_prob
            fair_away = outcomes[1].fair_prob
            if len(outcomes) >= 3:
                fair_draw = outcomes[2].fair_prob
        
        # Calculate agreement score across books
        books_with_odds = len(event.sharp_odds)
        agreement_score = 1.0
        
        if books_with_odds >= 2:
            # Compare fair probs across books
            home_probs = []
            for book_odds in event.sharp_odds.values():
                for outcome in book_odds.outcomes:
                    if outcome.name == event.home_team:
                        home_probs.append(outcome.fair_prob)
                        break
            
            if len(home_probs) >= 2:
                # Agreement = 1 - (max deviation / avg)
                avg = sum(home_probs) / len(home_probs)
                max_dev = max(abs(p - avg) for p in home_probs)
                agreement_score = max(0, 1 - (max_dev / avg) if avg > 0 else 0)
        
        return SharpBookData(
            event=event,
            primary_book=sharp_odds.bookmaker,
            fair_home_prob=fair_home,
            fair_away_prob=fair_away,
            fair_draw_prob=fair_draw,
            vig=sharp_odds.vig,
            last_update_ms=sharp_odds.last_update_ms,
            is_stale=sharp_odds.is_stale,
            books_count=books_with_odds,
            agreement_score=agreement_score,
        )
    
    def get_all_sharp_data(self) -> list[SharpBookData]:
        """Get sharp book data for all tracked events."""
        results = []
        for event_id in self._events:
            data = self.get_sharp_book_data(event_id)
            if data:
                results.append(data)
        return results
    
    # =========================================================================
    # Polling
    # =========================================================================
    
    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                # Poll each configured sport
                for sport_key in self.config.sports:
                    sport = Sport.from_string(sport_key)
                    if sport:
                        await self.get_upcoming_events(sport)
                    
                    # Small delay between sports to spread load
                    await asyncio.sleep(2)
                
                self._last_poll_ms = int(time.time() * 1000)
                
                # Wait for next poll interval
                await asyncio.sleep(self.poll_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Poll loop error", error=str(e))
                await asyncio.sleep(10)
    
    # =========================================================================
    # Metrics
    # =========================================================================
    
    def get_metrics(self) -> dict:
        """Get feed health metrics."""
        return {
            "name": "odds_api",
            "connected": self._connected,
            "events_tracked": len(self._events),
            "requests_remaining": self._requests_remaining,
            "requests_used": self._requests_used,
            "error_count": self._error_count,
            "last_poll_ms": self._last_poll_ms,
            "age_seconds": (int(time.time() * 1000) - self._last_success_ms) / 1000 if self._last_success_ms else 0,
        }
    
    def get_event(self, event_id: str) -> Optional[SportEvent]:
        """Get a tracked event by ID."""
        return self._events.get(event_id)
    
    def get_events_by_sport(self, sport: Sport) -> list[SportEvent]:
        """Get all tracked events for a sport."""
        return [
            e for e in self._events.values()
            if e.sport == sport
        ]


# =============================================================================
# Standalone Usage
# =============================================================================

async def main():
    """Test the Odds API feed."""
    import os
    
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        print("Set ODDS_API_KEY environment variable")
        return
    
    feed = OddsAPIFeed(api_key=api_key)
    
    # Just fetch once (don't start full polling loop)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    feed._http_client = httpx.AsyncClient(verify=ssl_context, timeout=15.0)
    
    # Get NFL events
    events = await feed.get_upcoming_events(Sport.NFL)
    
    print(f"\n{'='*60}")
    print(f"Found {len(events)} NFL events")
    print(f"{'='*60}\n")
    
    for event in events[:5]:  # Show first 5
        print(f"{event.get_display_name()}")
        print(f"  Starts: {event.commence_time}")
        
        sharp_data = feed.get_sharp_book_data(event.event_id)
        if sharp_data:
            print(f"  Sharp Book: {sharp_data.primary_book}")
            print(f"  Home Win: {sharp_data.fair_home_prob:.1%}")
            print(f"  Away Win: {sharp_data.fair_away_prob:.1%}")
            print(f"  Vig: {sharp_data.vig:.2%}")
            print(f"  Books: {sharp_data.books_count}")
        print()
    
    await feed._http_client.aclose()
    print(f"Requests used: {feed._requests_used}")
    print(f"Requests remaining: {feed._requests_remaining}")


if __name__ == "__main__":
    asyncio.run(main())

