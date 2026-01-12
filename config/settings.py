"""
Configuration settings for the Polymarket Oracle-Lag Trading Bot.
Uses pydantic-settings for validation and environment variable loading.
"""

from enum import Enum
from typing import Dict, Optional
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
    # LOWERED: 8% was too strict, missing 5-6% opportunities
    min_divergence_pct: float = 0.05  # 5% probability divergence required
    
    # PM Staleness window (CORRECTED: Stale PM + Divergence = OPPORTUNITY!)
    # If PM prices haven't changed but spot moved, MM is lagging = trade!
    min_pm_staleness_seconds: float = 0.0   # NO minimum! Fresh data = good
    optimal_pm_staleness_seconds: float = 0.0  # Fresh is best
    # Note: price_staleness ≠ data_staleness. Prices may not change during quiet periods.
    # Data freshness is checked separately in main.py (2 min max)
    # High divergence (>30%) bypasses staleness check entirely
    max_pm_staleness_seconds: float = 900.0  # 15 minutes - very generous for quiet markets
    soft_stale_threshold_seconds: float = 600.0  # Start soft penalty after 10 min
    
    # High divergence override - bypass supporting filters if divergence is massive
    high_divergence_override_pct: float = 0.30  # 30% divergence = always trade
    
    # Spot-implied probability scaling
    # Controls how sensitive the probability is to price moves
    # scale=100: 1% move → ~73% prob, 2% move → ~88% prob
    spot_implied_scale: float = 100.0  # Logistic curve scale factor
    
    # ==========================================================================
    # Spot movement thresholds (supporting filter)
    # ==========================================================================
    min_spot_move_pct: float = 0.0  # Disabled - divergence is primary signal
    atr_multiplier: float = 1.5  # move_threshold = max(0.7%, 1.5 * ATR)
    
    # Escape clause thresholds (allows sub-threshold moves when strongly supported)
    escape_clause_min_move: float = 0.008  # 0.8% minimum for escape clause
    escape_clause_min_oracle_age: int = 15  # Seconds - oracle must be older
    escape_clause_min_imbalance: float = 0.20  # 20% orderbook imbalance required
    escape_clause_min_liquidity: float = 75.0  # EUR minimum liquidity
    escape_clause_min_volume_surge: float = 2.5  # 2.5x volume surge required
    escape_clause_confidence_penalty: float = 0.10  # 10% confidence penalty
    
    # Volume authentication (prevents wash trading/fake breakouts)
    volume_surge_threshold: float = 0.0  # DISABLED - volume tracking broken (always <1.0x)
    
    # Spike concentration (anti-drift filter)
    spike_concentration_threshold: float = 0.0  # DISABLED - always 0% (calculation broken)
    
    # Volatility filter
    max_volatility_30s: float = 0.008  # 0.8% - slightly higher tolerance
    
    # Consensus / Exchange Agreement
    consensus_price_tolerance: float = 0.0020  # 0.20% max deviation (was 0.15%)
    min_agreement_score: float = 0.70  # 70% agreement - was 80% causing 1768 rejections!
    
    # Mispricing detection (legacy - kept for backward compat)
    min_mispricing_pct: float = 0.03  # 3% mispricing required
    
    # Liquidity - UPDATED: Increased from €1 (way too low!)
    # At €1, you're trading into markets with €10-50 total liquidity
    min_liquidity_eur: float = 10.0  # Production: ensure enough liquidity to fill positions
    liquidity_collapse_threshold: float = 0.50  # 50% drop triggers alert (was 60% - too sensitive)


class ConfidenceWeights(BaseSettings):
    """
    Confidence scoring component weights.
    
    UPDATED: Redistributed weights from broken filters (volume_surge, spike_concentration)
    to working components (divergence, liquidity).
    """
    
    # Primary signals (70% total) - INCREASED from 55%
    divergence_weight: float = 0.50      # Spot-PM divergence (PRIMARY SIGNAL) - was 0.35
    pm_staleness_weight: float = 0.20    # Orderbook age (stale = opportunity)
    
    # Supporting factors (30% total) - simplified
    consensus_strength_weight: float = 0.15  # Exchange agreement - was 0.12
    liquidity_weight: float = 0.10           # Liquidity depth (adjusted for volume surge)
    
    # DISABLED: These filters are broken (always return 0)
    volume_surge_weight: float = 0.05    # FIXED - now uses Z-score
    spike_concentration_weight: float = 0.0  # BROKEN - always 0%
    
    # Fee-aware scoring (0%) - reduce noise for now
    maker_advantage_weight: float = 0.0
    
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
    max_concurrent_positions: int = 3  # One per asset (BTC, ETH, SOL)
    
    # Circuit breakers
    daily_loss_limit_eur: float = 40.0
    max_consecutive_failed_fills: int = 3
    max_daily_gas_spend_eur: float = 10.0
    
    # Night mode limits
    night_mode_max_position_eur: float = 5.0
    night_mode_max_trades: int = 2
    night_mode_max_loss_eur: float = 40.0
    night_mode_min_confidence: float = 0.85
    night_mode_start_hour: int = 2  # 02:00
    night_mode_end_hour: int = 6  # 06:00


class AssetSpecificSettings(BaseSettings):
    """
    Per-asset configuration overrides.
    
    Different assets have different characteristics:
    - BTC: Efficient MMs (4-8s repricing), tight spreads → "Scalpel" strategy
    - ETH: Medium efficiency (6-12s repricing), lower volatility → "Sensitivity" strategy  
    - SOL: Slower MMs (10-15s repricing), high volatility → "Momentum" strategy
    
    These settings override the defaults in SignalSettings for specific assets.
    """
    
    # Signal Detection
    min_liquidity_eur: Optional[float] = None
    min_divergence_pct: Optional[float] = None
    spot_implied_scale: Optional[float] = None  # Sigmoid sensitivity (100=standard, 130=sensitive)
    
    # Staleness Window (asset-specific MM repricing speeds)
    optimal_staleness_min_s: Optional[float] = None  # Minimum staleness for opportunity
    optimal_staleness_max_s: Optional[float] = None  # Maximum before edge evaporates
    
    # Execution Parameters
    time_limit_s: Optional[float] = None  # Position time limit
    take_profit_pct: Optional[float] = None  # Take profit target
    stop_loss_eur: Optional[float] = None  # Absolute stop loss in EUR
    
    # Volatility Scaling (for ETH calm periods)
    volatility_scale_enabled: Optional[bool] = None
    volatility_scale_factor: Optional[float] = None  # Boost factor during low vol
    
    # Confidence threshold for alerts
    alert_confidence_threshold: Optional[float] = None
    
    # Price range to trade (avoid extreme favorites/underdogs)
    min_price: Optional[float] = None  # e.g., 0.10 = 10¢
    max_price: Optional[float] = None  # e.g., 0.90 = 90¢


class AssetConfigs(BaseSettings):
    """
    Container for all asset-specific configurations.
    
    Strategy per asset (based on MM efficiency analysis):
    - BTC: "Scalpel" - High quality, fast exit (MMs reprice in 4-8s)
    - ETH: "Sensitivity" - Lower threshold, volatility scaling (dormant periods)
    - SOL: "Momentum" - Proven settings, longer hold (MMs slower, 10-15s)
    
    Usage in .env:
        ASSET_BTC__MIN_LIQUIDITY_EUR=100
        ASSET_ETH__MIN_DIVERGENCE_PCT=0.08
    """
    
    # BTC: DISABLED for v2.2 - too many losses (117 losses = €-317 in testing)
    # MMs reprice too fast (4-8s), causing stop losses and liquidity collapses
    # Re-enable later with US VPS for lower latency
    BTC: AssetSpecificSettings = Field(default_factory=lambda: AssetSpecificSettings(
        # Signal Detection - EFFECTIVELY DISABLED
        min_liquidity_eur=40.0,
        min_divergence_pct=0.15,  # 15% - effectively disabled (rarely hit)
        spot_implied_scale=100.0,
        
        # Staleness Window
        optimal_staleness_min_s=4.0,
        optimal_staleness_max_s=10.0,
        
        # Execution
        time_limit_s=60.0,
        take_profit_pct=0.06,
        stop_loss_eur=0.035,
        
        volatility_scale_enabled=False,
        
        # Price range
        min_price=0.05,
        max_price=0.95,
    ))
    
    # ETH: "Sensitivity" Strategy - Unlock dormant asset
    # Lower volatility means smaller moves are more significant
    # v2.3: Lowered liquidity to €8 to match SOL
    ETH: AssetSpecificSettings = Field(default_factory=lambda: AssetSpecificSettings(
        # Signal Detection - More sensitive
        min_liquidity_eur=8.0,   # Production: reasonable liquidity
        min_divergence_pct=0.05, # 5% - Production: quality ETH signals
        spot_implied_scale=130.0, # ↑ from 100 - more sensitive to small moves
        
        # Staleness Window (ETH MMs slightly slower than BTC)
        optimal_staleness_min_s=8.0,
        optimal_staleness_max_s=15.0,
        
        # Execution
        time_limit_s=90.0,        # Keep standard
        take_profit_pct=0.06,     # ↓ from 8% - ETH doesn't overshoot
        stop_loss_eur=0.03,       # Keep current
        
        # Volatility scaling - boost sensitivity during calm periods
        volatility_scale_enabled=True,
        volatility_scale_factor=1.3,  # 30% boost during low vol
        
        # Price range
        min_price=0.08,
        max_price=0.92,
    ))
    
    # SOL: "Momentum" Strategy - THE PROFIT ENGINE
    # Slower MMs (10-15s), higher volatility = let positions breathe
    # v2.3: Lowered liquidity to €8 to catch 14-15% divergence opportunities
    SOL: AssetSpecificSettings = Field(default_factory=lambda: AssetSpecificSettings(
        # Signal Detection
        min_liquidity_eur=8.0,   # Production: reasonable liquidity
        min_divergence_pct=0.06, # 6% - Production: quality SOL signals
        spot_implied_scale=100.0,
        
        # Staleness Window
        optimal_staleness_min_s=8.0,
        optimal_staleness_max_s=12.0,
        
        # Execution - Let momentum play out
        time_limit_s=120.0,       # SOL trends longer
        take_profit_pct=0.09,     # 9% - SOL overshoots
        stop_loss_eur=0.03,
        
        volatility_scale_enabled=False,
        
        # Price range
        min_price=0.10,
        max_price=0.90,
    ))
    
    def get(self, asset: str) -> AssetSpecificSettings:
        """Get settings for a specific asset, with defaults."""
        return getattr(self, asset.upper(), AssetSpecificSettings())


class AlertSettings(BaseSettings):
    """Discord alerting settings."""
    
    discord_webhook_url: str = Field(default="", description="Discord webhook URL")
    alert_confidence_threshold: float = 0.50  # Lowered from 0.70 - max possible is ~80%
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
    
    # Real trading toggle (requires private_key to be set)
    real_trading_enabled: bool = Field(default=False, description="Enable real trading with actual money - DISABLED until exit bug fixed")
    real_trading_position_size_eur: float = Field(default=5.0, description="Position size in EUR for real trades")
    real_trading_max_daily_loss_eur: float = Field(default=25.0, description="Max daily loss before pausing real trades")
    real_trading_max_concurrent_positions: int = Field(default=3, description="Max concurrent real positions")
    
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
    asset_configs: AssetConfigs = Field(default_factory=AssetConfigs)
    
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

