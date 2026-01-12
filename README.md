# Polymarket Trading Bot - Post-Mortem

## ⚠️ Status: EXPERIMENTAL - Real Trading Disabled

This bot was an attempt to exploit market maker lag on Polymarket's 15-minute crypto up/down markets. **It did not work as intended and resulted in losses.**

---

## 📊 Results Summary

| Metric | Value |
|--------|-------|
| Total Trades | 56 |
| Money Spent | ~$272 |
| Money Lost | ~$260 |
| Final Balance | $9.15 |
| Win Rate | Below 50% |
| Time Invested | ~1 week |

---

## 🎯 Original Strategy

### The Thesis
1. **Spot price moves** on centralized exchanges (Binance, Kraken, Coinbase)
2. **Polymarket odds lag** behind by 8-12 seconds (market maker delay)
3. **Detect divergence** between spot-implied probability and PM odds
4. **Bet before MMs reprice** → profit from information edge

### How It Worked
```
Spot Price Moves → Calculate Implied Probability → Compare to PM Odds → Trade if Divergence > 5%
```

### The Markets
- 15-minute ETH/BTC/SOL up/down markets on Polymarket
- Binary outcome: UP (price ends higher than start) or DOWN
- Settlement via Chainlink Data Streams

---

## ❌ Why It Failed

### 1. No Real Edge
| Assumption | Reality |
|------------|---------|
| "MMs are slow (8-12s)" | Polymarket uses Chainlink Data Streams (~100ms). MMs have fast feeds too. |
| "30s momentum predicts outcome" | No evidence. Markets mean-revert. Spikes can reverse. |
| "Divergence = mispricing" | More likely: PM was correctly priced, we were wrong. |
| "We're outsmarting MMs" | We're retail. They're professionals with better infrastructure. |

### 2. Technical Issues (Secondary)
- Exit orders failed with "not enough balance/allowance" errors
- Markets settled before exits could complete
- Settlement detection was buggy (reported wins when positions lost)

### 3. Fundamental Flaw
The bot used **30-second price momentum** to predict direction, but 15-minute markets settle based on **window start vs window end price**. These are not the same thing.

---

## 🏗️ What Was Built

### Infrastructure (Working)
- Multi-exchange price feeds (Binance, Kraken, Coinbase)
- Polymarket CLOB integration (orders, positions, balances)
- Chainlink oracle monitoring
- Discord alerts
- Virtual trading simulation
- Real trading execution (maker orders with rebates)

### Key Files
```
src/
├── feeds/
│   ├── binance.py      # Binance WebSocket feed
│   ├── kraken.py       # Kraken WebSocket feed
│   ├── coinbase.py     # Coinbase WebSocket feed
│   ├── polymarket.py   # PM orderbook + market discovery
│   └── chainlink.py    # Oracle price + window tracking
├── engine/
│   ├── signal_detector.py  # Divergence detection
│   ├── consensus.py        # Multi-exchange price consensus
│   ├── confidence.py       # Signal scoring
│   └── validator.py        # Signal validation
├── trading/
│   ├── real_trader.py      # Real position management
│   └── maker_orders.py     # CLOB order execution
└── modes/
    ├── alert.py            # Alert mode with virtual/real trading
    └── virtual_trader.py   # Paper trading simulation
```

---

## 💡 Ideas That Might Actually Work

### 1. News/Event-Based Trading
- Political markets where human judgment matters
- Sports markets where you have domain expertise
- Breaking news before it's priced in

### 2. Longer Timeframe Markets
- Daily/weekly markets where speed doesn't matter
- Analysis-based edge rather than speed-based

### 3. Market Making
- Be the MM yourself, earn the spread
- Requires capital and risk management
- Polymarket offers rebates for makers

### 4. Less Competitive Markets
- Smaller, newer prediction markets
- Niche topics with less sophisticated participants

### 5. Proper Backtesting First
- Collect historical data
- Simulate strategy performance
- Only trade if backtests show positive edge

---

## 🔧 If You Want to Continue

### Before Trading Real Money Again:
1. **Backtest** - Prove strategy works on historical data
2. **Paper trade** - Run virtual-only for at least a week
3. **Small positions** - Never risk more than you can lose
4. **Clear edge** - Know exactly WHY you have an advantage

### Configuration
```bash
# .env file
REAL_TRADING_ENABLED=false  # Keep this OFF until proven
REAL_TRADING_POSITION_SIZE_EUR=5.0
REAL_TRADING_MAX_DAILY_LOSS_EUR=25.0
```

### Running Virtual Only
```bash
cd polybot
source venv/bin/activate
python -m src.main
# Watch logs - all trades will be virtual
```

---

## 📝 Lessons Learned

1. **Validate before deploying** - Paper trade first, always
2. **Question assumptions** - "MMs are slow" was wrong
3. **Check trade data early** - Would have caught losses sooner
4. **Professional MMs are good** - Hard to beat them at speed games
5. **Time has value** - A week of work is worth more than $300

---

## 🤝 Acknowledgments

Built with:
- [py-clob-client](https://github.com/Polymarket/py-clob-client) - Polymarket CLOB SDK
- [web3.py](https://github.com/ethereum/web3.py) - Ethereum interaction
- [structlog](https://www.structlog.org/) - Structured logging
- Various exchange WebSocket APIs

---

## 📄 License

MIT - Use at your own risk. This code lost money. You've been warned.
