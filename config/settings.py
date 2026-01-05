"""
Configuration settings for the Polymarket Oracle-Lag Trading Bot.
Uses pydantic-settings for validation and environment variable loading.
"""

from enum import Enum
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OperatingMode(str, Enum):
    """Bot operating modes."""
    SHADOW = "shadow"
    ALERT = "alert"
    NIGHT_AUTO = "night_auto"


class VolatilityRegime(str, Enum):
    """Market volatility regime classification."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ExchangeSettings(BaseSettings):
    """Settings for individual exchange connections."""
    
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    binance_symbol: str = "btcusdt"
    
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    coinbase_product_id: str = "BTC-USD"
    
    kraken_ws_url: str = "wss://ws.kraken.com"
    kraken_pair: str = "XBT/USD"


class ChainlinkSettings(BaseSettings):
    """Settings for Chainlink oracle monitoring."""
    
    # Polygon Mainnet BTC/USD Chainlink Feed
    btc_usd_feed_address: str = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
    polygon_rpc_url: str = Field(default="", description="Polygon RPC URL (Alchemy/Ankr)")
    polygon_ws_url: str = Field(default="", description="Polygon WebSocket URL")
    
    # Oracle timing thresholds (seconds)
    oracle_min_age_low_vol: int = 12
    oracle_min_age_normal_vol: int = 6
    oracle_max_age: int = 75
    oracle_optimal_age_low_vol: int = 40
    oracle_optimal_age_normal_vol: int = 30
    
    # Fast heartbeat detection
    fast_heartbeat_threshold: int = 35  # seconds


class PolymarketSettings(BaseSettings):
    """Settings for Polymarket connection."""
    
    api_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # Market identifiers (BTC 15-min up/down markets)
    btc_up_market_id: str = Field(default="", description="BTC 15-min UP market condition ID")
    btc_down_market_id: str = Field(default="", description="BTC 15-min DOWN market condition ID")


class SignalSettings(BaseSettings):
    """Signal detection thresholds."""
    
    # Spot movement thresholds
    min_spot_move_pct: float = 0.007  # 0.7% minimum
    atr_multiplier: float = 1.5  # move_threshold = max(0.7%, 1.5 * ATR)
    
    # Escape clause thresholds
    escape_clause_min_move: float = 0.008  # 0.8%
    escape_clause_min_oracle_age: int = 15
    escape_clause_min_imbalance: float = 0.20
    escape_clause_min_liquidity: float = 75.0
    escape_clause_min_volume_surge: float = 2.5
    escape_clause_confidence_penalty: float = 0.10
    
    # Volume and momentum
    volume_surge_threshold: float = 2.0  # 2x average
    spike_concentration_threshold: float = 0.60  # 60% of move in 10s
    
    # Volatility filter
    max_volatility_30s: float = 0.005  # 0.5%
    
    # Consensus
    consensus_price_tolerance: float = 0.0015  # 0.15%
    
    # Mispricing detection
    min_mispricing_pct: float = 0.03  # 3%
    
    # Liquidity
    min_liquidity_eur: float = 50.0
    liquidity_collapse_threshold: float = 0.60  # 60% of 30s ago


class ConfidenceWeights(BaseSettings):
    """Confidence scoring component weights."""
    
    oracle_age_weight: float = 0.35
    consensus_strength_weight: float = 0.25
    misalignment_weight: float = 0.15
    liquidity_weight: float = 0.10
    spread_anomaly_weight: float = 0.08
    volume_surge_weight: float = 0.04
    spike_concentration_weight: float = 0.03


class ExecutionSettings(BaseSettings):
    """Trade execution settings."""
    
    # Order settings
    max_order_wait_seconds: int = 8
    max_position_duration_seconds: int = 120
    take_profit_spread_threshold: float = 0.015  # 1.5%
    take_profit_pct: float = 0.08  # 8%
    time_based_exit_seconds: int = 90
    
    # Gas settings (Polygon)
    max_gas_price_gwei: int = 50
    gas_buffer_multiplier: float = 1.2
    max_priority_fee_gwei: int = 35
    max_fee_per_gas_gwei: int = 200
    
    # Slippage
    max_slippage_pct: float = 0.02  # 2%


class RiskSettings(BaseSettings):
    """Risk management settings."""
    
    # Capital allocation
    starting_capital_eur: float = 500.0
    max_position_pct: float = 0.005  # 0.5% of bankroll
    max_daily_exposure_pct: float = 0.05  # 5% of bankroll
    max_concurrent_positions: int = 1
    
    # Circuit breakers
    daily_loss_limit_eur: float = 40.0
    max_consecutive_failed_fills: int = 3
    max_daily_gas_spend_eur: float = 10.0
    
    # Night mode limits
    night_mode_max_position_eur: float = 20.0
    night_mode_max_trades: int = 2
    night_mode_max_loss_eur: float = 40.0
    night_mode_min_confidence: float = 0.85
    night_mode_start_hour: int = 2  # 02:00
    night_mode_end_hour: int = 6  # 06:00


class AlertSettings(BaseSettings):
    """Discord alerting settings."""
    
    discord_webhook_url: str = Field(default="", description="Discord webhook URL")
    alert_confidence_threshold: float = 0.70
    alert_cooldown_seconds: int = 30


class Settings(BaseSettings):
    """Main application settings."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore"
    )
    
    # Operating mode
    mode: OperatingMode = OperatingMode.SHADOW
    
    # Debug settings
    debug: bool = False
    log_level: str = "INFO"
    
    # Database
    database_url: str = "sqlite+aiosqlite:///./logs/trading.db"
    
    # Wallet settings
    wallet_address: str = Field(default="", description="Your Polygon wallet address")
    private_key: str = Field(default="", description="Your wallet private key (keep secure!)")
    
    # Sub-settings
    exchanges: ExchangeSettings = Field(default_factory=ExchangeSettings)
    chainlink: ChainlinkSettings = Field(default_factory=ChainlinkSettings)
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    confidence: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    
    # Feed health
    heartbeat_interval_seconds: float = 2.0
    feed_stale_threshold_seconds: float = 3.0
    
    # Performance targets
    target_win_rate: float = 0.65
    target_avg_profit_eur: float = 1.50
    min_signals_per_day: int = 5
    max_signals_per_day: int = 15
    max_e2e_latency_ms: int = 200


# Global settings instance
settings = Settings()

