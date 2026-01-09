"""
Polymarket Sports Market Discovery.

Finds and matches Polymarket sports markets to sharp book events.

Challenges:
- PM markets don't follow predictable naming like crypto 15-min markets
- Need to fuzzy match "Chiefs vs Ravens" to PM's "Kansas City to win AFC Championship"
- Sports markets can be structured differently (moneyline, spread, prop bets)

Strategy:
1. Fetch active sports events from Polymarket
2. Parse market questions to extract teams/leagues
3. Match to sharp book events using fuzzy matching
4. Track matched markets for signal detection
"""

import asyncio
import ssl
import time
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

import certifi
import httpx
import orjson
import structlog

from src.sports.models.schemas import Sport, SportEvent

logger = structlog.get_logger()


@dataclass
class PolymarketSportsMarket:
    """A discovered Polymarket sports market."""
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    
    # Token IDs for YES/NO
    yes_token_id: str
    no_token_id: str
    
    # Parsed info
    sport: Optional[Sport] = None
    teams: list[str] = field(default_factory=list)
    league: Optional[str] = None
    event_date: Optional[datetime] = None
    
    # Market metadata
    volume: float = 0.0
    liquidity: float = 0.0
    yes_price: float = 0.0
    no_price: float = 0.0
    
    # Matching
    matched_event_id: Optional[str] = None
    match_confidence: float = 0.0


@dataclass
class MatchedMarket:
    """A PM market matched to a sharp book event."""
    pm_market: PolymarketSportsMarket
    sharp_event: SportEvent
    match_score: float  # 0-1 confidence in match
    side: str  # "home", "away", "over", "under"


class SportsMarketDiscovery:
    """
    Discovers and matches Polymarket sports markets.
    
    Unlike crypto markets with predictable slugs, sports markets
    require parsing and fuzzy matching to connect PM markets
    to sharp book events.
    """
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    # Sport detection keywords
    SPORT_KEYWORDS = {
        Sport.NFL: [
            "nfl", "super bowl", "afc", "nfc", "touchdown",
            "chiefs", "ravens", "eagles", "49ers", "lions", "bills",
            "cowboys", "packers", "dolphins", "jets", "patriots",
            "broncos", "chargers", "raiders", "steelers", "bengals",
            "browns", "texans", "colts", "jaguars", "titans",
            "commanders", "giants", "saints", "falcons", "panthers",
            "buccaneers", "vikings", "bears", "cardinals", "rams", "seahawks",
        ],
        Sport.NBA: [
            "nba", "basketball", "lakers", "celtics", "warriors",
            "bucks", "nuggets", "suns", "heat", "76ers", "nets",
            "clippers", "mavericks", "grizzlies", "cavaliers", "kings",
            "pelicans", "hawks", "bulls", "knicks", "raptors", "magic",
            "pacers", "wizards", "hornets", "pistons", "rockets", "thunder",
            "timberwolves", "blazers", "jazz", "spurs",
        ],
        Sport.EPL: [
            "premier league", "epl", "manchester united", "manchester city",
            "liverpool", "chelsea", "arsenal", "tottenham", "spurs",
            "newcastle", "aston villa", "brighton", "west ham", "everton",
            "crystal palace", "wolves", "leicester", "fulham", "bournemouth",
            "brentford", "nottingham forest", "luton", "burnley", "sheffield",
        ],
        Sport.MLB: [
            "mlb", "baseball", "yankees", "dodgers", "braves", "astros",
            "phillies", "padres", "mariners", "mets", "cardinals", "cubs",
            "brewers", "guardians", "twins", "orioles", "rays", "blue jays",
            "red sox", "rangers", "angels", "white sox", "royals", "tigers",
            "giants", "diamondbacks", "rockies", "reds", "pirates", "nationals", "marlins",
        ],
        Sport.NHL: [
            "nhl", "hockey", "stanley cup", "bruins", "panthers", "avalanche",
            "rangers", "hurricanes", "devils", "maple leafs", "oilers",
            "golden knights", "jets", "stars", "lightning", "kings",
            "canucks", "kraken", "senators", "flames", "wild", "blues",
            "penguins", "capitals", "islanders", "flyers", "red wings",
            "predators", "blackhawks", "coyotes", "ducks", "sharks", "sabres", "blue jackets",
        ],
        Sport.UFC: [
            "ufc", "mma", "fight", "bout", "knockout", "ko",
        ],
    }
    
    # Team name normalization (handle abbreviations and common variants)
    TEAM_ALIASES = {
        # NFL
        "kc": "chiefs",
        "kansas city": "chiefs",
        "bal": "ravens",
        "baltimore": "ravens",
        "sf": "49ers",
        "san francisco": "49ers",
        "philly": "eagles",
        "philadelphia": "eagles",
        "det": "lions",
        "detroit": "lions",
        "buf": "bills",
        "buffalo": "bills",
        "gb": "packers",
        "green bay": "packers",
        "dal": "cowboys",
        "dallas": "cowboys",
        # NBA
        "la lakers": "lakers",
        "los angeles lakers": "lakers",
        "la clippers": "clippers",
        "los angeles clippers": "clippers",
        "gs": "warriors",
        "golden state": "warriors",
        "bos": "celtics",
        "boston": "celtics",
        # EPL
        "man utd": "manchester united",
        "man u": "manchester united",
        "man city": "manchester city",
        "mcfc": "manchester city",
        "mufc": "manchester united",
        "lfc": "liverpool",
        "cfc": "chelsea",
        "afc": "arsenal",
        "thfc": "tottenham",
    }
    
    def __init__(self):
        self.logger = logger.bind(component="sports_discovery")
        self._markets: dict[str, PolymarketSportsMarket] = {}  # condition_id -> market
        self._matched: dict[str, MatchedMarket] = {}  # event_id -> matched
        self._last_fetch_ms: int = 0
    
    async def discover_sports_markets(
        self,
        sports: Optional[list[Sport]] = None,
    ) -> list[PolymarketSportsMarket]:
        """
        Discover active sports markets on Polymarket.
        
        Args:
            sports: Optional list of sports to filter for
        
        Returns:
            List of discovered sports markets
        """
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        async with httpx.AsyncClient(verify=ssl_context, timeout=15.0) as client:
            # Search for sports-related events
            markets = await self._fetch_sports_events(client)
            
            # Parse and categorize
            for market in markets:
                self._parse_market(market)
                self._markets[market.condition_id] = market
            
            # Filter by sports if specified
            if sports:
                sport_set = set(sports)
                markets = [m for m in markets if m.sport in sport_set]
            
            self._last_fetch_ms = int(time.time() * 1000)
            
            self.logger.info(
                "Discovered sports markets",
                total=len(self._markets),
                filtered=len(markets),
                sports=[s.name for s in sports] if sports else "all",
            )
            
            return markets
    
    async def _fetch_sports_events(
        self,
        client: httpx.AsyncClient,
    ) -> list[PolymarketSportsMarket]:
        """Fetch sports-related events from Polymarket."""
        markets = []
        
        # Search for sports keywords
        search_terms = [
            "NFL",
            "Super Bowl",
            "NBA",
            "Premier League",
            "Champions League",
            "UFC",
            "NHL",
            "MLB",
            "World Series",
        ]
        
        seen_ids = set()
        
        for term in search_terms:
            try:
                response = await client.get(
                    f"{self.GAMMA_API_URL}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 50,
                        "title": term,
                    },
                )
                
                if response.status_code == 200:
                    events = response.json()
                    
                    for event in events:
                        # Process each market in the event
                        for market_data in event.get("markets", []):
                            condition_id = market_data.get("conditionId", "")
                            if condition_id in seen_ids:
                                continue
                            seen_ids.add(condition_id)
                            
                            market = self._parse_market_data(market_data, event)
                            if market:
                                markets.append(market)
                
            except Exception as e:
                self.logger.debug("Search error", term=term, error=str(e))
        
        # Also try the events endpoint with sports tags
        # Polymarket uses specific tag IDs for sports
        sports_tag_ids = [
            10,   # Sports
            50,   # NFL
            51,   # NBA
            52,   # MLB
            # Add more as discovered
        ]
        
        for tag_id in sports_tag_ids:
            try:
                response = await client.get(
                    f"{self.GAMMA_API_URL}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "tag_id": tag_id,
                        "limit": 100,
                    },
                )
                
                if response.status_code == 200:
                    events = response.json()
                    
                    for event in events:
                        for market_data in event.get("markets", []):
                            condition_id = market_data.get("conditionId", "")
                            if condition_id in seen_ids:
                                continue
                            seen_ids.add(condition_id)
                            
                            market = self._parse_market_data(market_data, event)
                            if market:
                                markets.append(market)
                
            except Exception as e:
                self.logger.debug("Tag search error", tag_id=tag_id, error=str(e))
        
        return markets
    
    def _parse_market_data(
        self,
        market_data: dict,
        event_data: dict,
    ) -> Optional[PolymarketSportsMarket]:
        """Parse market data into PolymarketSportsMarket."""
        try:
            condition_id = market_data.get("conditionId", "")
            question = market_data.get("question", "") or event_data.get("title", "")
            
            if not condition_id or not question:
                return None
            
            # Parse tokens
            tokens_raw = market_data.get("clobTokenIds", [])
            if isinstance(tokens_raw, str):
                tokens = orjson.loads(tokens_raw)
            else:
                tokens = tokens_raw
            
            if len(tokens) < 2:
                return None
            
            # Parse prices
            outcome_prices = market_data.get("outcomePrices", "")
            yes_price = 0.5
            no_price = 0.5
            if outcome_prices:
                try:
                    if isinstance(outcome_prices, str):
                        prices = orjson.loads(outcome_prices)
                    else:
                        prices = outcome_prices
                    if len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except:
                    pass
            
            return PolymarketSportsMarket(
                condition_id=condition_id,
                question=question,
                description=market_data.get("description", "") or event_data.get("description", ""),
                end_date_iso=event_data.get("endDate", "") or market_data.get("endDate", ""),
                yes_token_id=tokens[0],
                no_token_id=tokens[1],
                volume=float(market_data.get("volume", 0) or 0),
                liquidity=float(market_data.get("liquidity", 0) or 0),
                yes_price=yes_price,
                no_price=no_price,
            )
            
        except Exception as e:
            self.logger.debug("Failed to parse market", error=str(e))
            return None
    
    def _parse_market(self, market: PolymarketSportsMarket) -> None:
        """Parse market question to extract sport, teams, etc."""
        question_lower = market.question.lower()
        description_lower = market.description.lower()
        combined = f"{question_lower} {description_lower}"
        
        # Detect sport
        for sport, keywords in self.SPORT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in combined:
                    market.sport = sport
                    break
            if market.sport:
                break
        
        # Extract team names
        teams = []
        for sport, keywords in self.SPORT_KEYWORDS.items():
            for keyword in keywords:
                # Skip generic keywords
                if keyword in ["nfl", "nba", "mlb", "nhl", "epl", "ufc", 
                              "super bowl", "stanley cup", "premier league"]:
                    continue
                if keyword in combined:
                    teams.append(keyword)
        
        market.teams = list(set(teams))[:2]  # Max 2 teams
        
        # Try to parse event date from question
        # Common patterns: "Jan 28", "January 28, 2026", "1/28"
        date_patterns = [
            r"(\w+ \d{1,2},? \d{4})",  # January 28, 2026
            r"(\w+ \d{1,2})",          # Jan 28
            r"(\d{1,2}/\d{1,2})",      # 1/28
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, market.question)
            if match:
                try:
                    date_str = match.group(1)
                    # Try parsing (simplified)
                    # In production, use dateutil.parser
                    break
                except:
                    pass
    
    def match_to_sharp_event(
        self,
        pm_market: PolymarketSportsMarket,
        sharp_events: list[SportEvent],
    ) -> Optional[MatchedMarket]:
        """
        Match a PM market to a sharp book event.
        
        Uses fuzzy matching on team names and event timing.
        """
        if not pm_market.teams or not sharp_events:
            return None
        
        best_match: Optional[MatchedMarket] = None
        best_score = 0.0
        
        for event in sharp_events:
            # Calculate match score
            score = self._calculate_match_score(pm_market, event)
            
            if score > best_score and score > 0.5:  # Minimum 50% confidence
                best_score = score
                
                # Determine side
                side = self._determine_side(pm_market, event)
                
                best_match = MatchedMarket(
                    pm_market=pm_market,
                    sharp_event=event,
                    match_score=score,
                    side=side,
                )
        
        if best_match:
            pm_market.matched_event_id = best_match.sharp_event.event_id
            pm_market.match_confidence = best_match.match_score
            self._matched[best_match.sharp_event.event_id] = best_match
            
            self.logger.info(
                "Matched PM market to sharp event",
                pm_question=pm_market.question[:50],
                sharp_event=best_match.sharp_event.get_display_name(),
                score=f"{best_match.match_score:.1%}",
                side=best_match.side,
            )
        
        return best_match
    
    def _calculate_match_score(
        self,
        pm_market: PolymarketSportsMarket,
        event: SportEvent,
    ) -> float:
        """Calculate how well PM market matches sharp event."""
        score = 0.0
        weights = {"teams": 0.6, "sport": 0.2, "timing": 0.2}
        
        # Team matching (60%)
        pm_teams_normalized = [self._normalize_team(t) for t in pm_market.teams]
        event_teams = [
            self._normalize_team(event.home_team),
            self._normalize_team(event.away_team),
        ]
        
        team_matches = 0
        for pm_team in pm_teams_normalized:
            for event_team in event_teams:
                similarity = SequenceMatcher(None, pm_team, event_team).ratio()
                if similarity > 0.7:
                    team_matches += 1
                    break
        
        if pm_teams_normalized:
            score += weights["teams"] * (team_matches / len(pm_teams_normalized))
        
        # Sport matching (20%)
        if pm_market.sport and pm_market.sport == event.sport:
            score += weights["sport"]
        
        # Timing matching (20%)
        # If PM end date is within 1 day of event start, good match
        if pm_market.event_date and event.commence_time:
            time_diff = abs((pm_market.event_date - event.commence_time).total_seconds())
            if time_diff < 86400:  # 24 hours
                score += weights["timing"] * (1 - time_diff / 86400)
        else:
            # No timing info, give partial credit
            score += weights["timing"] * 0.5
        
        return score
    
    def _normalize_team(self, name: str) -> str:
        """Normalize team name for matching."""
        name_lower = name.lower().strip()
        
        # Apply aliases
        for alias, canonical in self.TEAM_ALIASES.items():
            if alias in name_lower:
                return canonical
        
        # Remove common suffixes
        for suffix in ["fc", "city", "united", "athletic"]:
            name_lower = name_lower.replace(suffix, "").strip()
        
        return name_lower
    
    def _determine_side(
        self,
        pm_market: PolymarketSportsMarket,
        event: SportEvent,
    ) -> str:
        """Determine which side of the event the PM market represents."""
        question_lower = pm_market.question.lower()
        home_normalized = self._normalize_team(event.home_team)
        away_normalized = self._normalize_team(event.away_team)
        
        # Check for team mentions
        if home_normalized in question_lower:
            return "home"
        elif away_normalized in question_lower:
            return "away"
        
        # Check for "to win" patterns
        for team in pm_market.teams:
            team_norm = self._normalize_team(team)
            if team_norm == home_normalized:
                return "home"
            elif team_norm == away_normalized:
                return "away"
        
        # Default to home if unclear
        return "home"
    
    def get_matched_market(self, event_id: str) -> Optional[MatchedMarket]:
        """Get matched market for an event."""
        return self._matched.get(event_id)
    
    def get_all_matched(self) -> list[MatchedMarket]:
        """Get all matched markets."""
        return list(self._matched.values())
    
    def get_metrics(self) -> dict:
        """Get discovery metrics."""
        return {
            "total_markets": len(self._markets),
            "matched_markets": len(self._matched),
            "last_fetch_ms": self._last_fetch_ms,
            "sports_breakdown": self._get_sports_breakdown(),
        }
    
    def _get_sports_breakdown(self) -> dict:
        """Get breakdown of markets by sport."""
        breakdown = {}
        for market in self._markets.values():
            sport_name = market.sport.name if market.sport else "UNKNOWN"
            breakdown[sport_name] = breakdown.get(sport_name, 0) + 1
        return breakdown

