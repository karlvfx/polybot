"""
Pinnacle API Feed (Skeleton).

Pinnacle is the GOLD STANDARD for sharp odds:
- Lowest vig in the industry (2-3%)
- Accepts professional bettors (doesn't limit winners)
- Their lines move the market

API Docs: https://pinnacleapi.github.io/

Note: Pinnacle API requires account approval.
This is a skeleton for when you have access.

For now, use The Odds API which includes Pinnacle data.
"""

import asyncio
import ssl
import time
from dataclasses import dataclass
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
)

logger = structlog.get_logger()


@dataclass
class PinnacleConfig:
    """Pinnacle API configuration."""
    username: str = ""
    password: str = ""
    base_url: str = "https://api.pinnacle.com/v2"
    
    # Rate limiting
    requests_per_minute: int = 60
    
    # Sports to monitor (Pinnacle sport IDs)
    sports: dict = None
    
    def __post_init__(self):
        if self.sports is None:
            self.sports = {
                Sport.NFL: 1,       # American Football
                Sport.NBA: 2,       # Basketball
                Sport.MLB: 3,       # Baseball
                Sport.NHL: 4,       # Hockey
                Sport.EPL: 29,      # Soccer - England Premier League
                Sport.UFC: 22,      # MMA
            }


class PinnacleFeed:
    """
    Direct Pinnacle API feed.
    
    Advantages over The Odds API:
    - Real-time updates (not polled)
    - Full line history
    - All markets (not just h2h/spread/total)
    
    Requirements:
    - Pinnacle account with API access
    - Account approval (they review applications)
    
    Usage:
        feed = PinnacleFeed(username="x", password="y")
        await feed.start()
        
        events = await feed.get_fixtures(Sport.NFL)
        odds = await feed.get_odds(Sport.NFL, league_id=123)
    """
    
    def __init__(self, config: Optional[PinnacleConfig] = None):
        self.config = config or PinnacleConfig()
        self.logger = logger.bind(feed="pinnacle")
        
        self._http_client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._last_request_ms = 0
        
        # Cache
        self._events: dict[str, SportEvent] = {}
        self._leagues: dict[int, dict] = {}  # league_id -> league info
    
    async def start(self) -> None:
        """Start the Pinnacle feed."""
        if not self.config.username or not self.config.password:
            self.logger.warning("Pinnacle credentials not configured")
            return
        
        self.logger.info("Starting Pinnacle feed")
        self._running = True
        
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._http_client = httpx.AsyncClient(
            verify=ssl_context,
            timeout=15.0,
            auth=(self.config.username, self.config.password),
        )
        
        # TODO: Implement full polling loop
        # await self._poll_loop()
    
    async def stop(self) -> None:
        """Stop the feed."""
        self._running = False
        if self._http_client:
            await self._http_client.aclose()
        self.logger.info("Pinnacle feed stopped")
    
    async def _make_request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make authenticated API request."""
        if not self._http_client:
            return None
        
        # Rate limiting
        now_ms = int(time.time() * 1000)
        min_interval_ms = 60_000 // self.config.requests_per_minute
        wait_ms = min_interval_ms - (now_ms - self._last_request_ms)
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        
        try:
            url = f"{self.config.base_url}{endpoint}"
            response = await self._http_client.get(url, params=params or {})
            self._last_request_ms = int(time.time() * 1000)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                self.logger.error("Pinnacle authentication failed")
            else:
                self.logger.warning("Pinnacle API error", status=response.status_code)
            
            return None
            
        except Exception as e:
            self.logger.error("Pinnacle request failed", error=str(e))
            return None
    
    # =========================================================================
    # API Endpoints (Skeleton - implement when you have access)
    # =========================================================================
    
    async def get_sports(self) -> list[dict]:
        """
        Get list of available sports.
        
        GET /v2/sports
        
        Response:
        {
            "sports": [
                {"id": 1, "name": "Football", "hasOfferings": true},
                ...
            ]
        }
        """
        data = await self._make_request("/sports")
        return data.get("sports", []) if data else []
    
    async def get_leagues(self, sport: Sport) -> list[dict]:
        """
        Get leagues for a sport.
        
        GET /v2/leagues?sportId={sportId}
        
        Response:
        {
            "leagues": [
                {"id": 1, "name": "NFL", "hasOfferings": true},
                ...
            ]
        }
        """
        sport_id = self.config.sports.get(sport)
        if not sport_id:
            return []
        
        data = await self._make_request("/leagues", {"sportId": sport_id})
        leagues = data.get("leagues", []) if data else []
        
        # Cache leagues
        for league in leagues:
            self._leagues[league["id"]] = league
        
        return leagues
    
    async def get_fixtures(self, sport: Sport, league_id: Optional[int] = None) -> list[SportEvent]:
        """
        Get upcoming fixtures (events) for a sport/league.
        
        GET /v2/fixtures?sportId={sportId}&leagueIds={leagueIds}
        
        Response:
        {
            "sportId": 1,
            "last": 123456789,  # For polling changes
            "league": [
                {
                    "id": 1,
                    "events": [
                        {
                            "id": 123,
                            "starts": "2026-01-28T19:00:00Z",
                            "home": "Kansas City Chiefs",
                            "away": "Baltimore Ravens",
                            "rotNum": "101",
                            "liveStatus": 0,
                            "status": "O",  # O=Open, H=Halftime, L=Live, I=Intermission
                            "parlayRestriction": 0
                        }
                    ]
                }
            ]
        }
        """
        sport_id = self.config.sports.get(sport)
        if not sport_id:
            return []
        
        params = {"sportId": sport_id}
        if league_id:
            params["leagueIds"] = str(league_id)
        
        data = await self._make_request("/fixtures", params)
        if not data:
            return []
        
        events = []
        for league_data in data.get("league", []):
            for event_data in league_data.get("events", []):
                event = self._parse_fixture(event_data, sport, league_data.get("id", 0))
                if event:
                    events.append(event)
                    self._events[event.event_id] = event
        
        return events
    
    async def get_odds(
        self,
        sport: Sport,
        league_id: Optional[int] = None,
        event_id: Optional[int] = None,
    ) -> dict[str, SharpOdds]:
        """
        Get current odds for a sport/league/event.
        
        GET /v2/odds?sportId={sportId}&leagueIds={leagueIds}&oddsFormat=american
        
        Response:
        {
            "sportId": 1,
            "last": 123456789,
            "leagues": [
                {
                    "id": 1,
                    "events": [
                        {
                            "id": 123,
                            "periods": [
                                {
                                    "number": 0,  # 0=Game, 1=1st Half, etc.
                                    "moneyline": {
                                        "home": -150,
                                        "away": 130,
                                        "draw": null
                                    },
                                    "spreads": [
                                        {
                                            "hdp": -3.5,  # Handicap
                                            "home": -110,
                                            "away": -110
                                        }
                                    ],
                                    "totals": [
                                        {
                                            "points": 45.5,
                                            "over": -110,
                                            "under": -110
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        """
        sport_id = self.config.sports.get(sport)
        if not sport_id:
            return {}
        
        params = {
            "sportId": sport_id,
            "oddsFormat": "american",
        }
        if league_id:
            params["leagueIds"] = str(league_id)
        if event_id:
            params["eventIds"] = str(event_id)
        
        data = await self._make_request("/odds", params)
        if not data:
            return {}
        
        odds_map = {}
        for league_data in data.get("leagues", []):
            for event_data in league_data.get("events", []):
                eid = str(event_data.get("id", ""))
                sharp_odds = self._parse_odds(event_data)
                if sharp_odds:
                    odds_map[eid] = sharp_odds
        
        return odds_map
    
    async def get_line_history(
        self,
        sport: Sport,
        event_id: int,
        period_number: int = 0,
        bet_type: str = "moneyline",  # moneyline, spread, total
    ) -> list[dict]:
        """
        Get line movement history for an event.
        
        GET /v2/line?sportId={sportId}&eventId={eventId}&periodNumber=0&betType=MONEYLINE
        
        This is INCREDIBLY valuable for:
        - Seeing line movement (steam moves)
        - Detecting sharp action
        - Understanding where the market is heading
        """
        sport_id = self.config.sports.get(sport)
        if not sport_id:
            return []
        
        bet_type_map = {
            "moneyline": "MONEYLINE",
            "spread": "SPREAD",
            "total": "TOTAL_POINTS",
        }
        
        params = {
            "sportId": sport_id,
            "eventId": event_id,
            "periodNumber": period_number,
            "betType": bet_type_map.get(bet_type, "MONEYLINE"),
        }
        
        data = await self._make_request("/line", params)
        return data if data else []
    
    def _parse_fixture(self, data: dict, sport: Sport, league_id: int) -> Optional[SportEvent]:
        """Parse fixture data into SportEvent."""
        try:
            from datetime import datetime
            
            event_id = str(data.get("id", ""))
            starts = data.get("starts", "")
            
            if starts:
                commence_time = datetime.fromisoformat(starts.replace("Z", "+00:00"))
                commence_time_ms = int(commence_time.timestamp() * 1000)
            else:
                commence_time = datetime.utcnow()
                commence_time_ms = int(time.time() * 1000)
            
            status = data.get("status", "O")
            is_live = status in ("L", "H", "I")  # Live, Halftime, Intermission
            
            return SportEvent(
                event_id=event_id,
                sport=sport,
                league=str(league_id),
                home_team=data.get("home", ""),
                away_team=data.get("away", ""),
                commence_time=commence_time,
                commence_time_ms=commence_time_ms,
                is_live=is_live,
            )
            
        except Exception as e:
            self.logger.debug("Failed to parse fixture", error=str(e))
            return None
    
    def _parse_odds(self, data: dict) -> Optional[SharpOdds]:
        """Parse odds data into SharpOdds."""
        try:
            periods = data.get("periods", [])
            if not periods:
                return None
            
            # Get full game odds (period 0)
            game_period = None
            for period in periods:
                if period.get("number") == 0:
                    game_period = period
                    break
            
            if not game_period:
                game_period = periods[0]
            
            # Parse moneyline
            ml = game_period.get("moneyline", {})
            if not ml:
                return None
            
            outcomes = []
            
            if ml.get("home"):
                outcomes.append(Outcome.from_american("Home", ml["home"]))
            if ml.get("away"):
                outcomes.append(Outcome.from_american("Away", ml["away"]))
            if ml.get("draw"):
                outcomes.append(Outcome.from_american("Draw", ml["draw"]))
            
            if not outcomes:
                return None
            
            sharp_odds = SharpOdds(
                bookmaker="pinnacle",
                market_type="h2h",
                outcomes=outcomes,
                last_update_ms=int(time.time() * 1000),
            )
            
            sharp_odds.calculate_vig_and_fair_odds()
            
            return sharp_odds
            
        except Exception as e:
            self.logger.debug("Failed to parse odds", error=str(e))
            return None
    
    def get_metrics(self) -> dict:
        """Get feed metrics."""
        return {
            "name": "pinnacle",
            "connected": self._http_client is not None,
            "events_cached": len(self._events),
            "leagues_cached": len(self._leagues),
        }

