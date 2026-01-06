# Testing Guide - Critical Improvements

## ✅ Validation Results

All 11 critical improvements have been validated:

1. ✓ Volume tracking with 5-minute rolling average
2. ✓ Exchange agreement scoring (0-1 scale)
3. ✓ Volume authentication (surge detection)
4. ✓ Spike concentration (anti-drift filter)
5. ✓ Liquidity collapse detection
6. ✓ Escape clause for sub-threshold moves
7. ✓ Enhanced configuration settings
8. ✓ Comprehensive logging

## 🧪 Quick Validation

Run the validation script to verify all improvements:

```bash
python3 validate_improvements.py
```

## 🚀 Running in Shadow Mode

### Prerequisites

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create `.env` file with minimal configuration:
```env
# Operating Mode
MODE=shadow

# Polygon RPC (for Chainlink oracle)
CHAINLINK__POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Optional: Discord alerts
ALERTS__DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional: Polymarket market IDs (if you have them)
POLYMARKET__BTC_UP_MARKET_ID=...
POLYMARKET__BTC_DOWN_MARKET_ID=...
```

### Start the Bot

```bash
python3 -m src.main
```

The bot will:
- Connect to Binance, Coinbase, and Kraken WebSocket feeds
- Monitor Chainlink oracle on Polygon
- Monitor Polymarket orderbooks (if market IDs configured)
- Detect signals with the new multi-layer validation
- Log all signals to `logs/signals_YYYY-MM-DD.jsonl`

### What to Monitor

1. **Signal Quality**: Check logs for rejection reasons
   - Most rejections should be due to volume/surge filters
   - Spike concentration should filter out smooth drifts
   - Agreement score should ensure consensus quality

2. **Signal Density**: Target 5-15 signals per day
   - Too many = thresholds too low
   - Too few = thresholds too high

3. **Win Rate**: Target ≥65% in shadow mode
   - Track in shadow mode performance report
   - Review after 50+ signals

4. **Filter Effectiveness**: Check rejection reasons
   ```bash
   # Count rejection reasons
   grep -o '"reason":"[^"]*"' logs/signals_*.jsonl | sort | uniq -c
   ```

## 📊 Key Metrics to Track

### Volume Surge
- Should see 2x+ volume surge on valid signals
- Rejections with `VOLUME_LOW` indicate filter working

### Spike Concentration
- Valid signals: 60%+ of move in 10s window
- Rejections with `SMOOTH_DRIFT` indicate filter working

### Agreement Score
- Valid signals: ≥0.85 agreement score
- Rejections with `CONSENSUS_FAILURE` may indicate low agreement

### Oracle Timing
- Low vol regime: 12-75s window
- Normal vol regime: 6-75s window
- Rejections with `ORACLE_TOO_FRESH` or `ORACLE_TOO_STALE` indicate timing filter

### Liquidity Collapse
- Rejections with `LIQUIDITY_COLLAPSING` indicate protection working
- Check logs for liquidity drop percentages

## 🔍 Analyzing Logs

### View Recent Signals
```bash
tail -f logs/signals_$(date +%Y-%m-%d).jsonl | jq .
```

### Count Signals by Type
```bash
jq -r '.signal_type' logs/signals_*.jsonl | sort | uniq -c
```

### Analyze Volume Surge Distribution
```bash
jq -r '.spot_data.volume_surge_ratio' logs/signals_*.jsonl | \
  awk '{sum+=$1; count++} END {print "Avg:", sum/count}'
```

### Check Agreement Scores
```bash
jq -r '.spot_data.agreement_score' logs/signals_*.jsonl | \
  awk '{sum+=$1; count++} END {print "Avg:", sum/count}'
```

## ⚙️ Tuning Thresholds

If signals are too frequent/rare, adjust in `config/settings.py`:

```python
class SignalSettings:
    # Volume surge threshold (default: 2.0)
    volume_surge_threshold: float = 2.0  # Increase to reduce signals
    
    # Spike concentration (default: 0.60)
    spike_concentration_threshold: float = 0.60  # Increase to reduce signals
    
    # Agreement score (default: 0.85)
    min_agreement_score: float = 0.85  # Increase to reduce signals
```

## 🎯 Success Criteria

After 2-4 weeks of shadow mode:

- [ ] 100+ signals logged
- [ ] Win rate ≥ 65%
- [ ] Avg profit ≥ €1.50/trade (after gas)
- [ ] Signal density: 5-15/day
- [ ] E2E latency < 200ms consistently

## 🐛 Troubleshooting

### No Signals Generated
- Check feed connections (should see "Connected" logs)
- Verify oracle age is in valid window
- Check if all filters are too strict

### Too Many Rejections
- Review rejection reasons in logs
- May need to adjust thresholds
- Check if market conditions are unusual

### Feed Connection Issues
- Verify WebSocket URLs are correct
- Check network connectivity
- Review feed health logs

## 📝 Next Steps

1. Run in shadow mode for 2-4 weeks
2. Collect 100+ signals
3. Analyze win rate and profit metrics
4. Tune thresholds based on data
5. Move to alert mode for manual review
6. Finally, enable night_auto mode if criteria met

