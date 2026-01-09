# Sports Arbitrage Bot for Polymarket

A sports betting arbitrage system that exploits price discrepancies between **sharp sportsbooks** (Pinnacle, Betfair) and **Polymarket** prediction markets.

## Why Sports? (The Crypto Bot Problem)

As of January 2026, Polymarket introduced **dynamic taker fees up to 3%+** on 15-minute crypto markets. This killed the crypto latency arbitrage strategy:

| Metric | Crypto (Now) | Sports |
|--------|-------------|--------|
| Taker Fee | 1.6-3%+ | **0%** |
| Min Viable Edge | 8%+ | **0.5%** |
| Market Liquidity | ~$44k avg | **$1.32M avg** |
| Signal Window | 8-12s | **10-60s** |
| Competition | High (bots) | Low (different skillset) |

## Architecture

```
src/sports/
├── __init__.py           # Module entry
├── main.py               # Main bot entry point
├── config.py             # Sports-specific settings
├── feeds/
│   ├── odds_api.py       # The Odds API (40+ books, free tier)
│   └── pinnacle.py       # Direct Pinnacle API (skeleton)
├── engine/
│   ├── signal_detector.py # Sharp-PM divergence detection
│   └── confidence.py      # Signal scoring
├── discovery/
│   └── polymarket.py      # Find/match PM sports markets
├── models/
│   └── schemas.py         # Sports data structures
└── README.md              # This file
```

## Quick Start

### 1. Get The Odds API Key (Free)

1. Go to [the-odds-api.com](https://the-odds-api.com/)
2. Sign up for free tier (500 requests/month)
3. Copy your API key

### 2. Configure Environment

```bash
# Required
export ODDS_API_KEY="your_api_key_here"

# Optional
export SPORTS_MODE="shadow"                    # shadow|alert|live
export SPORTS_DISCORD_WEBHOOK="https://..."    # For alerts
export SPORTS_MIN_DIVERGENCE="0.005"           # 0.5% minimum edge
export SPORTS_MIN_LIQUIDITY="100"              # $100 minimum PM liquidity
```

### 3. Run

```bash
# From project root
python -m src.sports.main
```

## How It Works

### 1. Sharp Books as "Truth"

Sharp sportsbooks (Pinnacle, Betfair) have the most accurate odds because:
- They accept high-volume professional bettors
- They don't limit winners
- Their lines move first (others follow)

### 2. Polymarket Lag

Polymarket sports markets often lag behind sharp books by **10-60 seconds**:
- Market makers are slower to update
- Less sophisticated participants
- Lower liquidity = slower price discovery

### 3. The Edge

```
Sharp Prob = 55% (Pinnacle says Team A has 55% chance)
PM Price   = 52% (Polymarket shows 52¢ for Team A)
Divergence = 3%  (You can buy 55% value for 52¢)

With 0% taker fees, this is a +3% edge!
```

### 4. Signal Detection

```python
# Pseudocode
if divergence >= 0.5% and pm_staleness >= 5s:
    if pm_liquidity >= $100 and time_to_event >= 5min:
        SIGNAL_DETECTED
```

## Configuration

### Signal Thresholds

| Setting | Default | Description |
|---------|---------|-------------|
| `min_divergence_pct` | 0.5% | Minimum edge to signal |
| `high_divergence_pct` | 2.0% | High-confidence threshold |
| `min_pm_staleness_seconds` | 5s | PM must be at least this stale |
| `max_pm_staleness_seconds` | 300s | PM too stale = dead market |
| `min_time_to_event_seconds` | 300s | 5 min buffer (avoid court-siding) |

### Supported Sports

- 🏈 NFL (American Football)
- 🏀 NBA (Basketball)
- ⚽ EPL (English Premier League)
- 🏒 NHL (Ice Hockey)
- ⚾ MLB (Baseball)
- 🥊 UFC (MMA)

Add more in `config.py` → `OddsAPISettings.sports`

## Comparison to Crypto Bot

| Feature | Crypto Bot | Sports Bot |
|---------|-----------|------------|
| Data Source | Binance/Coinbase/Kraken | Pinnacle/Betfair |
| Signal Type | Spot-PM divergence | Sharp-PM divergence |
| Min Edge | 8% (fees eat small edges) | 0.5% (no fees!) |
| PM Discovery | btc-updown-15m-{ts} slug | Fuzzy team matching |
| Confidence Scoring | Same pattern | Same pattern |
| Virtual Trading | Same pattern | Same pattern |

## Risks

### 1. Court-siding (Live Betting)
People at stadiums transmit scores 2-3s before TV/API. **Avoid live markets.**

### 2. Model Risk (Far-out Events)
24+ hours before event = more uncertainty. **Focus on 5min - 6hr window.**

### 3. PM Market Matching
Fuzzy matching can be wrong. **Verify matches manually at first.**

### 4. Sharp Book Staleness
If sharp data is old, it's not useful. **Check last_update timestamps.**

## Roadmap

- [x] The Odds API feed
- [x] Sports signal detector
- [x] PM market discovery
- [x] Confidence scoring
- [ ] Virtual trading (port from crypto)
- [ ] Direct Pinnacle API (when access granted)
- [ ] Betfair Exchange API
- [ ] Live line movement alerts
- [ ] Backtesting module

## API Costs

| Service | Free Tier | Paid |
|---------|-----------|------|
| The Odds API | 500 req/mo | $20/mo (10k) |
| Pinnacle API | Requires account | Free with account |
| Betfair API | Free | Free (but needs account) |
| Polymarket | Free | Free |

## Files Reference

### `feeds/odds_api.py`
- Fetches odds from 40+ sportsbooks via The Odds API
- Prioritizes sharp books (Pinnacle, Betfair)
- Calculates vig and fair probabilities
- Tracks API quota usage

### `engine/signal_detector.py`
- Compares sharp odds to PM prices
- Applies validation filters (liquidity, timing, staleness)
- Generates signal candidates
- Mirrors crypto bot's divergence logic

### `discovery/polymarket.py`
- Searches PM for sports markets
- Fuzzy matches PM questions to sharp events
- Handles team name variations ("KC" → "Chiefs")
- Tracks matched markets

### `models/schemas.py`
- `SportEvent`: A game/match with timing and teams
- `SharpOdds`: Odds from a sharp book
- `Outcome`: A single betting line with conversions
- `SportSignalCandidate`: A detected arbitrage opportunity

## Example Output

```
🏈 Sports Bot Started
Mode: SHADOW
Sports: NFL, NBA, EPL
Min Divergence: 0.5%
Advantage: 0% taker fees! (vs 3% for crypto)

📊 Fetched 12 NFL events (497 API requests remaining)
📊 Discovered 8 PM sports markets
📊 Matched 5 markets to sharp events

🎯 SPORTS SIGNAL DETECTED
Event: Ravens @ Chiefs
Sport: NFL
Direction: YES (home)
Sharp Prob: 58.2% (Pinnacle)
PM Prob: 54.0%
Divergence: 4.2%
Confidence: ★★★★★ EXCELLENT
Time to Start: 45 min
```

## Contributing

This is a standalone module that can be extracted. The architecture mirrors the crypto bot for easy integration:

1. Feeds → Engine → Mode → Execution
2. Reuses `PolymarketFeed` from crypto bot
3. Reuses `DiscordAlerter` from crypto bot
4. Can share virtual trading infrastructure

