"""
Data models and schemas for the trading bot.
All data structures are defined using Pydantic for validation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SignalDirection(str, Enum):
    """Trading signal direction."""
    UP = "up"
    DOWN = "down"


class SignalType(str, Enum):
    """Type of signal generated."""
    STANDARD = "standard"
    ESCAPE_CLAUSE = "escape_clause"


class VolatilityRegime(str, Enum):
    """Market volatility classification."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ActionDecision(str, Enum):
    """What action the bot took."""
    TRADE = "trade"
    ALERT = "alert"
    SHADOW = "shadow"
    REJECT = "reject"


class ExitReason(str, Enum):
    """Reason for position exit."""
    SPREAD_CONVERGED = "spread_converged"
    TAKE_PROFIT = "take_profit"
    TIME_EXIT = "time_exit"
    EMERGENCY = "emergency"
    STOP_LOSS = "stop_loss"
    MANUAL = "manual"


class RejectionReason(str, Enum):
    """Reason a signal was rejected."""
    CONSENSUS_FAILURE = "consensus_failure"
    INSUFFICIENT_MOVE = "insufficient_move"
    SMOOTH_DRIFT = "smooth_drift"
    VOLUME_LOW = "volume_low"
    ORACLE_TOO_FRESH = "oracle_too_fresh"
    ORACLE_TOO_STALE = "oracle_too_stale"
    FAST_HEARTBEAT_MODE = "fast_heartbeat_mode"
    LIQUIDITY_LOW = "liquidity_low"
    LIQUIDITY_COLLAPSING = "liquidity_collapsing"
    SPREAD_CONVERGING = "spread_converging"
    DIRECTION_REVERSED = "direction_reversed"
    HISTORICAL_WIN_RATE_LOW = "historical_win_rate_low"
    SLIPPAGE_TOO_HIGH = "slippage_too_high"
    GAS_TOO_HIGH = "gas_too_high"
    FEE_UNFAVORABLE = "fee_unfavorable"  # Edge doesn't exceed effective fees
    VOLATILITY_TOO_HIGH = "volatility_too_high"
    CONFIDENCE_TOO_LOW = "confidence_too_low"
    FEED_STALE = "feed_stale"


# --- Exchange Data Models ---

@dataclass
class ExchangeTick:
    """Single tick from an exchange."""
    exchange: str
    symbol: str
    price: float
    timestamp_ms: int
    local_timestamp_ms: int
    volume_1m: float = 0.0


@dataclass
class ExchangeMetrics:
    """Calculated metrics for a single exchange."""
    exchange: str
    current_price: float
    exchange_timestamp_ms: int
    local_timestamp_ms: int
    
    # 30-second rolling window
    move_30s_pct: float = 0.0
    velocity_30s: float = 0.0
    volatility_30s: float = 0.0
    
    # Volume and ATR
    volume_1m: float = 0.0
    volume_5m_avg: float = 0.0  # Rolling 5-minute average volume
    atr_5m: float = 0.0
    
    # Spike detection
    max_move_10s_pct: float = 0.0


@dataclass
class ConsensusData:
    """Aggregated consensus data from all exchanges."""
    consensus_price: float
    consensus_timestamp_ms: int
    
    # Individual exchange data
    binance: Optional[ExchangeMetrics] = None
    coinbase: Optional[ExchangeMetrics] = None
    kraken: Optional[ExchangeMetrics] = None
    
    # Consensus metrics
    move_30s_pct: float = 0.0
    volatility_30s: float = 0.0
    atr_5m: float = 0.0
    volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL
    
    # Spike concentration
    max_10s_move_pct: float = 0.0
    spike_concentration: float = 0.0
    
    # Volume
    total_volume_1m: float = 0.0
    avg_volume_5m: float = 0.0
    volume_surge_ratio: float = 0.0
    
    # Agreement - NEW: Added agreement_score (0-1) for exchange agreement quality
    agreement: bool = False
    max_deviation_pct: float = 0.0
    agreement_score: float = 0.0  # 1.0 = perfect agreement, 0.0 = high deviation
    
    # Number of exchanges contributing to consensus
    exchange_count: int = 0


# --- Oracle Data Models ---

@dataclass
class OracleData:
    """Chainlink oracle state."""
    current_value: float
    last_update_timestamp_ms: int
    oracle_age_seconds: float
    round_id: int
    
    # Heartbeat analysis
    recent_heartbeat_intervals: list[float] = field(default_factory=list)
    avg_heartbeat_interval: float = 60.0
    next_heartbeat_estimate_ms: Optional[int] = None
    is_fast_heartbeat_mode: bool = False


# --- Polymarket Data Models ---

@dataclass
class OrderbookLevel:
    """Single level in orderbook."""
    price: float
    size: float  # in EUR equivalent


@dataclass
class PolymarketData:
    """Polymarket orderbook state."""
    market_id: str
    timestamp_ms: int
    
    # YES token
    yes_bid: float
    yes_ask: float
    yes_liquidity_best: float
    
    # NO token
    no_bid: float
    no_ask: float
    no_liquidity_best: float
    
    # Fields with defaults (must come after non-default fields)
    yes_depth_3: list[OrderbookLevel] = field(default_factory=list)
    no_depth_3: list[OrderbookLevel] = field(default_factory=list)
    
    # Derived metrics
    spread: float = 0.0
    implied_probability: float = 0.0
    
    # Liquidity history for collapse detection
    liquidity_30s_ago: float = 0.0
    liquidity_60s_ago: float = 0.0
    liquidity_collapsing: bool = False
    
    # Orderbook imbalance
    orderbook_imbalance_ratio: float = 0.0
    yes_depth_total: float = 0.0
    no_depth_total: float = 0.0
    
    # NEW: Orderbook staleness tracking (for divergence strategy)
    last_price_change_ms: int = 0  # When YES/NO prices last changed
    orderbook_age_seconds: float = 0.0  # Seconds since last price change
    
    # NEW: Fee tracking (Polymarket fee update Jan 2026)
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_fee_rate_bps: int = 0  # e.g., 1000 = 0.1% base rate
    no_fee_rate_bps: int = 0
    
    @property
    def yes_fee_pct(self) -> float:
        """Convert bps to percentage (decimal)."""
        return self.yes_fee_rate_bps / 10000
    
    @property
    def no_fee_pct(self) -> float:
        """Convert bps to percentage (decimal)."""
        return self.no_fee_rate_bps / 10000
    
    def calculate_effective_fee(self, side: str, price: float, is_maker: bool = False) -> float:
        """
        Calculate effective fee for a trade.
        
        Fee structure (Jan 2026):
        - Makers: 0% fee + daily rebate
        - Takers: 0.25% base rate squared by price
          - For YES: effective_fee = base_fee * price
          - For NO: effective_fee = base_fee * (1 - price)
        
        Args:
            side: "YES" or "NO"
            price: Entry price (0.0 - 1.0)
            is_maker: If True, return 0 (maker gets rebate)
        
        Returns:
            Effective fee as decimal (e.g., 0.016 = 1.6%)
        """
        if is_maker:
            return 0.0  # Makers pay no fees
        
        # Get base fee rate
        base_fee_bps = self.yes_fee_rate_bps if side == "YES" else self.no_fee_rate_bps
        base_fee = base_fee_bps / 10000  # Convert to decimal
        
        # Fee is squared by price
        if side == "YES":
            effective_fee = base_fee * price
        else:
            effective_fee = base_fee * (1 - price)
        
        return effective_fee
    
    def get_orderbook_age_ms(self) -> int:
        """Get milliseconds since last orderbook price change."""
        import time
        if self.last_price_change_ms == 0:
            return 0
        return int(time.time() * 1000) - self.last_price_change_ms


@dataclass
class DivergenceData:
    """
    Divergence signal data for spot-PM strategy.
    
    The core edge: when spot price moves but PM odds haven't adjusted yet.
    """
    spot_implied_prob: float  # Probability implied by spot price momentum
    pm_implied_prob: float    # Current PM YES price (= UP probability)
    divergence: float         # Absolute difference between the two
    pm_orderbook_age_seconds: float  # How long PM odds have been stale
    
    # Direction and strength
    signal_direction: str = ""  # "UP" or "DOWN"
    is_actionable: bool = False  # Meets minimum thresholds
    
    # Thresholds used
    min_divergence: float = 0.08  # 8% probability difference
    min_pm_age: float = 8.0  # Seconds of PM staleness required


# --- Scoring Models ---

@dataclass
class ConfidenceBreakdown:
    """Breakdown of confidence score components."""
    # Primary signal weights (60%)
    divergence: float = 0.0         # 35% - Spot-PM divergence magnitude
    pm_staleness: float = 0.0       # 20% - Orderbook age (stale = opportunity)
    
    # Supporting factors (35%)
    consensus_strength: float = 0.0  # 12% - Exchange agreement quality
    liquidity: float = 0.0           # 8%  - Available depth
    volume_surge: float = 0.0        # 5%  - Volume authentication
    spike_concentration: float = 0.0 # 5%  - Move quality (spike vs drift)
    
    # Fee-aware scoring (5%)
    maker_advantage: float = 0.0     # 5%  - Maker order viability
    
    # Legacy (kept for backwards compatibility, weight=0)
    oracle_age: float = 0.0
    misalignment: float = 0.0
    spread_anomaly: float = 0.0


@dataclass
class ScoringData:
    """Full scoring information for a signal."""
    confidence: float
    breakdown: ConfidenceBreakdown
    escape_clause_used: bool = False
    confidence_penalty: float = 0.0


# --- Validation Models ---

@dataclass
class ValidationResult:
    """Result of signal validation checks."""
    passed: bool
    directional_persistence: bool = True
    liquidity_sufficient: bool = True
    liquidity_not_collapsing: bool = True
    oracle_window_safe: bool = True
    spread_not_converging: bool = True
    volume_authenticated: bool = True
    spike_not_smooth_drift: bool = True
    historical_win_rate: float = 0.0
    rejection_reason: Optional[RejectionReason] = None


# --- Action Models ---

@dataclass
class ActionData:
    """Action taken on a signal."""
    mode: str
    decision: ActionDecision
    order_id: Optional[str] = None
    position_size_eur: float = 0.0
    entry_price: float = 0.0
    gas_used: int = 0
    gas_price_gwei: float = 0.0
    gas_cost_eur: float = 0.0
    fill_delay_ms: int = 0
    nonce: int = 0


# --- Outcome Models ---

@dataclass
class OutcomeData:
    """Outcome of a trade."""
    filled: bool
    fill_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: Optional[ExitReason] = None
    gross_profit_eur: float = 0.0
    net_profit_eur: float = 0.0
    profit_pct: float = 0.0
    oracle_updated_at_ms: Optional[int] = None
    oracle_update_delay_after_signal_s: float = 0.0
    position_duration_seconds: float = 0.0
    max_adverse_move_pct: float = 0.0
    notes: str = ""


# --- Main Signal Log Model ---

class SpotDataLog(BaseModel):
    """Spot data section of signal log."""
    binance: dict = Field(default_factory=dict)
    coinbase: dict = Field(default_factory=dict)
    kraken: dict = Field(default_factory=dict)
    consensus_price: float
    consensus_move_30s_pct: float
    consensus_volatility_30s: float
    consensus_5m_atr: float
    volatility_regime: str
    max_10s_move_pct: float
    spike_concentration: float
    volume_surge_ratio: float
    agreement: bool
    agreement_score: float = 0.0  # Exchange agreement quality (0-1)
    exchange_count: int = 0  # Number of exchanges contributing


class OracleDataLog(BaseModel):
    """Oracle data section of signal log."""
    current_value: float
    last_update_ts: int
    oracle_age_seconds: float
    next_heartbeat_estimate_ts: Optional[int] = None
    recent_heartbeat_intervals: list[float] = Field(default_factory=list)


class PolymarketDataLog(BaseModel):
    """Polymarket data section of signal log."""
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread: float
    liquidity_yes_best: float
    liquidity_yes_depth_3: list[float] = Field(default_factory=list)
    liquidity_30s_ago: float
    liquidity_60s_ago: float
    liquidity_collapsing: bool
    orderbook_imbalance_ratio: float
    last_orderbook_update_ts: int


class ScoringLog(BaseModel):
    """Scoring section of signal log."""
    confidence: float
    breakdown: dict = Field(default_factory=dict)
    escape_clause_used: bool
    confidence_penalty: float


class ValidationLog(BaseModel):
    """Validation section of signal log."""
    passed: bool
    directional_persistence: bool
    liquidity_sufficient: bool
    liquidity_not_collapsing: bool
    oracle_window_safe: bool
    spread_not_converging: bool
    volume_authenticated: bool
    spike_not_smooth_drift: bool
    historical_win_rate: float
    rejection_reason: Optional[str] = None


class ActionLog(BaseModel):
    """Action section of signal log."""
    mode: str
    decision: str
    order_id: Optional[str] = None
    position_size_eur: float = 0.0
    entry_price: float = 0.0
    gas_used: int = 0
    gas_price_gwei: float = 0.0
    gas_cost_eur: float = 0.0
    fill_delay_ms: int = 0
    nonce: int = 0


class OutcomeLog(BaseModel):
    """Outcome section of signal log."""
    filled: bool = False
    fill_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: Optional[str] = None
    gross_profit_eur: float = 0.0
    net_profit_eur: float = 0.0
    profit_pct: float = 0.0
    oracle_updated_at_ts: Optional[int] = None
    oracle_update_delay_after_signal_s: float = 0.0
    position_duration_seconds: float = 0.0
    max_adverse_move_pct: float = 0.0
    notes: str = ""


class SignalLog(BaseModel):
    """
    Complete signal log entry.
    This is the master schema for all logged signals.
    """
    timestamp_ms: int
    signal_id: str = Field(default_factory=lambda: str(uuid4()))
    market_id: str
    direction: str
    signal_type: str
    
    spot_data: SpotDataLog
    oracle_data: OracleDataLog
    polymarket_data: PolymarketDataLog
    scoring: ScoringLog
    validation: ValidationLog
    action: ActionLog
    outcome: OutcomeLog = Field(default_factory=lambda: OutcomeLog(filled=False))
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }


# --- Signal candidate for internal use ---

@dataclass
class SignalCandidate:
    """
    Internal representation of a potential signal.
    Used during signal detection and validation.
    """
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp_ms: int = 0
    market_id: str = ""
    direction: SignalDirection = SignalDirection.UP
    signal_type: SignalType = SignalType.STANDARD
    
    # Raw data
    consensus: Optional[ConsensusData] = None
    oracle: Optional[OracleData] = None
    polymarket: Optional[PolymarketData] = None
    
    # Scoring
    scoring: Optional[ScoringData] = None
    
    # Validation
    validation: Optional[ValidationResult] = None
    
    # Whether signal passed all checks
    is_valid: bool = False
    
    def to_log(self) -> SignalLog:
        """Convert to logging format."""
        return SignalLog(
            timestamp_ms=self.timestamp_ms,
            signal_id=self.signal_id,
            market_id=self.market_id,
            direction=self.direction.value,
            signal_type=self.signal_type.value,
            spot_data=SpotDataLog(
                binance={
                    "price": self.consensus.binance.current_price if self.consensus and self.consensus.binance else 0,
                    "ts": self.consensus.binance.exchange_timestamp_ms if self.consensus and self.consensus.binance else 0,
                    "volume_1m": self.consensus.binance.volume_1m if self.consensus and self.consensus.binance else 0,
                } if self.consensus else {},
                coinbase={
                    "price": self.consensus.coinbase.current_price if self.consensus and self.consensus.coinbase else 0,
                    "ts": self.consensus.coinbase.exchange_timestamp_ms if self.consensus and self.consensus.coinbase else 0,
                    "volume_1m": self.consensus.coinbase.volume_1m if self.consensus and self.consensus.coinbase else 0,
                } if self.consensus else {},
                kraken={
                    "price": self.consensus.kraken.current_price if self.consensus and self.consensus.kraken else 0,
                    "ts": self.consensus.kraken.exchange_timestamp_ms if self.consensus and self.consensus.kraken else 0,
                    "volume_1m": self.consensus.kraken.volume_1m if self.consensus and self.consensus.kraken else 0,
                } if self.consensus else {},
                consensus_price=self.consensus.consensus_price if self.consensus else 0,
                consensus_move_30s_pct=self.consensus.move_30s_pct if self.consensus else 0,
                consensus_volatility_30s=self.consensus.volatility_30s if self.consensus else 0,
                consensus_5m_atr=self.consensus.atr_5m if self.consensus else 0,
                volatility_regime=self.consensus.volatility_regime.value if self.consensus else "normal",
                max_10s_move_pct=self.consensus.max_10s_move_pct if self.consensus else 0,
                spike_concentration=self.consensus.spike_concentration if self.consensus else 0,
                volume_surge_ratio=self.consensus.volume_surge_ratio if self.consensus else 0,
                agreement=self.consensus.agreement if self.consensus else False,
                agreement_score=self.consensus.agreement_score if self.consensus else 0,
                exchange_count=self.consensus.exchange_count if self.consensus else 0,
            ),
            oracle_data=OracleDataLog(
                current_value=self.oracle.current_value if self.oracle else 0,
                last_update_ts=self.oracle.last_update_timestamp_ms if self.oracle else 0,
                oracle_age_seconds=self.oracle.oracle_age_seconds if self.oracle else 0,
                next_heartbeat_estimate_ts=self.oracle.next_heartbeat_estimate_ms if self.oracle else None,
                recent_heartbeat_intervals=self.oracle.recent_heartbeat_intervals if self.oracle else [],
            ),
            polymarket_data=PolymarketDataLog(
                yes_bid=self.polymarket.yes_bid if self.polymarket else 0,
                yes_ask=self.polymarket.yes_ask if self.polymarket else 0,
                no_bid=self.polymarket.no_bid if self.polymarket else 0,
                no_ask=self.polymarket.no_ask if self.polymarket else 0,
                spread=self.polymarket.spread if self.polymarket else 0,
                liquidity_yes_best=self.polymarket.yes_liquidity_best if self.polymarket else 0,
                liquidity_yes_depth_3=[l.size for l in self.polymarket.yes_depth_3] if self.polymarket else [],
                liquidity_30s_ago=self.polymarket.liquidity_30s_ago if self.polymarket else 0,
                liquidity_60s_ago=self.polymarket.liquidity_60s_ago if self.polymarket else 0,
                liquidity_collapsing=self.polymarket.liquidity_collapsing if self.polymarket else False,
                orderbook_imbalance_ratio=self.polymarket.orderbook_imbalance_ratio if self.polymarket else 1.0,
                last_orderbook_update_ts=self.polymarket.timestamp_ms if self.polymarket else 0,
            ),
            scoring=ScoringLog(
                confidence=self.scoring.confidence if self.scoring else 0,
                breakdown={
                    "oracle_age": self.scoring.breakdown.oracle_age if self.scoring else 0,
                    "consensus_strength": self.scoring.breakdown.consensus_strength if self.scoring else 0,
                    "misalignment": self.scoring.breakdown.misalignment if self.scoring else 0,
                    "liquidity": self.scoring.breakdown.liquidity if self.scoring else 0,
                    "spread_anomaly": self.scoring.breakdown.spread_anomaly if self.scoring else 0,
                    "volume_surge": self.scoring.breakdown.volume_surge if self.scoring else 0,
                    "spike_concentration": self.scoring.breakdown.spike_concentration if self.scoring else 0,
                },
                escape_clause_used=self.scoring.escape_clause_used if self.scoring else False,
                confidence_penalty=self.scoring.confidence_penalty if self.scoring else 0,
            ),
            validation=ValidationLog(
                passed=self.validation.passed if self.validation else False,
                directional_persistence=self.validation.directional_persistence if self.validation else True,
                liquidity_sufficient=self.validation.liquidity_sufficient if self.validation else True,
                liquidity_not_collapsing=self.validation.liquidity_not_collapsing if self.validation else True,
                oracle_window_safe=self.validation.oracle_window_safe if self.validation else True,
                spread_not_converging=self.validation.spread_not_converging if self.validation else True,
                volume_authenticated=self.validation.volume_authenticated if self.validation else True,
                spike_not_smooth_drift=self.validation.spike_not_smooth_drift if self.validation else True,
                historical_win_rate=self.validation.historical_win_rate if self.validation else 0,
                rejection_reason=self.validation.rejection_reason.value if self.validation and self.validation.rejection_reason else None,
            ),
            action=ActionLog(mode="shadow", decision="shadow"),
        )

