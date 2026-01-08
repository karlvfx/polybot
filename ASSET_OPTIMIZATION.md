# Asset-Specific Optimization Guide

**Last Updated:** January 8, 2026  
**Version:** 2.0 - Synthesized from Multi-Expert Analysis

This guide documents asset-specific optimizations for BTC, ETH, and SOL trading on Polymarket's 15-minute crypto markets. The recommendations synthesize insights from multiple expert analyses to create a balanced, practical approach.

---

## 🎯 Executive Summary

**The Core Problem:** BTC and ETH aren't failing because of bad signals—they're failing because the bot treats them like SOL. Each asset has a unique market microstructure that requires tailored parameters.

| Asset | Problem | Solution | Expected Impact |
|-------|---------|----------|-----------------|
| **BTC** | MMs reprice in 4-8s (too fast for current 90s window) | Shorter exposure, higher quality bar | Win rate ↑15%, trades ↓40% |
| **ETH** | Low volatility makes current thresholds too "stiff" | Higher sensitivity, lower liquidity floor | Unlock 15-25 trades/day |
| **SOL** | Working well | Keep winning config, extend time limit | Maintain 60%+ win rate |

---

## 📊 The "Efficiency Gap" Explained

### Why SOL Outperforms BTC/ETH

SOL's edge comes from the **market maker efficiency gap**:

| Metric | BTC | ETH | SOL |
|--------|-----|-----|-----|
| MM Repricing Speed | 4-8 seconds | 6-12 seconds | 10-15 seconds |
| Typical Spread | 0.5-1% | 1-2% | 2-4% |
| Liquidity Depth | Deep | Medium | Thin |
| Mispricing "Stickiness" | Low | Medium | High |
| Current Win Rate | ~45% | ~40% | ~60% |

**Key Insight:** BTC's sophisticated MMs reprice before our 8-12s signal window closes. SOL's thinner liquidity creates "stickier" mispricings that last long enough to exploit.

---

## 🔧 Optimized Configuration

### BTC: "Scalpel" Strategy (PRIORITY)

BTC requires surgical precision—high quality signals with fast exits.

```python
BTC_CONFIG = {
    # Signal Detection
    "min_divergence_pct": 0.085,      # 8.5% (↑ from 7%) - quality over quantity
    "spot_implied_scale": 100,         # Keep standard scale
    "min_liquidity_eur": 50.0,         # Keep current (good balance)
    
    # Execution - CRITICAL CHANGES
    "time_limit_s": 60,                # ↓ from 90s - exit before MM reprices
    "take_profit_pct": 0.06,           # ↓ from 8% - capture first repricing
    "stop_loss_eur": 0.025,            # ↓ from €0.03 - tighter stops for tight spreads
    
    # Staleness Window
    "optimal_staleness_min": 4,        # 4-10s (not 8-12s)
    "optimal_staleness_max": 10,
    
    # Price Range
    "min_price": 0.05,
    "max_price": 0.95,
}
```

**Why This Works:**
1. **Higher divergence (8.5%)**: Only trade "Massive Lag" events where MM is significantly behind
2. **Shorter time limit (60s)**: BTC doesn't trend—it snaps. Exit before MMs overcorrect
3. **Smaller TP (6%)**: BTC wins are fast & small. Capture first repricing, don't wait for 8%
4. **Tighter stop (€0.025)**: BTC spreads are narrow, so smaller adverse moves are significant

**Expected Results:**
- Trades: ↓40% (quality filter)
- Win Rate: ↑15% (55% → 70%)
- Avg Win: ~5-6% (vs current 4%)

---

### ETH: "Sensitivity" Strategy

ETH needs higher sensitivity to catch moves during calm periods.

```python
ETH_CONFIG = {
    # Signal Detection - MORE SENSITIVE
    "min_divergence_pct": 0.065,       # ↓ from 7% - catch smaller divergences
    "spot_implied_scale": 130,         # ↑ from 100 - more sensitive to small moves
    "min_liquidity_eur": 30.0,         # ↓ from 40€ - unlock thinner but profitable setups
    
    # Execution
    "time_limit_s": 90,                # Keep standard (ETH MMs lag ~1s longer than BTC)
    "take_profit_pct": 0.06,           # ↓ from 8% - ETH doesn't overshoot like SOL
    "stop_loss_eur": 0.03,             # Keep current
    
    # Volatility Adjustment
    "volatility_scale_enabled": True,   # Scale thresholds by ATR
    "volatility_scale_factor": 1.3,     # 30% boost to sensitivity during calm periods
    
    # Staleness Window
    "optimal_staleness_min": 8,
    "optimal_staleness_max": 15,
    
    # Price Range
    "min_price": 0.08,
    "max_price": 0.92,
}
```

**Why This Works:**
1. **Lower divergence (6.5%)**: ETH's smaller moves are more significant than SOL's larger ones
2. **Higher sigmoid scale (130)**: Makes `spot_implied_prob` more reactive to 0.3-0.5% moves
3. **Lower liquidity (€30)**: ETH often trades at €35-45 depth during low vol—catch these
4. **Volatility scaling**: During calm periods, boost sensitivity to catch dormant opportunities

**Volatility Scaling Logic:**
```python
# In signal detection for ETH
atr_30s = calculate_atr(prices[-30:])
base_atr = 0.002  # 0.2% typical for ETH

if asset == "ETH":
    volatility_ratio = atr_30s / base_atr
    if volatility_ratio < 0.8:  # Calm period
        # Boost sensitivity (lower threshold, higher scale)
        effective_divergence = divergence * 1.3  # 30% boost
    else:
        effective_divergence = divergence
```

**Expected Results:**
- Trades: 0 → 15-25/day (unlocked)
- Win Rate: 50-60%
- Previously dormant asset now active

---

### SOL: "Momentum" Strategy

SOL is working—don't fix what isn't broken. Just optimize for bigger wins.

```python
SOL_CONFIG = {
    # Signal Detection - KEEP PROVEN SETTINGS
    "min_divergence_pct": 0.08,        # Keep 8% - proven profitable
    "spot_implied_scale": 100,          # Keep standard
    "min_liquidity_eur": 30.0,          # Keep current
    
    # Execution - LET IT BREATHE
    "time_limit_s": 120,               # ↑ from 90s - SOL trends, let momentum play
    "take_profit_pct": 0.09,           # ↑ from 8% - SOL overshoots, capture more
    "stop_loss_eur": 0.03,             # Keep current
    
    # Staleness Window
    "optimal_staleness_min": 8,
    "optimal_staleness_max": 12,
    
    # Price Range
    "min_price": 0.10,
    "max_price": 0.90,
}
```

**Why This Works:**
1. **Keep divergence (8%)**: This is the proven sweet spot for SOL
2. **Longer time limit (120s)**: SOL trends inside PM markets. Let momentum play out
3. **Higher TP (9%)**: SOL overshoots—capture the extra 1%

**Expected Results:**
- Win Rate: Maintain 60%+
- Avg Win: ↑ slightly (9% vs 8% TP)
- SOL remains the profit engine

---

## 📈 Complete Parameter Summary

| Parameter | BTC | ETH | SOL | Notes |
|-----------|-----|-----|-----|-------|
| **min_divergence_pct** | **8.5%** | **6.5%** | 8% | BTC quality, ETH sensitivity |
| **spot_implied_scale** | 100 | **130** | 100 | ETH gets volatility boost |
| **min_liquidity_eur** | €50 | **€30** | €30 | Lower floor for ETH/SOL |
| **time_limit_s** | **60s** | 90s | **120s** | BTC fast, SOL slow |
| **take_profit_pct** | **6%** | **6%** | **9%** | BTC/ETH lower, SOL higher |
| **stop_loss_eur** | **€0.025** | €0.03 | €0.03 | BTC tighter |
| **staleness_min** | **4s** | 8s | 8s | BTC MMs are faster |
| **staleness_max** | **10s** | 15s | 12s | Asset-specific windows |
| **Execution Mode** | Maker-Only | Hybrid | Hybrid | BTC needs maker rebates |

---

## 🔬 Advanced Optimizations

### 1. Repricing Speed Filter (High Impact)

Track how fast MMs respond per asset:

```python
class RepricingSpeedTracker:
    """Track time from spot move to first PM tick."""
    
    def __init__(self):
        self.speed_by_asset = {"BTC": [], "ETH": [], "SOL": []}
    
    def record(self, asset: str, spot_move_time: int, pm_reprice_time: int):
        speed = pm_reprice_time - spot_move_time
        self.speed_by_asset[asset].append(speed)
    
    def get_avg_speed(self, asset: str) -> float:
        speeds = self.speed_by_asset[asset][-100:]  # Last 100 samples
        return sum(speeds) / len(speeds) if speeds else 8.0

# Usage in signal detection
repricing_speed = tracker.get_avg_speed(asset)
if repricing_speed < 6:  # Very fast MM
    min_divergence *= 1.15  # Require higher quality
    time_limit *= 0.7       # Exit faster
```

### 2. Partial Snap Take-Profit (Prevents Giving Back Wins)

Instead of waiting for full TP, take profits at halfway point:

```python
# Current: Wait for 8% then exit 100%
# NEW: Take 50% at 4%, let remainder run to 8%

async def manage_position_with_partial_tp(position, current_pnl):
    if current_pnl >= 0.04 and not position.partial_taken:
        # Take 50% profit immediately
        await close_partial(position, 0.5)
        position.partial_taken = True
    
    if current_pnl >= position.take_profit_pct:
        await close_remaining(position)
```

**Why This Matters for BTC/ETH:**
- BTC often moves 3-5% then stalls, then reverts
- Current strategy waits for 8% and gives back the 4-5% gain
- Partial TP locks in guaranteed profit

### 3. Fee-Aware BTC Filtering (Critical for Profitability)

BTC at 50% odds = 1.6-3% taker fee. This destroys edge.

```python
def check_btc_fee_viability(entry_price: float, divergence: float) -> bool:
    """Reject BTC trades with unfavorable fees."""
    
    # Calculate effective taker fee
    taker_fee = calculate_taker_fee(entry_price)
    
    # For BTC specifically: require divergence > 3x fee
    # (Stricter than other assets due to faster MM repricing)
    if asset == "BTC":
        if taker_fee > 0.0125:  # >1.25% fee
            if divergence < 0.15:  # <15% divergence
                return False  # Reject - edge doesn't justify fee
    
    return True
```

### 4. Volume Z-Score Surge Detection

Re-enable volume filter using Z-scores instead of fixed multipliers:

```python
def calculate_volume_zscore(current_volume: float, volumes_5min: list) -> float:
    """
    Z-score based volume surge detection.
    More robust than fixed multipliers.
    """
    if len(volumes_5min) < 10:
        return 0.0
    
    mean = sum(volumes_5min) / len(volumes_5min)
    std = (sum((v - mean) ** 2 for v in volumes_5min) / len(volumes_5min)) ** 0.5
    
    if std == 0:
        return 0.0
    
    return (current_volume - mean) / std

# Usage
volume_zscore = calculate_volume_zscore(current_volume, volumes_5min)
if volume_zscore > 2.0:  # 2 std devs above mean
    confidence_boost = 0.05  # +5% confidence
```

---

## 🚀 Implementation Checklist

### Phase 1: Config Updates (Day 1)

- [ ] Update `config/settings.py` with new asset-specific parameters
- [ ] Add `spot_implied_scale` per asset
- [ ] Add execution parameters (`time_limit_s`, `take_profit_pct`, `stop_loss_eur`)
- [ ] Implement volatility scaling flag for ETH

### Phase 2: Signal Detection (Day 2)

- [ ] Modify `signal_detector.py` to use asset-specific thresholds
- [ ] Add volatility scaling for ETH
- [ ] Implement repricing speed tracking
- [ ] Add fee-aware filtering for BTC

### Phase 3: Execution (Day 3)

- [ ] Update `execution.py` with asset-specific exit parameters
- [ ] Implement partial take-profit logic
- [ ] Add per-asset stop loss handling
- [ ] Enable Maker-Only mode for BTC

### Phase 4: Testing (Day 4-7)

- [ ] Run shadow mode for 24+ hours with new config
- [ ] Track win rates by asset
- [ ] Compare fill rates and P&L
- [ ] Adjust parameters based on results

---

## 📊 Metrics to Track

### Per-Asset Metrics

| Metric | BTC Target | ETH Target | SOL Target |
|--------|------------|------------|------------|
| Win Rate | 65-70% | 55-60% | 60-65% |
| Trades/Day | 5-10 | 15-25 | 10-20 |
| Avg Win | 5-6% | 5-6% | 7-9% |
| Avg Loss | 2-3% | 2-3% | 2-3% |
| Time in Trade | 30-45s | 45-75s | 60-100s |

### New Metrics to Add

```python
# Track these for optimization feedback
metrics = {
    "repricing_speed_ms": [],        # Time from spot move to PM tick
    "divergence_at_exit": [],        # Was edge still there at exit?
    "partial_tp_rate": [],           # How often do we hit 4% before 8%?
    "fee_drag_pct": [],              # Fees as % of gross P&L
    "maker_fill_rate": [],           # BTC maker order success rate
}
```

---

## ⚠️ Risk Management Updates

### BTC-Specific Risks

1. **Fast MM Repricing**: BTC MMs are sophisticated. If signals don't fill in 3.5s, abort
2. **Tight Spreads**: Smaller adverse moves are significant. Tighter stops required
3. **High Fee Zone**: Avoid 45-55% odds entries where fees peak at 1.6-3%

### ETH-Specific Risks

1. **Low Volatility**: During calm periods, watch for false signals
2. **Volume Confirmation**: Require volume surge to confirm dormant setups
3. **Thin Liquidity**: At €30 minimum, monitor for liquidity collapse

### SOL-Specific Risks

1. **Momentum Reversals**: Longer positions = more exposure to reversals
2. **Liquidity Gaps**: Thin books can gap against position
3. **Wide Spreads**: Entry/exit slippage can eat profits

---

## 🔄 Fallback Strategy

If new config underperforms after 7 days:

1. **BTC**: Revert to 8% divergence, 90s time limit
2. **ETH**: Revert to 7% divergence, disable volatility scaling
3. **SOL**: Keep 8% divergence (proven)

**Red Flags to Watch:**
- Win rate drops below 45% for any asset
- Trades/day exceeds 50 (overtrading)
- Consecutive 5+ losses on single asset

---

## 📚 References

- [System Documentation](./SYSTEM_DOCUMENTATION.md) - Full v1.2 architecture
- [VPS Setup Guide](./VPS_SETUP.md) - Deployment instructions
- [Polymarket Fee Structure](https://docs.polymarket.com) - Jan 2026 taker fee update
- [py-clob-client](https://github.com/Polymarket/py-clob-client) - Maker order implementation

---

**Next Steps:** 
1. Apply config changes to `config/settings.py`
2. Run 24h shadow test with new parameters
3. Compare metrics against baseline
4. Iterate based on results

