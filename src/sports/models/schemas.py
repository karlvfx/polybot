"""
Sports betting data models and schemas.

Defines the core data structures for:
- Sports events and leagues
- Odds formats and conversions
- Sharp book data (Pinnacle, Betfair)
- Signal candidates for arbitrage
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import math


class Sport(Enum):
    """Supported sports."""
    NFL = "americanfootball_nfl"
    NBA = "basketball_nba"
    MLB = "baseball_mlb"
    NHL = "icehockey_nhl"
    EPL = "soccer_epl"
    LA_LIGA = "soccer_spain_la_liga"
    BUNDESLIGA = "soccer_germany_bundesliga"
    SERIE_A = "soccer_italy_serie_a"
    LIGUE_1 = "soccer_france_ligue_one"
    CHAMPIONS_LEAGUE = "soccer_uefa_champs_league"
    MLS = "soccer_usa_mls"
    UFC = "mma_mixed_martial_arts"
    BOXING = "boxing_boxing"
    TENNIS_ATP = "tennis_atp_french_open"  # Example, varies by tournament
    GOLF_PGA = "golf_pga_championship"
    
    @classmethod
    def from_string(cls, value: str) -> Optional["Sport"]:
        """Convert string to Sport enum."""
        value_lower = value.lower()
        for sport in cls:
            if sport.value.lower() == value_lower or sport.name.lower() == value_lower:
                return sport
        return None


@dataclass
class League:
    """A sports league/competition."""
    sport: Sport
    name: str
    key: str  # API key (e.g., "americanfootball_nfl")
    is_active: bool = True
    
    # Polymarket-specific
    pm_keywords: list[str] = field(default_factory=list)  # Keywords to search for markets
    pm_tag_id: Optional[int] = None  # Polymarket tag ID if available


class OddsFormat(Enum):
    """Odds format types."""
    AMERICAN = "american"      # +150, -200
    DECIMAL = "decimal"        # 2.50, 1.50
    FRACTIONAL = "fractional"  # 3/2, 1/2
    PROBABILITY = "probability"  # 0.40, 0.67


@dataclass
class Outcome:
    """A single betting outcome (e.g., Team A wins)."""
    name: str           # "Kansas City Chiefs", "Over 45.5"
    odds_american: float  # +150 or -200
    odds_decimal: float   # 2.50 or 1.50
    implied_prob: float   # 0.40 or 0.67 (raw, includes vig)
    fair_prob: float      # 0.38 or 0.65 (vig-removed)
    
    # Metadata
    is_home: bool = False
    is_favorite: bool = False
    line: Optional[float] = None  # For spreads/totals: -3.5, 45.5
    
    @staticmethod
    def american_to_decimal(american: float) -> float:
        """Convert American odds to Decimal."""
        if american > 0:
            return (american / 100) + 1
        else:
            return (100 / abs(american)) + 1
    
    @staticmethod
    def decimal_to_probability(decimal: float) -> float:
        """Convert Decimal odds to implied probability."""
        if decimal <= 0:
            return 0.0
        return 1 / decimal
    
    @staticmethod
    def american_to_probability(american: float) -> float:
        """Convert American odds to implied probability."""
        if american > 0:
            return 100 / (american + 100)
        else:
            return abs(american) / (abs(american) + 100)
    
    @classmethod
    def from_american(cls, name: str, american_odds: float, **kwargs) -> "Outcome":
        """Create Outcome from American odds."""
        decimal = cls.american_to_decimal(american_odds)
        implied = cls.american_to_probability(american_odds)
        return cls(
            name=name,
            odds_american=american_odds,
            odds_decimal=decimal,
            implied_prob=implied,
            fair_prob=implied,  # Will be adjusted when vig is calculated
            **kwargs
        )
    
    @classmethod
    def from_decimal(cls, name: str, decimal_odds: float, **kwargs) -> "Outcome":
        """Create Outcome from Decimal odds."""
        implied = cls.decimal_to_probability(decimal_odds)
        # Convert back to American for consistency
        if decimal_odds >= 2.0:
            american = (decimal_odds - 1) * 100
        else:
            american = -100 / (decimal_odds - 1)
        return cls(
            name=name,
            odds_american=american,
            odds_decimal=decimal_odds,
            implied_prob=implied,
            fair_prob=implied,
            **kwargs
        )


@dataclass
class SharpOdds:
    """
    Odds from a sharp sportsbook for a specific market.
    
    Sharp books (Pinnacle, Betfair, Circa) have the most accurate
    odds because they accept high-volume professional bettors.
    """
    bookmaker: str        # "pinnacle", "betfair", "circa"
    market_type: str      # "h2h" (moneyline), "spreads", "totals"
    outcomes: list[Outcome]
    
    # Timing
    last_update_ms: int
    update_age_seconds: float = 0.0
    
    # Quality metrics
    vig: float = 0.0        # House edge (should be 2-4% for Pinnacle)
    is_stale: bool = False  # True if >60s since update
    
    def calculate_vig_and_fair_odds(self) -> None:
        """Calculate vig and adjust outcomes to fair probabilities."""
        if len(self.outcomes) < 2:
            return
        
        # Sum of implied probabilities = 1 + vig
        total_prob = sum(o.implied_prob for o in self.outcomes)
        self.vig = total_prob - 1.0
        
        # Adjust each outcome to fair probability (remove vig)
        for outcome in self.outcomes:
            outcome.fair_prob = outcome.implied_prob / total_prob
    
    @property
    def spread(self) -> float:
        """Get bid-ask spread as proxy for market confidence."""
        if len(self.outcomes) < 2:
            return 0.0
        probs = sorted([o.fair_prob for o in self.outcomes])
        return 1.0 - sum(probs)  # Rough measure


@dataclass 
class SportEvent:
    """
    A single sports event (game/match).
    
    Contains odds from multiple bookmakers for comparison.
    """
    event_id: str
    sport: Sport
    league: str
    
    # Teams/participants
    home_team: str
    away_team: str
    
    # Timing
    commence_time: datetime
    commence_time_ms: int
    is_live: bool = False
    
    # Odds from different sources
    sharp_odds: dict[str, SharpOdds] = field(default_factory=dict)  # bookmaker -> odds
    
    # Polymarket matching
    pm_market_id: Optional[str] = None
    pm_condition_id: Optional[str] = None
    pm_question: Optional[str] = None
    
    @property
    def time_until_start_seconds(self) -> float:
        """Get seconds until event starts."""
        now = datetime.utcnow()
        if self.commence_time.tzinfo:
            now = now.replace(tzinfo=self.commence_time.tzinfo)
        delta = (self.commence_time - now).total_seconds()
        return max(0, delta)
    
    @property
    def best_sharp_odds(self) -> Optional[SharpOdds]:
        """Get odds from the sharpest available book."""
        # Priority: Pinnacle > Betfair > Circa > others
        priority = ["pinnacle", "betfair", "betfair_ex_eu", "circa"]
        for book in priority:
            if book in self.sharp_odds:
                return self.sharp_odds[book]
        # Return any available
        if self.sharp_odds:
            return next(iter(self.sharp_odds.values()))
        return None
    
    def get_display_name(self) -> str:
        """Get human-readable event name."""
        return f"{self.away_team} @ {self.home_team}"


class DivergenceType(Enum):
    """Type of divergence detected."""
    SHARP_HIGHER = "sharp_higher"  # Sharp book implies higher prob than PM
    SHARP_LOWER = "sharp_lower"    # Sharp book implies lower prob than PM
    NONE = "none"


@dataclass
class SharpBookData:
    """
    Aggregated sharp book data for an event.
    
    This is the "truth" that we compare against Polymarket.
    Similar to how consensus price from exchanges is the "truth" for crypto.
    """
    event: SportEvent
    primary_book: str           # Which sharp book is primary
    
    # The key numbers
    fair_home_prob: float       # Fair probability of home win (vig-removed)
    fair_away_prob: float       # Fair probability of away win
    fair_draw_prob: float = 0.0  # For soccer
    
    # Spread/total if applicable
    spread_line: Optional[float] = None
    total_line: Optional[float] = None
    
    # Quality metrics
    vig: float = 0.0
    last_update_ms: int = 0
    is_stale: bool = False
    
    # Agreement across books (like exchange agreement in crypto)
    books_count: int = 1
    agreement_score: float = 1.0  # How much books agree (0-1)
    
    @property
    def update_age_seconds(self) -> float:
        """Seconds since last update."""
        import time
        now_ms = int(time.time() * 1000)
        return (now_ms - self.last_update_ms) / 1000.0


@dataclass
class SportSignalCandidate:
    """
    A potential sports arbitrage signal.
    
    Similar to SignalCandidate in crypto bot but for sports.
    """
    signal_id: str
    timestamp_ms: int
    
    # Event info
    event: SportEvent
    sport: Sport
    
    # The divergence
    divergence_type: DivergenceType
    side: str                    # "home", "away", "over", "under"
    
    # Sharp vs PM comparison
    sharp_fair_prob: float       # What sharp books say
    pm_implied_prob: float       # What Polymarket shows
    divergence_pct: float        # Absolute difference
    
    # PM market details
    pm_market_id: str
    pm_yes_price: float
    pm_no_price: float
    pm_liquidity: float
    pm_spread: float
    
    # Timing
    pm_staleness_seconds: float  # How long PM odds have been static
    time_to_event_seconds: float
    
    # Scoring (filled in by scorer)
    confidence: float = 0.0
    edge_after_slippage: float = 0.0
    
    # Validation
    is_valid: bool = False
    rejection_reason: Optional[str] = None
    
    @property
    def direction(self) -> str:
        """Get trade direction (YES or NO)."""
        if self.divergence_type == DivergenceType.SHARP_HIGHER:
            return "YES"  # Sharp says more likely than PM shows
        elif self.divergence_type == DivergenceType.SHARP_LOWER:
            return "NO"   # Sharp says less likely than PM shows
        return "NONE"
    
    def to_log(self) -> dict:
        """Convert to loggable dict."""
        return {
            "signal_id": self.signal_id,
            "timestamp_ms": self.timestamp_ms,
            "sport": self.sport.name,
            "event": self.event.get_display_name(),
            "side": self.side,
            "direction": self.direction,
            "sharp_prob": self.sharp_fair_prob,
            "pm_prob": self.pm_implied_prob,
            "divergence": self.divergence_pct,
            "pm_staleness": self.pm_staleness_seconds,
            "confidence": self.confidence,
            "is_valid": self.is_valid,
        }


# =============================================================================
# Utility Functions
# =============================================================================

def calculate_kelly_fraction(
    win_prob: float,
    odds_decimal: float,
    fraction: float = 0.25,  # Quarter Kelly for safety
) -> float:
    """
    Calculate Kelly Criterion bet size.
    
    Args:
        win_prob: True probability of winning
        odds_decimal: Decimal odds offered
        fraction: Kelly fraction (0.25 = quarter Kelly)
    
    Returns:
        Recommended bet as fraction of bankroll
    """
    # Kelly: f = (bp - q) / b
    # where b = decimal odds - 1, p = win prob, q = 1 - p
    b = odds_decimal - 1
    p = win_prob
    q = 1 - p
    
    kelly = (b * p - q) / b if b > 0 else 0
    
    # Apply fractional Kelly and clamp
    return max(0, min(fraction, kelly * fraction))


def calculate_expected_value(
    win_prob: float,
    win_payout: float,
    stake: float = 1.0,
) -> float:
    """
    Calculate expected value of a bet.
    
    Args:
        win_prob: Probability of winning (0-1)
        win_payout: Amount won if successful (not including stake)
        stake: Amount wagered
    
    Returns:
        Expected value (positive = +EV)
    """
    ev = (win_prob * win_payout) - ((1 - win_prob) * stake)
    return ev


def remove_vig_power_method(probs: list[float]) -> list[float]:
    """
    Remove vig using the power method (more accurate than simple division).
    
    This method assumes the vig is distributed proportionally to the
    square root of the odds, which is closer to how sharp books operate.
    """
    if not probs or sum(probs) <= 1:
        return probs
    
    total = sum(probs)
    
    # Simple method: divide each by total
    # Power method: more accurate for 2-way markets
    if len(probs) == 2:
        # Use multiplicative method for 2-way
        # Find k such that p1^k + p2^k = 1
        # Approximation: k ≈ log(0.5) / log(avg prob)
        avg = sum(probs) / len(probs)
        if 0 < avg < 1:
            k = math.log(0.5) / math.log(avg)
            fair = [p ** k for p in probs]
            fair_total = sum(fair)
            return [f / fair_total for f in fair]
    
    # Fallback to simple division
    return [p / total for p in probs]

