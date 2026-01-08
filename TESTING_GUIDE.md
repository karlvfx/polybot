# Polybot Optimization Plan - Synthesized from Expert Analysis

**Last Updated:** January 8, 2026

This plan synthesizes recommendations from 3 different AI expert analyses, resolving conflicts and prioritizing by impact.

---

## 🔬 Expert Consensus

All 3 models agreed on these key points:

| Topic | Consensus |
|-------|-----------|
| **#1 Priority** | Maker strategy (not signals, not infra) |
| **Fee Impact** | Taker fees (up to 3%) are destroying profits |
| **Testing First** | Shadow/paper test before real capital |
| **Your Wins** | Z-score, freeze detection, per-asset configs = excellent |

---

## ⚔️ Conflict Resolution

| Topic | Model 1 | Model 2 | Model 3 | **Winner** |
|-------|---------|---------|---------|------------|
| **Wait time** | 10-15s | Not specified | **3.5s max** | 🏆 Model 3 |
| **Pricing** | 1-2% inside spread | Not specified | **Spread-relative (tick)** | 🏆 Model 3 |
| **Dual-region** | Priority | €4/mo forwarder | **Delay it** | 🏆 Model 3 |
| **Stale filter** | - | 15-20s | - | Consider tightening |

---

## 🎯 The Action Plan

### PHASE A: Maker Shadow Mode (Day 1-2)
*Test the strategy without risking capital*

**Goal:** Validate fill rates before committing real money

```python
# Shadow Maker Logic
async def shadow_maker_test(signal):
    """Log what WOULD happen with a maker order"""
    
    # 1. Calculate maker price (spread-relative, not %)
    best_bid, best_ask = get_pm_spread(signal.token_id)
    tick = 0.01  # Polymarket tick size
    maker_price = best_ask - tick  # Step inside spread minimally
    
    # 2. Log the hypothetical order
    log_entry = {
        "time": time.time(),
        "token_id": signal.token_id,
        "maker_price": maker_price,
        "direction": signal.direction,
        "divergence": signal.divergence,
    }
    
    # 3. Watch PM WebSocket for 3.5s (fast timeout)
    filled = await check_if_price_hit(maker_price, timeout=3.5)
    
    log_entry["would_have_filled"] = filled
    log_entry["time_to_fill"] = filled_time if filled else None
    
    save_shadow_log(log_entry)
```

**Success Metric:** Need **>40% fill rate** to proceed

---

### PHASE B: Real Maker Implementation (Day 3-5)
*After shadow testing proves viability*

**Critical Implementation Details:**

```python
# CORRECT Maker Order Placement
async def place_maker_order(token_id, signal):
    best_bid, best_ask = get_pm_spread(token_id)
    tick = 0.01
    
    # Step inside spread, but barely (spread-relative)
    maker_price = min(best_ask - tick, signal.target_price)
    
order = client.create_and_post_order(
    OrderArgs(
        token_id=token_id,
        price=maker_price,
        size=100,
        side=BUY,
    ),
    OrderType.GTC,
        post_only=True  # CRITICAL: Fail rather than become taker
    )
    
    # Pseudo-IOC behavior with 3.5s timeout (NOT 15s!)
    MAKER_TIMEOUT = 3.5  # seconds
    
    done, pending = await asyncio.wait(
        [
            wait_for_fill(order['orderID']),
            asyncio.sleep(MAKER_TIMEOUT),
            wait_for_divergence_collapse(signal),  # Cancel if edge gone
        ],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    # Cancel immediately if not filled
    if not is_filled(order['orderID']):
        await client.cancel(order['orderID'])
        # DON'T fallback to taker - just miss the trade
        return None
    
    return order
```

**Key Points:**
- `post_only=True` prevents accidental taker orders (and 3% fees)
- 3.5s timeout, NOT 10-15s (edge is gone by then)
- Cancel if divergence collapses (don't sit on stale orders)
- Don't fallback to taker - missed maker trade is better than paying fees

---

### PHASE C: Refinements (Week 2)

#### C.1 MM Pullback Detector
Detect when MMs are about to reprice:

```python
# Cancel burst detection
if cancels_per_second > threshold and price_static:
    repricing_imminent = True
    # Use smaller size, faster cancel
```

#### C.2 Time-to-Close Multiplier
Near close = faster MM repricing = better fills:

```python
if time_to_close < 180:  # 3 minutes to market close
    confidence *= 1.15
```

#### C.3 Tighten Stale Filter
Change from 60s to 20s for ghost liquidity detection:

```python
STALE_BOOK_THRESHOLD = 20  # was 60
```

---

### PHASE D: Optional Enhancements (Month 2+)

| Item | Priority | Cost | Notes |
|------|----------|------|-------|
| Pyth Network | Low | Free | Adds redundancy, 1 hour setup |
| Binance Forwarder | Low | €4/mo | EU VPS → UDP to US, only if needed |
| Dual-region mesh | Low | $10/mo | Delay until connection issues return |

---

## 📊 Metrics to Track

### OLD (Wrong Focus)
- ❌ Win rate (targeting 60%)

### NEW (Correct Focus)
- ✅ **EV per trade** = (avg_win × win_rate) - (avg_loss × loss_rate)
- ✅ **Maker capture rate** = maker_fills / (maker_fills + missed)
- ✅ **Time to fill** distribution
- ✅ **Fee savings** = taker_fees_avoided per day

**Why EV matters more than win rate:**
With maker rebates, you can be profitable at 45% win rate. What matters is:
- Avg win size (bigger with maker pricing)
- Fee drag (eliminated with maker)
- Trade quality (better entries)

---

## ⚡ Immediate Actions Checklist

### Today
- [ ] Enable ETH, SOL on VPS: `ASSETS=BTC,ETH,SOL`
- [ ] Pull latest code: `git pull && sudo systemctl restart polybot`
- [ ] Monitor virtual trades for 4+ hours

### Day 2
- [ ] Implement shadow maker logging
- [ ] Collect 50+ shadow samples

### Day 3
- [ ] Analyze shadow logs
- [ ] If fill rate >40% → proceed to real maker

### Day 4-5
- [ ] Implement py-clob-client integration
- [ ] Test maker orders in shadow mode
- [ ] Verify `post_only` prevents taker fills

### Week 2
- [ ] Add cancel burst detection
- [ ] Add time-to-close multiplier
- [ ] Tighten stale book filter to 20s

---

## 🔧 Technical Requirements

### py-clob-client Installation
```bash
pip install py-clob-client
```

### Required for Maker Orders
- Polygon wallet with private key
- Small amount of MATIC for gas (~$1)
- USDC balance for trading

### Order Signing Note
EIP-712 signing can take 200-800ms in Python. Mitigate by:
- Pre-calculating order structures where possible
- Running signing in separate worker if needed

---

## 📈 Expected Results

### Current State (Taker Mode)
- Fees: 1.6-3% per trade
- Profit per €20 trade: ~€0.30
- Break-even win rate: ~55%

### After Maker Strategy
- Fees: 0% (+ rebates!)
- Profit per €20 trade: ~€1.50+
- Break-even win rate: ~40%
- **5x profit improvement**

---

## ⚠️ Risk Management

1. **Never chase fills** - If maker doesn't fill in 3.5s, let it go
2. **post_only is mandatory** - One accidental taker = wipes out 5 maker rebates
3. **Shadow test first** - Don't risk capital until fill rate proven
4. **Start small** - €10-20 positions until strategy validated

---

## 📚 References

- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Polymarket Order API Docs](https://docs.polymarket.com/developers/CLOB/orders/create-order)
- [CLOB Introduction](https://docs.polymarket.com/developers/CLOB/introduction)

---

**Next Step:** Switch to Agent mode to implement Shadow Maker testing.
