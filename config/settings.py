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
    
    # Multi-asset symbols (asset -> exchange symbol mapping)
    symbols: dict = Field(default_factory=lambda: {
        "BTC": {"binance": "btcusdt", "coinbase": "BTC-USD", "kraken": "XBT/USD"},
        "ETH": {"binance": "ethusdt", "coinbase": "ETH-USD", "kraken": "ETH/USD"},
        "SOL": {"binance": "solusdt", "coinbase": "SOL-USD", "kraken": "SOL/USD"},
        "XRP": {"binance": "xrpusdt", "coinbase": "XRP-USD", "kraken": "XRP/USD"},
    })


class ChainlinkSettings(BaseSettings):
    """Settings for Chainlink oracle monitoring."""
    
    # Polygon Mainnet Chainlink Feed Addresses
    btc_usd_feed_address: str = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
    eth_usd_feed_address: str = "0xF9680D99D6C9589e2a93a78A04A279e509205945"
    sol_usd_feed_address: str = "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC"
    # XRP doesn't have a reliable Chainlink feed on Polygon - use spot price only
    xrp_usd_feed_address: str = ""
    
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
    """Signal detection thresholds with multi-layered validation."""
    
    # ==========================================================================
    # NEW: Divergence-based signal detection (primary strategy)
    # The edge: spot price moves but PM odds haven't caught up yet
    # ==========================================================================
    
    # Divergence thresholds
    min_divergence_pct: float = 0.08  # 8% probability divergence required
    min_pm_staleness_seconds: float = 3.0  # PM orderbook must be stale for 3+ seconds (lowered from 8)
    max_pm_staleness_seconds: float = 600.0  # 10 minutes max (PM markets can be slow)
    
    # Spot-implied probability scaling
    # Controls how sensitive the probability is to price moves
    # scale=100: 1% move → ~73% prob, 2% move → ~88% prob
    spot_implied_scale: float = 100.0  # Logistic curve scale factor
    
    # ==========================================================================
    # Spot movement thresholds (supporting filter)
    # ==========================================================================
    min_spot_move_pct: float = 0.003  # 0.3% absolute minimum (lowered from 0.7%)
    atr_multiplier: float = 1.5  # move_threshold = max(0.7%, 1.5 * ATR)
    
    # Escape clause thresholds (allows sub-threshold moves when strongly supported)
    escape_clause_min_move: float = 0.008  # 0.8% minimum for escape clause
    escape_clause_min_oracle_age: int = 15  # Seconds - oracle must be older
    escape_clause_min_imbalance: float = 0.20  # 20% orderbook imbalance required
    escape_clause_min_liquidity: float = 75.0  # EUR minimum liquidity
    escape_clause_min_volume_surge: float = 2.5  # 2.5x volume surge required
    escape_clause_confidence_penalty: float = 0.10  # 10% confidence penalty
    
    # Volume authentication (prevents wash trading/fake breakouts)
    volume_surge_threshold: float = 1.2  # 1.2x average (lowered to allow more signals)
    
    # Spike concentration (anti-drift filter)
    spike_concentration_threshold: float = 0.30  # 30% of move in sharpest 10s (lowered significantly)
    
    # Volatility filter
    max_volatility_30s: float = 0.008  # 0.8% - slightly higher tolerance
    
    # Consensus / Exchange Agreement
    consensus_price_tolerance: float = 0.0015  # 0.15% max deviation
    min_agreement_score: float = 0.80  # 80% agreement quality required (lowered)
    
    # Mispricing detection (legacy - kept for backward compat)
    min_mispricing_pct: float = 0.03  # 3% mispricing required
    
    # Liquidity
    min_liquidity_eur: float = 50.0  # EUR at best price
    liquidity_collapse_threshold: float = 0.60  # Alert if <60% of 30s ago


class ConfidenceWeights(BaseSettings):
    """
    Confidence scoring component weights.
    
    NEW: Divergence-based weights (spot-PM divergence is primary signal).
    """
    
    # Primary signals (60% total)
    divergence_weight: float = 0.40      # Spot-PM divergence magnitude
    pm_staleness_weight: float = 0.20    # Orderbook age (stale = opportunity)
    
    # Supporting factors (40% total)
    consensus_strength_weight: float = 0.15
    liquidity_weight: float = 0.10
    volume_surge_weight: float = 0.08
    spike_concentration_weight: float = 0.07
    
    # Legacy weights (kept at 0 for backward compatibility)
    oracle_age_weight: float = 0.0       # No longer used as primary signal
    misalignment_weight: float = 0.0     # Replaced by divergence
    spread_anomaly_weight: float = 0.0   # Less relevant


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
    
    # Assets to trade (comma-separated)
    assets: str = Field(default="BTC", description="Comma-separated list of assets to trade (BTC,ETH,SOL,XRP)")
    
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

