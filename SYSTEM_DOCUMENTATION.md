# Polymarket Divergence Trading Bot - System Documentation

## Table of Contents

1. [Overview](#overview)
2. [Strategy](#strategy)
3. [System Architecture](#system-architecture)
4. [Multi-Asset Support](#multi-asset-support)
5. [Core Components](#core-components)
6. [Data Flow](#data-flow)
7. [Signal Detection Pipeline](#signal-detection-pipeline)
8. [Virtual Trading System](#virtual-trading-system)
9. [Operating Modes](#operating-modes)
10. [Configuration](#configuration)
11. [Risk Management](#risk-management)
12. [Monitoring & Alerting](#monitoring--alerting)
13. [Performance Metrics](#performance-metrics)
14. [Technical Details](#technical-details)

---

## Overview

The Polymarket Divergence Trading Bot is a sophisticated algorithmic trading system designed to exploit **market maker lag** on Polymarket's 15-minute prediction markets. When spot prices move, there's an 8-12 second window before Polymarket odds adjust.

### Key Features

- **Multi-Asset Support**: Monitors BTC, ETH, and SOL simultaneously
- **Real-time Multi-Exchange Data**: Aggregates spot prices from Binance, Coinbase, and Kraken
- **Divergence Detection**: Identifies spot-PM probability divergence (the core signal)
- **PM Staleness Tracking**: Monitors when Polymarket orderbook hasn't updated
- **Polymarket Integration**: Auto-discovers and monitors 15-minute markets
- **Virtual Trading**: Simulates trades with full P&L tracking before live execution
- **Multiple Operating Modes**: Shadow, Alert, and Night Auto modes
- **Rich Discord Alerts**: Real-time notifications with divergence metrics

### Edge Window

The exploitable window is **8-12 seconds** between:
1. Significant spot price movement (>0.7% in 30 seconds)
2. Spot-implied probability diverging from PM odds (>8% difference)
3. PM orderbook becoming stale (no price updates for 8+ seconds)

**Note**: The edge is *market maker lag*, not oracle lag. Polymarket uses Chainlink Data Streams (~100ms latency), but market makers take 8-12 seconds to reprice.

---

## Strategy

### Core Hypothesis (v3 - Divergence-Based)

The edge is **market maker lag**, not oracle lag:
- Polymarket uses Chainlink Data Streams (~100ms latency)
- But market makers wait 8-12 seconds before repricing odds
- This creates a window where spot price implies a different probability than PM shows

### Key Insight

Polymarket's 15-minute markets resolve via **Chainlink Data Streams** (not on-chain oracles):
- Resolution source: `data.chain.link/streams/btc-usd`
- Compares open vs. close price in 15-minute window
- "Up" if close ≥ open, "Down" otherwise

### Signal Logic

```python
# Calculate what spot price implies
spot_implied_prob = 1 / (1 + exp(-spot_move_30s * scale))

# Calculate divergence from PM odds
divergence = abs(spot_implied_prob - pm_yes_price)

# Signal conditions
if divergence >= 0.08 and pm_orderbook_age >= 8s:
    SIGNAL_DETECTED
```

### Why Divergence > Oracle Age

| Old Strategy (Oracle Age) | New Strategy (Divergence) |
|---------------------------|---------------------------|
| Wait for on-chain oracle to be stale | Detect when spot-PM probability diverges |
| 30-90 second windows | **8-12 second windows** |
| Oracle might not be used by PM | Directly measures mispricing |
| Edge sometimes evaporated | Edge is immediate and measurable |

### Signal Types

- **Standard Signals**: Meet all primary thresholds (divergence > 8%, PM stale > 8s)
- **Escape Clause Signals**: Sub-threshold moves supported by other factors

---

## System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Trading Bot (main.py)                        │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              MultiAssetManager                               │    │
│  │                                                              │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐                   │    │
│  │  │   BTC    │  │   ETH    │  │   SOL    │                   │    │
│  │  │ Feeds    │  │ Feeds    │  │ Feeds    │                   │    │
│  │  └──────────┘  └──────────┘  └──────────┘                   │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                       │
│         ┌────────────────────┼────────────────────┐                 │
│         │                    │                    │                 │
│  ┌──────▼──────┐     ┌───────▼───────┐    ┌──────▼──────┐          │
│  │   Engine    │     │   Operating   │    │  Alerting   │          │
│  │             │     │    Modes      │    │             │          │
│  │ - Consensus │     │ - Shadow      │    │ - Discord   │          │
│  │ - Detector  │     │ - Alert       │    │ - Logging   │          │
│  │ - Validator │     │ - Night Auto  │    │             │          │
│  │ - Scorer    │     │               │    │             │          │
│  └─────────────┘     └───────────────┘    └─────────────┘          │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Interaction

1. **MultiAssetManager** → Manages feeds for BTC, ETH, SOL simultaneously
2. **Feeds** → Stream real-time data from exchanges, oracle, and Polymarket
3. **Consensus Engine** → Aggregates multi-exchange data into unified price (per asset)
4. **Signal Detector** → Identifies potential trading opportunities
5. **Validator** → Performs additional safety checks
6. **Confidence Scorer** → Calculates signal quality score
7. **Mode Router** → Determines action based on operating mode
8. **Virtual Trader** → Simulates trades and tracks performance

---

## Multi-Asset Support

The bot supports monitoring multiple cryptocurrency assets simultaneously through the `MultiAssetManager`.

### Configured Assets

| Asset | Chainlink Feed | Exchange Symbols |
|-------|----------------|------------------|
| BTC | `0xc907E116054Ad103354f2D350FD2514433D57F6f` | btcusdt, BTC-USD, XBT/USD |
| ETH | `0xF9680D99D6C9589e2a93a78A04A279e509205945` | ethusdt, ETH-USD, ETH/USD |
| SOL | `0x10C8264C0935b3B9870013e057f330Ff3e9C56dC` | solusdt, SOL-USD, SOL/USD |

### Configuration

Set active assets in `.env`:
```bash
ASSETS=BTC,ETH,SOL
```

### MultiAssetManager (`src/engine/multi_asset.py`)

```python
class MultiAssetManager:
    """Manages feeds and data for multiple assets simultaneously."""
    
    def __init__(self):
        # Parse assets from settings
        self.assets = ["BTC", "ETH", "SOL"]  # from ASSETS env var
        self.asset_feeds: Dict[str, AssetFeeds] = {}
    
    async def initialize(self):
        """Initialize feeds for all configured assets."""
        for asset in self.assets:
            # Create exchange feeds (Binance, Coinbase, Kraken)
            # Create Chainlink feed (if address available)
            # Create Polymarket feed with auto-discovery
            # Create Consensus engine
            # Setup callbacks
    
    async def start(self):
        """Start all feeds for all assets."""
    
    def get_consensus(self, asset: str) -> Optional[ConsensusData]
    def get_oracle_data(self, asset: str) -> Optional[OracleData]
    def get_polymarket_data(self, asset: str) -> Optional[PolymarketData]
```

### Per-Asset Data Flow

Each asset gets its own:
- 3 exchange feeds (Binance, Coinbase, Kraken)
- 1 Chainlink oracle feed
- 1 Polymarket feed (auto-discovers markets for that asset)
- 1 Consensus engine

---

## Core Components

### 1. Data Feeds (`src/feeds/`)

#### Base Feed (`base.py`)

Abstract base class providing:
- WebSocket connection management
- Automatic reconnection with exponential backoff
- Health monitoring and staleness detection (30s threshold)
- Price buffer with rolling windows for metrics calculation
- Heartbeat/ping management

#### Exchange Feeds

**Binance Feed** (`binance.py`)
- WebSocket: `wss://stream.binance.com:9443/ws`
- Symbols: btcusdt, ethusdt, solusdt

**Coinbase Feed** (`coinbase.py`)
- WebSocket: `wss://ws-feed.exchange.coinbase.com`
- Products: BTC-USD, ETH-USD, SOL-USD

**Kraken Feed** (`kraken.py`)
- WebSocket: `wss://ws.kraken.com`
- Pairs: XBT/USD, ETH/USD, SOL/USD

**Common Metrics Calculated:**
- 30-second price movement (%)
- 30-second volatility (std dev of returns)
- 5-minute ATR (Average True Range)
- 10-second max move (for spike detection)
- 1-minute volume
- 5-minute average volume

#### Chainlink Feed (`chainlink.py`)

Monitors on-chain Chainlink price feeds on Polygon.

**Key Features:**
- Polls oracle contract every 2 seconds
- Tracks oracle age (time since last update)
- Heartbeat interval analysis
- Fast heartbeat mode detection
- Next update prediction

**Oracle Data Structure:**
```python
OracleData(
    current_value: float,           # Current oracle price
    last_update_timestamp_ms: int,  # When oracle last updated
    oracle_age_seconds: float,      # Age of oracle data
    round_id: int,                  # Chainlink round ID
    is_fast_heartbeat_mode: bool,   # Oracle updating frequently?
    next_heartbeat_estimate_ms: int # Predicted next update
)
```

#### Polymarket Feed (`polymarket.py`)

Monitors Polymarket orderbook for crypto 15-minute markets.

**Key Features:**
- **Multi-asset market discovery** (BTC, ETH, SOL)
- Market quality scoring (liquidity, age, spread, time-to-close)
- WebSocket subscription to orderbook updates
- 500ms snapshot frequency
- Orderbook imbalance calculation
- Liquidity collapse detection

---

### Market Discovery Process

Polymarket's 15-minute crypto markets predict if price will close higher or lower over a 15-minute window. They follow consistent naming and URL patterns.

**Market Structure:**
- **Title Format**: `"Bitcoin Up or Down - [Date], [Start Time]-[End Time] ET"`
- **URL Pattern**: `https://polymarket.com/event/btc-updown-15m-[UNIX_TIMESTAMP]`
- **Resolution**: Uses Chainlink oracle - resolves "Up" if price at end ≥ price at start

**API Discovery Methods:**

1. **Primary: Time-Based Slug Lookup**
   ```
   GET https://gamma-api.polymarket.com/markets?slug=btc-up-or-down-15m-[TIMESTAMP]
   ```
   The timestamp is the Unix epoch of the 15-minute window END time (e.g., `1767161700`).

2. **Fallback: Events API Search**
   ```
   GET https://gamma-api.polymarket.com/events?active=true&tag_id=235
   ```
   Filter for "15-min" or "up or down" in titles.

3. **CLOB API for Live Prices**
   ```
   GET https://clob.polymarket.com/book?token_id=[TOKEN_ID]
   ```

**Timestamp Calculation:**

```python
# Calculate current 15-minute window end (rounded up)
INTERVAL = 900  # 15 minutes in seconds
now = int(time.time())
window_end = ((now // INTERVAL) + 1) * INTERVAL

# Try current, next, and previous windows
timestamps = [window_end, window_end + 900, window_end - 900]
```

**Slug Patterns by Asset:**

| Asset | Slug Patterns |
|-------|---------------|
| BTC | `btc-up-or-down-15m-{ts}`, `btc-updown-15m-{ts}` |
| ETH | `eth-up-or-down-15m-{ts}`, `ethereum-up-or-down-15m-{ts}` |
| SOL | `sol-up-or-down-15m-{ts}`, `solana-up-or-down-15m-{ts}` |

**Tag IDs for Event Search Fallback:**

| Asset | Tag ID |
|-------|--------|
| BTC | 235 |
| ETH | 236 |
| SOL | (keyword search) |

**Discovery Flow:**

```
┌────────────────────────────────────────────────────────────┐
│ 1. Calculate current 15-min window timestamps              │
│    └── [window_end, window_end+900, window_end-900]        │
├────────────────────────────────────────────────────────────┤
│ 2. Try each slug pattern with each timestamp               │
│    └── GET /markets?slug=btc-up-or-down-15m-1767161700     │
├────────────────────────────────────────────────────────────┤
│ 3. If found: Extract condition_id, tokens, metadata        │
│    └── Parse clobTokenIds for YES/NO token IDs             │
├────────────────────────────────────────────────────────────┤
│ 4. Fallback: Search events API with tag_id                 │
│    └── Filter by "15" + "min" or "up or down" in title     │
├────────────────────────────────────────────────────────────┤
│ 5. Score market quality (liquidity, age, spread, ttc)      │
│    └── Select best market for trading                      │
└────────────────────────────────────────────────────────────┘
```

**Example API Response:**

```json
{
    "conditionId": "0x...",
    "question": "Bitcoin Up or Down - Jan 6, 2:00PM-2:15PM ET",
    "clobTokenIds": ["token_yes_id", "token_no_id"],
    "endDate": "2026-01-06T19:15:00Z",
    "closed": false,
    "outcomePrices": "[0.52, 0.48]",
    "volume": "15000"
}
```

**Market Quality Scoring:**

Once markets are discovered, they're scored for quality:

| Factor | Weight | Optimal Value |
|--------|--------|---------------|
| Liquidity | 40% | €10,000+ |
| Age | 30% | 30-300 seconds |
| Spread | 20% | <5% |
| Time to Close | 10% | >10 minutes |

---

**Orderbook Metrics:**

```python
PolymarketData(
    market_id: str,
    yes_bid: float,                    # Current YES bid price (= UP probability)
    yes_ask: float,
    no_bid: float,
    no_ask: float,
    yes_liquidity_best: float,
    spread: float,
    implied_probability: float,
    liquidity_collapsing: bool,
    orderbook_imbalance_ratio: float,  # -1 to +1 (positive = YES heavy)
    yes_depth_total: float,
    no_depth_total: float,
    
    # NEW: Staleness tracking for divergence strategy
    last_price_change_ms: int,         # When prices last changed
    orderbook_age_seconds: float,      # Seconds since last price change
)
```

**Staleness Tracking:**

The `orderbook_age_seconds` field is critical for the divergence strategy. It tracks how long since YES/NO prices changed:

```python
# In _create_snapshot():
price_changed = (
    abs(yes_bid - self._last_yes_bid) > 0.001 or
    abs(yes_ask - self._last_yes_ask) > 0.001 or
    ...
)

if price_changed:
    self._last_price_change_ms = now_ms

orderbook_age_seconds = (now_ms - self._last_price_change_ms) / 1000.0
```

### 2. Consensus Engine (`src/engine/consensus.py`)

Aggregates data from multiple exchanges into a unified consensus price (per asset).

#### Consensus Logic

1. **Price Agreement Check**
   - If all prices within 0.15%: Use volume-weighted average
   - If one outlier beyond 0.15%: Use median of three
   - If high deviation: Consensus failure (no signal)

2. **Staleness Filter**
   - Removes exchange data >10 seconds old
   - Requires at least 2 fresh exchanges

3. **Agreement Score**
   - Calculates quality of exchange agreement (0-1)
   - Used in confidence scoring

#### Volatility Regime Classification

- **LOW**: ATR < 25th percentile → Longer oracle age required
- **NORMAL**: 25th ≤ ATR ≤ 75th percentile
- **HIGH**: ATR > 75th percentile

### 3. Signal Detector (`src/engine/signal_detector.py`)

**NEW: Divergence-based detection (v3)**

Identifies trading opportunities based on **spot-PM divergence**, not oracle age.

#### Core Signal Logic

```python
# 1. Calculate spot-implied probability
spot_implied_prob = 1 / (1 + exp(-spot_move_30s * scale))

# 2. Compare to PM odds (YES price = UP probability)
divergence = abs(spot_implied_prob - pm_yes_price)

# 3. Check PM orderbook staleness
pm_age = time_since_last_price_change()

# 4. Signal conditions
if divergence >= 0.08 and pm_age >= 8s and pm_age <= 30s:
    SIGNAL_DETECTED
```

#### Primary Conditions (All Must Pass)

1. **Divergence Threshold** (PRIMARY) ✅ ACTIVE
   - Spot-implied prob differs from PM odds by ≥8%
   - This is the core signal

2. **PM Staleness Window** ⚠️ RELAXED
   - Currently: 0s - 600s (effectively disabled for testing)
   - Original: 8-30 seconds

3. **Movement Threshold** ⚠️ LOWERED
   - Currently: 0.05% minimum (lowered for testing)
   - Original: 0.7% minimum spot move in 30s

4. **Volume Authentication** ❌ DISABLED
   - Currently: Threshold set to 0.0x (always passes)
   - Issue: Volume surge calculation always returned <1.0x
   - To fix: Investigate volume baseline calculation

5. **Spike Concentration** ❌ DISABLED
   - Currently: Threshold set to 0% (always passes)
   - Issue: Spike concentration always returned 0%
   - To fix: Investigate spike detection algorithm

6. **Exchange Agreement** ✅ ACTIVE
   - Agreement score ≥ 0.80

7. **Liquidity Checks** ⚠️ LOWERED
   - Currently: €1 minimum (lowered for testing)
   - Original: €50 at best price
   - Liquidity collapse detection still active

#### High Divergence Override (v1.1)

**NEW**: When divergence exceeds 30%, ALL supporting filters are bypassed:

```python
HIGH_DIV_OVERRIDE_THRESHOLD = 0.30  # 30%

if divergence >= HIGH_DIV_OVERRIDE_THRESHOLD:
    # Skip volume, spike, spot move, agreement checks
    # Only check: liquidity collapse (safety)
    return SIGNAL_APPROVED
```

**Why**: Night session analysis showed 40%+ divergences being rejected due to minor filters like "spot move too small". This override ensures massive opportunities aren't missed.

#### Stale Data Filter (v1.1)

**NEW**: Signals are skipped if PM data is older than 5 minutes:

```python
MAX_PM_DATA_AGE_SECONDS = 300  # 5 minutes

if pm_data.orderbook_age_seconds > MAX_PM_DATA_AGE_SECONDS:
    log_warning("PM data too stale")
    return SKIP_SIGNAL_CHECK
```

**Why**: Night session showed decisions being made on 10-45 minute old data.

#### Escape Clause

Allows sub-threshold moves (0.8-1.0%) when strongly supported by other factors.
Applies 10% confidence penalty.

### 4. Confidence Scorer (`src/engine/confidence.py`)

**NEW: Divergence-based scoring (v3)**

| Component | Weight | Description |
|-----------|--------|-------------|
| **Divergence** | **0.40** | **Spot-PM probability divergence (PRIMARY)** |
| **PM Staleness** | **0.20** | **Orderbook age (8-12s optimal)** |
| Consensus Strength | 0.15 | Exchange agreement quality |
| Liquidity | 0.10 | Absolute liquidity + stability |
| Volume Surge | 0.08 | 2.5× volume = perfect score |
| Spike Concentration | 0.07 | 70% concentration = perfect |

**Note**: Oracle age is no longer weighted. The edge is market maker lag, not oracle lag.

#### Divergence Score Calculation

```python
def _score_divergence(spot_move_pct, pm_yes_price):
    # Calculate spot-implied probability
    spot_implied = 1 / (1 + exp(-spot_move_pct * 10 * 100))
    
    # Calculate divergence
    divergence = abs(spot_implied - pm_yes_price)
    
    # Normalize: 8% min, 15% = perfect score
    if divergence < 0.08:
        return 0.0
    return min(1.0, (divergence - 0.08) / 0.07)
```

#### PM Staleness Score Calculation

```python
def _score_pm_staleness(orderbook_age_seconds):
    # Optimal window: 8-12 seconds
    if orderbook_age_seconds < 8:
        return 0.0  # Too fresh
    elif orderbook_age_seconds <= 12:
        return (orderbook_age_seconds - 8) / 4  # Ramping up
    elif orderbook_age_seconds <= 30:
        return 1.0 - (orderbook_age_seconds - 12) / 18  # Ramping down
    else:
        return 0.0  # Too stale
```

#### Confidence Tiers

- **HIGH (★★★★★)**: ≥85%
- **GOOD (★★★★☆)**: ≥75%
- **MODERATE (★★★☆☆)**: ≥65%
- **LOW (★★☆☆☆)**: ≥55%
- **POOR (★☆☆☆☆)**: <55%

---

## Virtual Trading System

The virtual trading system simulates trades without real execution, allowing performance validation before going live.

### Virtual Trader (`src/modes/virtual_trader.py`)

```python
class VirtualTrader:
    """Simulates trades without real execution."""
    
    def __init__(
        self,
        polymarket_feed,
        chainlink_feed,
        position_size_eur: float = 20.0,
        take_profit_pct: float = 0.08,      # 8%
        stop_loss_pct: float = -0.03,       # -3%
        time_limit_seconds: float = 90.0,
        emergency_time_seconds: float = 120.0,
    )
```

### Virtual Position Tracking

```python
@dataclass
class VirtualPosition:
    position_id: str
    signal_id: str
    market_id: str
    direction: str  # "UP" or "DOWN"
    
    # Entry details
    entry_price: float
    entry_time_ms: int
    position_size_eur: float
    
    # Context at entry
    oracle_age_at_entry: float
    spread_at_entry: float
    confidence_at_entry: float
    spot_price_at_entry: float
    oracle_price_at_entry: float
    
    # Tracking
    max_profit_pct: float
    max_drawdown_pct: float
    current_price: Optional[float]
    
    # Exit (filled when closed)
    exit_price: Optional[float]
    exit_reason: Optional[str]
    realized_pnl_eur: Optional[float]
```

### Exit Conditions

| Exit Type | Condition | Priority |
|-----------|-----------|----------|
| Oracle Update Imminent | Age > 65s | 1 (Highest) |
| Spread Converged | Spread < 1.5% | 2 |
| Take Profit | PnL ≥ 8% | 3 |
| Stop Loss | PnL < -3% | 4 |
| Time Limit | Duration > 90s | 5 |
| Emergency | Duration > 120s | 6 |
| Liquidity Collapse | Detected | 7 |

### Performance Tracking

```python
@dataclass
class VirtualPerformance:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl_eur: float
    
    # Streaks
    current_streak: int
    best_streak: int
    worst_streak: int
    
    # Best/worst trades
    best_trade_pnl_eur: float
    worst_trade_pnl_eur: float
    
    # Exit reason breakdown
    exit_reasons: Dict[str, int]
    
    # Hourly stats
    trades_by_hour: Dict[int, Dict]
```

---

## Operating Modes

### Shadow Mode (`modes/shadow.py`)

**Purpose**: Simulate all trades and collect data without risk.

**Features:**
- Simulates all trades
- Tracks would-be performance
- Collects comprehensive logs
- Builds oracle timing distribution
- Discord notifications for signals

**Use Case:**
- Run for 2-4 weeks before live trading
- Validate strategy
- Understand oracle timing patterns

### Alert Mode (`modes/alert.py`)

**Purpose**: Human-in-the-loop trading with virtual simulation.

**Features:**
- Sends Discord alerts for signals ≥70% confidence
- **Virtual Trading**: Automatically opens simulated positions
- Rich embeds with full market context
- Position updates every 30 seconds
- Closure alerts with P&L
- Hourly performance summaries

**Virtual Trading in Alert Mode:**
```python
class AlertMode:
    def __init__(self, polymarket_feed, chainlink_feed):
        self._virtual_trader = VirtualTrader(
            polymarket_feed,
            chainlink_feed,
            position_size_eur=20.0,
        )
        
        # Setup callbacks for Discord alerts
        self._virtual_trader.set_callbacks(
            on_opened=self._on_virtual_position_opened,
            on_update=self._on_virtual_position_update,
            on_closed=self._on_virtual_position_closed,
        )
```

### Night Auto Mode (`modes/night_auto.py`)

**Purpose**: Conservative automated trading during low-competition hours.

**Active Hours**: 02:00 - 06:00 local time

**Requirements:**
- Confidence ≥85%
- Within night hours
- Max 2 trades per night
- Daily loss cap €40

---

## Configuration

### Environment Variables (`.env`)

```bash
# Operating Mode
MODE=alert  # shadow, alert, night_auto

# Assets to monitor
ASSETS=BTC,ETH,SOL

# Polygon RPC (required for Chainlink)
CHAINLINK__POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY
CHAINLINK__POLYGON_WS_URL=wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Discord Alerts
ALERTS__DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Wallet (for live trading)
WALLET_ADDRESS=0x...
PRIVATE_KEY=...
```

### Settings Structure (`config/settings.py`)

```python
class Settings(BaseSettings):
    # Operating mode
    mode: OperatingMode = OperatingMode.SHADOW
    
    # Assets to trade
    assets: str = "BTC"  # Comma-separated: "BTC,ETH,SOL"
    
    # Sub-settings
    exchanges: ExchangeSettings
    chainlink: ChainlinkSettings
    polymarket: PolymarketSettings
    signals: SignalSettings
    confidence: ConfidenceWeights
    execution: ExecutionSettings
    risk: RiskSettings
    alerts: AlertSettings
```

### Key Thresholds

| Setting | Current Value | Description |
|---------|---------------|-------------|
| `min_divergence_pct` | 8% | Minimum spot-PM probability divergence |
| `min_pm_staleness_seconds` | 0s | PM staleness minimum (DISABLED) |
| `max_pm_staleness_seconds` | 600s | PM staleness maximum |
| `min_spot_move_pct` | 0.05% | Minimum price movement (lowered for testing) |
| `volume_surge_threshold` | 0.0x | DISABLED (calculation broken) |
| `spike_concentration_threshold` | 0% | DISABLED (calculation broken) |
| `min_liquidity_eur` | €1 | Minimum liquidity (lowered for testing) |
| `min_agreement_score` | 80% | Exchange agreement quality |
| `alert_confidence_threshold` | 70% | Min confidence for alerts |
| `night_mode_min_confidence` | 85% | Min confidence for auto trades |

**Note**: Volume surge and spike concentration filters are currently disabled due to calculation issues. They will be fixed in a future update.

---

## Risk Management

### Position Limits

- **Max Position Size**: €20 (night mode) or 0.5% of bankroll
- **Max Concurrent Positions**: 1
- **Max Daily Exposure**: 5% of bankroll

### Circuit Breakers

1. **Consecutive Failed Fills**: Pause after 3 failures
2. **Daily Loss Limit**: Pause at €40 loss
3. **Daily Gas Limit**: Pause at €10 gas spent

### Virtual Trading Safeguards

- Stop loss at -3%
- Time limit at 90 seconds
- Emergency exit at 120 seconds
- Liquidity collapse detection

---

## Monitoring & Alerting

### Discord Notifications

**Status Updates (every 60s):**
```
📊 System Status

BTC:
  Exchanges: Binance: ✅ $97,245.00 Coinbase: ✅ $97,243.50 Kraken: ✅ $97,244.00
  PM: YES:0.52 NO:0.48
  Oracle: $97,200.00 (35s)

ETH:
  Exchanges: Binance: ✅ $3,456.00 ...
  PM: Discovering market...
  Oracle: $3,450.00 (42s)
```

**Signal Alerts:**
- Direction and asset
- Confidence score with visual indicator
- Market data (spot, oracle, mispricing)
- Orderbook state
- Virtual position opened notification

**Position Updates (every 30s):**
- Current P&L
- Max profit/drawdown
- Time in position
- Exit probability

**Position Closures:**
- Exit reason
- Realized P&L (% and €)
- Duration
- Max profit/drawdown during position

**Hourly Summaries:**
- Trades this hour
- Win rate
- Total P&L
- Best/worst hour stats

### Logging System

**Signal Logs** (`logs/signals_YYYY-MM-DD.jsonl`):
- Full signal context
- Consensus data
- Oracle data
- Polymarket data
- Scoring breakdown
- Outcome (if simulated)

**Metrics Logs** (`logs/metrics.jsonl`):
- Feed health
- Connection status
- Price snapshots

---

## Performance Metrics

### Target Metrics

| Metric | Target |
|--------|--------|
| Win Rate | ≥65% |
| Avg Profit/Trade | ≥€1.50 |
| E2E Latency | <200ms |
| Signals/Day | 5-15 |

### Shadow Mode Success Criteria

Before going live:
- 100+ shadow signals logged
- Would-be win rate ≥65%
- Would-be avg profit ≥€1.50/trade
- Consistent across multiple days

---

## Technical Details

### Async Architecture

All components use asyncio for concurrent operations:
- **Feed Connections**: Parallel WebSocket connections per asset
- **Signal Checking**: 500ms tick loop per asset
- **Health Monitoring**: 10s interval
- **Status Updates**: 60s interval
- **Virtual Position Monitoring**: 1s interval per position

### Error Handling

- **Connection Failures**: Automatic reconnection with exponential backoff (max 60s)
- **Stale Data**: Filtered out after 30 seconds
- **Validation Failures**: Logged and rejected
- **Rate Limits**: Automatic backoff for Discord

### Dependencies

```
# Core
pydantic>=2.0
pydantic-settings>=2.0
structlog>=23.0

# Async
asyncio
aiohttp
websockets>=12.0
httpx>=0.25

# Blockchain
web3>=6.0

# Data
numpy
```

### Project Structure

```
polybot/
├── config/
│   └── settings.py          # All configuration
├── src/
│   ├── main.py              # Main orchestrator
│   ├── engine/
│   │   ├── consensus.py     # Multi-exchange aggregation
│   │   ├── signal_detector.py
│   │   ├── validator.py
│   │   ├── confidence.py
│   │   ├── execution.py     # Trade execution
│   │   └── multi_asset.py   # Asset orchestration
│   ├── feeds/
│   │   ├── base.py          # Base WebSocket feed
│   │   ├── binance.py
│   │   ├── coinbase.py
│   │   ├── kraken.py
│   │   ├── chainlink.py     # Oracle feed
│   │   └── polymarket.py    # Orderbook + discovery
│   ├── modes/
│   │   ├── shadow.py
│   │   ├── alert.py
│   │   ├── night_auto.py
│   │   └── virtual_trader.py
│   ├── models/
│   │   └── schemas.py       # Data models
│   └── utils/
│       ├── alerts.py        # Discord alerter
│       └── logging.py       # Signal logger
├── logs/                    # Signal and metrics logs
├── .env                     # Environment config
└── requirements.txt
```

---

## Quick Start

1. **Clone and install:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure `.env`:**
   ```bash
   MODE=alert
   ASSETS=BTC,ETH,SOL
   CHAINLINK__POLYGON_RPC_URL=https://...
   ALERTS__DISCORD_WEBHOOK_URL=https://...
   ```

3. **Run:**
   ```bash
   python -m src.main
   ```

4. **Monitor:**
   - Watch Discord for status updates and signals
   - Check `logs/` for detailed signal history

---

## Roadmap

### Current Status (v1.1 - January 2026)

✅ Multi-asset support (BTC, ETH, SOL)  
✅ Virtual trading with P&L tracking  
✅ Rich Discord alerts  
✅ Orderbook imbalance detection  
✅ Market quality scoring  
✅ Auto market discovery  
✅ VPS deployment guide  
✅ Signal rejection logging (INFO level)  

### Recent Changes (v1.1)

**Critical Fixes (Latest):**
- **HIGH DIVERGENCE OVERRIDE**: If divergence >30%, bypass all supporting filters
- **STALE DATA FILTER**: Skip signals if PM data >5 minutes old
- **ORACLE OPTIONAL**: Divergence strategy doesn't require oracle (spot-PM is primary signal)

**Signal Detection Tuning:**
- Volume surge filter: **DISABLED** (was always <1.0x due to calculation issue)
- Spike concentration filter: **DISABLED** (was always 0%)
- PM staleness minimum: **DISABLED** (PM updates faster than expected)
- PM staleness maximum: Increased to 600s
- Min spot move: Lowered to 0.05% (testing)
- Min liquidity: Lowered to €1 (testing)

**Discord Alerter Improvements:**
- Fresh HTTP client per request (avoids stale connection issues)
- Timeout increased to 30s for all operations
- Better retry logic with exponential backoff

**Logging Improvements:**
- Signal rejections now logged at INFO level (visible in normal logs)
- Shows exactly which filter blocked each signal
- 30-second rejection stats summary
- High divergence override logged as 🚀

**VPS Deployment:**
- Complete VPS setup guide added (VPS_SETUP.md)
- Hetzner CX22 recommended (€4.35/mo)
- Systemd service for auto-restart
- Convenience aliases for management

### Future Enhancements

- [ ] Fix volume surge calculation (currently disabled)
- [ ] Fix spike concentration calculation (currently disabled)
- [ ] Historical backtesting module
- [ ] Machine learning signal enhancement
- [ ] Additional assets (DOGE, AVAX, etc.)
- [ ] Web dashboard for monitoring
- [ ] Database storage for long-term analysis
