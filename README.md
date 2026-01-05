# Polymarket Oracle-Lag Trading Bot

A sophisticated trading bot that exploits the delay between real-time cryptocurrency spot prices and Chainlink oracle updates on Polymarket's 15-minute up/down prediction markets.

## 🎯 Strategy Overview

This bot identifies and trades mispricing windows that occur when:
1. **Spot prices move significantly** (>0.7% in 30 seconds)
2. **Oracle data is stale** (6-75 seconds old)
3. **Polymarket odds haven't adjusted** to the new price reality

The typical edge window is **30-90 seconds** before the Chainlink oracle updates and market makers reprice.

## 🏗️ Architecture

```
Multi-Exchange Spot Feed (Binance + Coinbase + Kraken WebSocket)
    ↓
Spot Consensus Engine (weighted avg, outlier rejection)
    ↓
Oracle Age Monitor (Chainlink on-chain feed tracking)
    ↓
Polymarket Orderbook Monitor (WebSocket, 500ms snapshots)
    ↓
Signal Detection + Validation Engine
    ↓
Confidence Scoring System v2
    ↓
Mode Router (Shadow / Alert / Night Auto)
    ↓
Execution Engine (nonce mgmt, gas optimization)
    ↓
Comprehensive Logging System
```

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Polygon RPC endpoint (Alchemy or Ankr recommended)
- Discord webhook (for alerts)

### Installation

```bash
# Clone the repository
cd polymarket-oracle-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your configuration
```

### Configuration

Edit `.env` with your settings:

```env
# Operating Mode: shadow, alert, night_auto
MODE=shadow

# Polygon RPC (required)
CHAINLINK__POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/your-api-key

# Wallet (required for night_auto mode)
WALLET_ADDRESS=0x...
PRIVATE_KEY=...

# Discord Alerts
ALERTS__DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Polymarket Market IDs
POLYMARKET__BTC_UP_MARKET_ID=...
```

### Running

```bash
# Start in shadow mode (recommended for first 2-4 weeks)
python -m src.main
```

## 📊 Operating Modes

### 🟡 Shadow Mode (Default)
- Simulates all trades without real execution
- Collects comprehensive logs
- Tracks would-be performance
- **Run for 2-4 weeks before live trading**

### 🔔 Alert Mode
- Sends Discord notifications for high-confidence signals (≥70%)
- Human decides whether to trade
- Good for learning the system

### 🌙 Night Auto Mode
- Fully automated conservative trading
- Active: 02:00-06:00 local time
- Strict requirements:
  - ≥85% confidence
  - €20 max position size
  - Max 2 trades per night
  - €40 daily loss limit

## 🔍 Signal Detection

A signal is generated when **ALL** conditions are met:

| Condition | Threshold |
|-----------|-----------|
| Spot move (30s) | > max(0.7%, 1.5 × 5min ATR) |
| Volume surge | > 2× 5-minute average |
| Spike concentration | > 60% of move in 10s window |
| Oracle age | 6-75s (regime-dependent) |
| Mispricing | > 3% odds misalignment |
| Liquidity | > €50 at best bid |
| Volatility filter | < 0.5% (anti-chop) |

### Escape Clause
For moves between 0.8% and threshold, signals are allowed if:
- Oracle age ≥ 15s
- Orderbook imbalance > 20%
- Liquidity ≥ €75
- Volume surge > 2.5×

## 📈 Confidence Scoring

| Component | Weight |
|-----------|--------|
| Oracle Age | 0.35 |
| Consensus Strength | 0.25 |
| Odds Misalignment | 0.15 |
| Liquidity | 0.10 |
| Spread Anomaly | 0.08 |
| Volume Surge | 0.04 |
| Spike Concentration | 0.03 |

## 📁 Project Structure

```
polymarket-oracle-bot/
├── config/
│   └── settings.py          # Configuration management
├── src/
│   ├── feeds/
│   │   ├── binance.py       # Binance WebSocket
│   │   ├── coinbase.py      # Coinbase WebSocket
│   │   ├── kraken.py        # Kraken WebSocket
│   │   ├── chainlink.py     # Chainlink oracle monitor
│   │   └── polymarket.py    # Polymarket orderbook
│   ├── engine/
│   │   ├── consensus.py     # Multi-exchange aggregation
│   │   ├── signal_detector.py
│   │   ├── validator.py
│   │   ├── confidence.py
│   │   └── execution.py     # Trade execution
│   ├── modes/
│   │   ├── shadow.py
│   │   ├── alert.py
│   │   └── night_auto.py
│   ├── utils/
│   │   ├── logging.py
│   │   └── alerts.py        # Discord integration
│   ├── models/
│   │   └── schemas.py       # Data models
│   └── main.py              # Main application
├── logs/                    # Signal and metrics logs
├── requirements.txt
└── .env.example
```

## 📋 Success Criteria

Before moving to live trading, ensure:

- [ ] 100+ shadow signals logged
- [ ] Would-be win rate ≥ 65%
- [ ] Would-be avg profit ≥ €1.50/trade after gas
- [ ] Oracle timing patterns understood
- [ ] Signal density: 5-15 per day on BTC
- [ ] E2E latency consistently < 200ms

## ⚠️ Risk Management

### Circuit Breakers
- Daily loss limit: €40
- Max 3 consecutive failed fills
- Max €10 daily gas spend
- Automatic pause on errors

### Position Limits
- 0.5% of bankroll per trade
- Max 1 concurrent position
- 5% daily exposure limit

## 🔧 Development

```bash
# Run tests
pytest tests/

# Format code
black src/
ruff check src/

# Type checking
mypy src/
```

## 📝 Logging

Signals are logged to `logs/signals_YYYY-MM-DD.jsonl` with full details:
- Spot data from all exchanges
- Oracle state and age
- Polymarket orderbook snapshot
- Confidence breakdown
- Validation results
- Action taken
- Outcome (if traded)

## ⚡ Performance Targets

| Metric | Target |
|--------|--------|
| Win Rate | ≥ 65% |
| Avg Profit/Trade | ≥ €1.50 |
| E2E Latency | < 200ms |
| Signals/Day | 5-15 |

## 🛑 When to Stop

If after 50 shadow trades:
- Would-be profit < €30 after gas
- Win rate < 55%
- Avg profit/trade < €1.00

**Then this edge doesn't exist for you.** The market may have changed or competition is too fierce.

## 📜 Disclaimer

This software is for educational purposes only. Trading cryptocurrency derivatives carries significant risk. You may lose some or all of your investment. Past performance does not guarantee future results. Understand the risks before trading.

## 📄 License

MIT License - See LICENSE file for details.

