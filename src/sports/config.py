"""
Sports Bot Configuration.

Separate from crypto bot settings to allow independent tuning.
Can be merged with main config/settings.py later.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import os


class SportsOperatingMode(Enum):
    """Operating modes for sports bot."""
    SHADOW = "shadow"           # Log everything, no trades
    ALERT = "alert"             # Send Discord alerts + virtual trades
    LIVE = "live"               # Real trades (future)


@dataclass
class OddsAPISettings:
    """The Odds API configuration."""
    api_key: str = ""
    base_url: str = "https://api.the-odds-api.com/v4"
    
    # Rate limiting
    requests_per_minute: int = 10
    
    # Which bookmakers to fetch (prioritize sharp books)
    bookmakers: list[str] = field(default_factory=lambda: [
        "pinnacle",        # Sharpest book
        "betfair_ex_eu",   # Betfair Exchange
        "betfair",
        "circa",           # Sharp US book
        "draftkings",
        "fanduel",
    ])
    
    # Markets to fetch
    markets: list[str] = field(default_factory=lambda: [
        "h2h",      # Moneyline
        "spreads",  # Point spread
        "totals",   # Over/Under
    ])
    
    # Regions
    regions: list[str] = field(default_factory=lambda: ["us", "eu", "uk"])
    
    # Sports to monitor
    sports: list[str] = field(default_factory=lambda: [
        "americanfootball_nfl",
        "basketball_nba",
        "soccer_epl",
        "icehockey_nhl",
    ])
    
    # Poll interval (seconds)
    poll_interval: float = 30.0


@dataclass
class PinnacleSettings:
    """Pinnacle API configuration (future)."""
    username: str = ""
    password: str = ""
    base_url: str = "https://api.pinnacle.com"
    enabled: bool = False


@dataclass
class BetfairSettings:
    """Betfair Exchange API configuration (future)."""
    app_key: str = ""
    session_token: str = ""
    base_url: str = "https://api.betfair.com/exchange"
    enabled: bool = False


@dataclass
class SignalSettings:
    """Sports signal detection settings."""
    
    # Divergence thresholds (MUCH lower than crypto due to 0% fees!)
    min_divergence_pct: float = 0.005   # 0.5% minimum (vs 8% for crypto)
    high_divergence_pct: float = 0.02   # 2% = high confidence
    max_divergence_pct: float = 0.15    # 15% = probably stale/wrong data
    
    # PM staleness thresholds
    min_pm_staleness_seconds: float = 5.0    # PM must be slightly stale
    max_pm_staleness_seconds: float = 300.0  # 5 min max
    
    # Timing thresholds
    min_time_to_event_seconds: float = 300.0    # 5 min (avoid court-siding)
    max_time_to_event_seconds: float = 86400.0  # 24 hours
    
    # Quality thresholds
    min_pm_liquidity: float = 100.0     # $100 minimum
    max_pm_spread: float = 0.05         # 5% max spread
    min_sharp_agreement: float = 0.90   # 90% book agreement
    min_sharp_books: int = 1            # At least 1 sharp book
    max_sharp_vig: float = 0.04         # 4% max vig (Pinnacle is ~2%)
    
    # Signal cooldown
    signal_cooldown_ms: int = 60_000    # 1 min between signals per event


@dataclass
class VirtualTradingSettings:
    """Virtual trading settings."""
    enabled: bool = True
    position_size_usd: float = 20.0     # $20 per trade
    max_concurrent_positions: int = 5
    take_profit_pct: float = 0.10       # 10% take profit
    stop_loss_pct: float = -0.05        # 5% stop loss
    time_limit_seconds: float = 300.0   # 5 min position time limit


@dataclass
class AlertSettings:
    """Alert configuration."""
    discord_webhook_url: str = ""
    min_confidence_for_alert: float = 0.60  # 60% confidence minimum
    
    # Notification settings
    notify_on_signal: bool = True
    notify_on_position_open: bool = True
    notify_on_position_close: bool = True
    status_update_interval_seconds: float = 300.0  # 5 min


@dataclass
class SportsSettings:
    """
    Main sports bot settings.
    
    Load from environment variables with SPORTS_ prefix.
    """
    # Operating mode
    mode: SportsOperatingMode = SportsOperatingMode.SHADOW
    
    # Sub-settings
    odds_api: OddsAPISettings = field(default_factory=OddsAPISettings)
    pinnacle: PinnacleSettings = field(default_factory=PinnacleSettings)
    betfair: BetfairSettings = field(default_factory=BetfairSettings)
    signals: SignalSettings = field(default_factory=SignalSettings)
    virtual_trading: VirtualTradingSettings = field(default_factory=VirtualTradingSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    
    # Logging
    log_level: str = "INFO"
    
    @classmethod
    def from_env(cls) -> "SportsSettings":
        """Load settings from environment variables."""
        settings = cls()
        
        # Mode
        mode_str = os.getenv("SPORTS_MODE", "shadow").lower()
        try:
            settings.mode = SportsOperatingMode(mode_str)
        except ValueError:
            pass
        
        # Odds API
        settings.odds_api.api_key = os.getenv("ODDS_API_KEY", "")
        poll_interval = os.getenv("SPORTS_POLL_INTERVAL")
        if poll_interval:
            settings.odds_api.poll_interval = float(poll_interval)
        
        # Sports to monitor
        sports_str = os.getenv("SPORTS_MONITOR")
        if sports_str:
            settings.odds_api.sports = [s.strip() for s in sports_str.split(",")]
        
        # Signal thresholds
        min_div = os.getenv("SPORTS_MIN_DIVERGENCE")
        if min_div:
            settings.signals.min_divergence_pct = float(min_div)
        
        min_liq = os.getenv("SPORTS_MIN_LIQUIDITY")
        if min_liq:
            settings.signals.min_pm_liquidity = float(min_liq)
        
        # Alerts
        settings.alerts.discord_webhook_url = os.getenv(
            "SPORTS_DISCORD_WEBHOOK",
            os.getenv("ALERTS__DISCORD_WEBHOOK_URL", "")  # Fall back to main bot's webhook
        )
        
        min_conf = os.getenv("SPORTS_MIN_CONFIDENCE")
        if min_conf:
            settings.alerts.min_confidence_for_alert = float(min_conf)
        
        # Virtual trading
        pos_size = os.getenv("SPORTS_POSITION_SIZE")
        if pos_size:
            settings.virtual_trading.position_size_usd = float(pos_size)
        
        # Logging
        settings.log_level = os.getenv("SPORTS_LOG_LEVEL", "INFO")
        
        return settings


# Singleton instance
_settings: Optional[SportsSettings] = None


def get_sports_settings() -> SportsSettings:
    """Get sports settings singleton."""
    global _settings
    if _settings is None:
        _settings = SportsSettings.from_env()
    return _settings


def reload_settings() -> SportsSettings:
    """Reload settings from environment."""
    global _settings
    _settings = SportsSettings.from_env()
    return _settings

