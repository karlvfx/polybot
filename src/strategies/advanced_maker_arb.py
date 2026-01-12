"""
Advanced Maker Arbitrage Strategy - Post Jan 6, 2026 Edition.

This strategy exploits the NEW Polymarket fee structure:
- 15-min markets: 3% taker fee (but dynamic based on price)
- 1-hour/Daily markets: ZERO taker fees (old sniper works!)
- Maker rebates: 100% of taker fees redistributed (decaying)

Key Edges:
1. Fee-Free 1-Hour Markets - Sniper mode viable
2. Dynamic Fee Extremes - Sniper at <$0.15 or >$0.85 prices
3. Dual-Sided Maker Bids - Earn rebates, guaranteed $1 on dual fill
4. Synthetic Depth Analysis - Exploit shared orderbook inefficiencies
5. Rebate Tracking - Estimate daily USDC earnings

Future Enhancements (require external accounts):
- Hyperliquid perp hedging for one-sided fills
- Kalshi cross-platform LP mirroring
"""

import asyncio
import time
import csv
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable
from pathlib import Path
from uuid import uuid4

import structlog

from src.models.schemas import ConsensusData, PolymarketData
from src.utils.alerts import DiscordAlerter

logger = structlog.get_logger()


@dataclass
class VirtualTradeLog:
    """Enhanced trade log with fee-aware tracking."""
    timestamp: datetime
    asset: str
    market_type: str  # "15m", "1h", "24h", "daily"
    strategy: str  # "sniper", "maker_dual", "extreme_sniper"
    
    # Prices
    yes_price: float
    no_price: float
    combined_cost: float
    
    # Fee calculation
    base_fee_pct: float  # 0.0315 for 15m, 0 for others
    dynamic_fee_pct: float  # Actual fee after price adjustment
    
    # Opportunity
    potential_gap_pct: float
    net_virtual_pnl: float
    
    # Fill tracking
    fill_type: str = "none"  # "both", "yes_only", "no_only", "none"
    
    # Rebate estimation
    maker_rebate_est: float = 0.0
    rebate_multiplier: float = 1.0  # 100% initially, may decay to 20%
    
    # Inventory tracking
    inventory_skew: float = 0.0  # Positive = more YES, Negative = more NO
    
    # Rejection/notes
    rejection_reason: str = ""
    notes: str = ""


@dataclass
class MakerPosition:
    """Tracks an open maker position."""
    position_id: str
    asset: str
    market_type: str
    
    # Orders
    yes_order_id: Optional[str] = None
    yes_bid_price: float = 0.0
    yes_size: float = 0.0
    yes_filled: bool = False
    
    no_order_id: Optional[str] = None
    no_bid_price: float = 0.0
    no_size: float = 0.0
    no_filled: bool = False
    
    # Status
    status: str = "pending"  # pending, partial, dual_fill, closed
    
    # P&L
    total_cost: float = 0.0
    realized_pnl: float = 0.0
    rebate_earned: float = 0.0
    
    # Timing
    opened_at_ms: int = 0
    closed_at_ms: int = 0


@dataclass
class DailyStats:
    """Daily statistics for rebate and P&L tracking."""
    date: str
    total_volume_provided: float = 0.0
    estimated_rebate_share: float = 0.0
    
    sniper_trades: int = 0
    sniper_pnl: float = 0.0
    
    maker_trades: int = 0
    maker_pnl: float = 0.0
    
    dual_fills: int = 0
    partial_fills: int = 0
    
    # By market type
    pnl_15m: float = 0.0
    pnl_1h: float = 0.0
    pnl_daily: float = 0.0


class AdvancedMakerArb:
    """
    Advanced Maker Arbitrage Strategy.
    
    Adapts to the Jan 2026 fee structure:
    - Uses Sniper mode on fee-free 1h/daily markets
    - Uses Maker mode on 15m markets for rebates
    - Exploits dynamic fee reduction at price extremes
    """
    
    # Fee structure (as of Jan 6, 2026)
    BASE_FEE_15M = 0.0315  # 3.15% at $0.50
    BASE_FEE_1H = 0.0  # Zero fees on 1-hour markets
    BASE_FEE_DAILY = 0.0  # Zero fees on daily markets
    
    # Thresholds
    MIN_ARB_GAP_PCT = 0.02  # 2% minimum gap for sniper
    MIN_DISCOUNT_PCT = 0.03  # 3% discount for maker dual-entry
    EXTREME_PRICE_THRESHOLD = 0.15  # <$0.15 or >$0.85 = low fees
    
    # Position sizing
    MAX_POSITION_USD = 50.0
    MAX_INVENTORY_SKEW = 0.20  # Max 20% imbalance before hedging
    
    def __init__(
        self,
        virtual_mode: bool = True,
        capital_usd: float = 500.0,
        log_dir: str = "logs/maker_arb",
        simulate_latency: bool = True,  # Add realistic delays
        simulate_fill_probability: bool = True,  # Simulate partial fills
        discord_alerter: Optional[DiscordAlerter] = None,  # Discord notifications
    ):
        self.logger = logger.bind(component="advanced_maker_arb")
        self._virtual_mode = virtual_mode
        self._capital_usd = capital_usd
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        
        # Discord alerter for notifications
        self._discord = discord_alerter
        
        # Realism settings for accurate virtual testing
        self._simulate_latency = simulate_latency
        self._simulate_fill_probability = simulate_fill_probability
        
        # Realistic parameters based on Polymarket behavior
        self._avg_latency_ms = 150  # Network + order processing
        self._maker_fill_rate = 0.35  # 35% of maker orders fill before price moves
        self._taker_fill_rate = 0.95  # 95% of taker orders fill (some fail due to liquidity)
        
        # State
        self._running = False
        self._positions: dict[str, MakerPosition] = {}
        self._trade_logs: list[VirtualTradeLog] = []
        self._daily_stats: dict[str, DailyStats] = {}
        
        # Inventory tracking (net exposure)
        self._inventory: dict[str, float] = {}  # asset -> net YES exposure
        
        # Price history for spike detection
        self._price_history: dict[str, list[tuple[int, float]]] = {}
        
        # Rebate tracking
        self._current_rebate_multiplier = 1.0  # 100% initially
        self._daily_volume_provided = 0.0
        
        # Callbacks
        self._on_opportunity: Optional[Callable] = None
        self._on_trade: Optional[Callable] = None
    
    def set_callbacks(
        self,
        on_opportunity: Optional[Callable] = None,
        on_trade: Optional[Callable] = None,
    ) -> None:
        """Set callbacks for events."""
        self._on_opportunity = on_opportunity
        self._on_trade = on_trade
    
    async def start(self) -> None:
        """Start the strategy."""
        self._running = True
        mode_str = "🧪 VIRTUAL" if self._virtual_mode else "💰 REAL"
        self.logger.info(
            f"🏦 Advanced Maker Arb started ({mode_str})",
            capital=f"${self._capital_usd:.2f}",
            rebate_multiplier=f"{self._current_rebate_multiplier:.0%}",
            simulate_latency=self._simulate_latency,
            simulate_fills=self._simulate_fill_probability,
            maker_fill_rate=f"{self._maker_fill_rate:.0%}",
            taker_fill_rate=f"{self._taker_fill_rate:.0%}",
        )
        
        if self._virtual_mode and self._simulate_fill_probability:
            self.logger.info(
                "📊 Realistic simulation enabled",
                note="Virtual P&L accounts for slippage, partial fills, and failed orders",
            )
    
    async def stop(self) -> None:
        """Stop and export logs."""
        self._running = False
        await self._export_trade_logs()
        self.logger.info(
            "Advanced Maker Arb stopped",
            total_trades=len(self._trade_logs),
            stats=self.get_summary(),
        )
    
    def calculate_dynamic_fee(self, price: float, market_type: str) -> float:
        """
        Calculate actual fee based on price and market type.
        
        Key insight: Fees are ZERO on 1h/daily markets.
        On 15m markets, fees scale with price distance from $0.50.
        """
        if "1h" in market_type or "1-hour" in market_type.lower():
            return 0.0
        if "24h" in market_type or "daily" in market_type.lower():
            return 0.0
        
        # 15-minute market: Dynamic fee
        # Max fee at $0.50, drops toward zero at extremes
        dist_from_center = abs(price - 0.5)
        
        # Fee formula: Fee = Max_Fee * (1 - (distance * 1.8))
        # At $0.50: fee = 3.15%
        # At $0.10: fee = 3.15% * (1 - 0.4*1.8) = 3.15% * 0.28 = 0.88%
        # At $0.05: fee = 3.15% * (1 - 0.45*1.8) = 3.15% * 0.19 = 0.60%
        dynamic_fee = self.BASE_FEE_15M * max(0.05, 1 - (dist_from_center * 1.8))
        
        return dynamic_fee
    
    def is_fee_free_market(self, market_type: str) -> bool:
        """Check if market has zero taker fees."""
        fee_free_types = ["1h", "1-hour", "hourly", "24h", "daily", "1d"]
        return any(ft in market_type.lower() for ft in fee_free_types)
    
    def is_extreme_price(self, price: float) -> bool:
        """Check if price is at extremes where fees are minimal."""
        return price < self.EXTREME_PRICE_THRESHOLD or price > (1 - self.EXTREME_PRICE_THRESHOLD)
    
    def detect_volatility_spike(self, asset: str, lookback_seconds: int = 30) -> Optional[float]:
        """Detect if there's been a significant price spike."""
        history = self._price_history.get(asset, [])
        if len(history) < 2:
            return None
        
        now_ms = int(time.time() * 1000)
        lookback_ms = lookback_seconds * 1000
        
        recent_prices = [
            (ts, p) for ts, p in history
            if now_ms - ts <= lookback_ms
        ]
        
        if len(recent_prices) < 2:
            return None
        
        oldest_price = recent_prices[0][1]
        newest_price = recent_prices[-1][1]
        
        if oldest_price <= 0:
            return None
        
        move_pct = (newest_price - oldest_price) / oldest_price
        
        # 1% move in 30s = significant
        if abs(move_pct) >= 0.01:
            return move_pct
        
        return None
    
    def calculate_synthetic_midpoint(self, pm_data: PolymarketData) -> tuple[float, float, float]:
        """
        Calculate synthetic prices from shared orderbook.
        
        Returns (synthetic_yes, synthetic_no, spread_inefficiency).
        If there's a gap, we can exploit it.
        """
        # Direct prices
        yes_price = pm_data.yes_ask
        no_price = pm_data.no_ask
        
        # Synthetic prices (from the other side)
        synthetic_yes = 1 - pm_data.no_bid if pm_data.no_bid > 0 else yes_price
        synthetic_no = 1 - pm_data.yes_bid if pm_data.yes_bid > 0 else no_price
        
        # Check for inefficiency
        inefficiency = abs(yes_price - synthetic_yes)
        
        return synthetic_yes, synthetic_no, inefficiency
    
    async def check_opportunity(
        self,
        asset: str,
        market_type: str,
        consensus: ConsensusData,
        pm_data: PolymarketData,
    ) -> Optional[VirtualTradeLog]:
        """
        Check for trading opportunity based on market type and conditions.
        
        Strategy selection:
        1. Fee-free markets (1h/daily) → Sniper mode
        2. 15m markets at extremes → Extreme Sniper mode
        3. 15m markets with discount → Maker dual-entry mode
        """
        if not self._running:
            return None
        
        # Update price history
        self._update_price_history(asset, consensus.consensus_price, consensus.consensus_timestamp_ms)
        
        # Determine fee
        yes_price = pm_data.yes_ask
        no_price = pm_data.no_ask
        combined_cost = yes_price + no_price
        
        dynamic_fee = self.calculate_dynamic_fee(yes_price, market_type)
        
        # Create base log entry
        log = VirtualTradeLog(
            timestamp=datetime.now(),
            asset=asset,
            market_type=market_type,
            strategy="none",
            yes_price=yes_price,
            no_price=no_price,
            combined_cost=combined_cost,
            base_fee_pct=self.BASE_FEE_15M if "15m" in market_type else 0.0,
            dynamic_fee_pct=dynamic_fee,
            potential_gap_pct=0.0,
            net_virtual_pnl=0.0,
            rebate_multiplier=self._current_rebate_multiplier,
        )
        
        # Check synthetic depth for inefficiency
        syn_yes, syn_no, inefficiency = self.calculate_synthetic_midpoint(pm_data)
        
        # Detect volatility spike
        spike = self.detect_volatility_spike(asset)
        
        # Strategy selection
        if self.is_fee_free_market(market_type):
            # SNIPER MODE - No fees, use old strategy
            return await self._check_sniper_opportunity(log, consensus, pm_data, spike)
        
        elif self.is_extreme_price(yes_price) or self.is_extreme_price(no_price):
            # EXTREME SNIPER - Fees are minimal at extremes
            return await self._check_extreme_sniper_opportunity(log, consensus, pm_data, spike)
        
        else:
            # MAKER MODE - Earn rebates on 15m markets
            return await self._check_maker_opportunity(log, consensus, pm_data, spike)
    
    async def _check_sniper_opportunity(
        self,
        log: VirtualTradeLog,
        consensus: ConsensusData,
        pm_data: PolymarketData,
        spike: Optional[float],
    ) -> Optional[VirtualTradeLog]:
        """
        Sniper mode for fee-free markets (1h, daily).
        
        This is the OLD strategy that still works on these markets!
        """
        log.strategy = "sniper"
        
        # Calculate spot-implied probability
        # For hourly/daily, we have more time for mean reversion
        spot_move_pct = consensus.price_move_30s if hasattr(consensus, 'price_move_30s') else 0.0
        
        # Simple divergence: If spot moved 1%+, PM should reflect it
        if abs(spot_move_pct) < 0.01 and not spike:
            log.rejection_reason = "No significant spot move"
            return None
        
        # Check PM response
        # If spot went UP, YES should be higher
        expected_direction = "up" if (spike and spike > 0) else "down"
        
        # Check for lag
        if spike and spike > 0:
            # Spot went UP - we want to buy YES if it's lagging
            if pm_data.yes_ask < 0.55:  # Still cheap despite spike
                gap = spike - (pm_data.yes_ask - 0.5)  # How much PM lags
                if gap > self.MIN_ARB_GAP_PCT:
                    log.potential_gap_pct = gap
                    log.net_virtual_pnl = gap * self._capital_usd * 0.1  # Simplified
                    log.notes = f"FEE-FREE SNIPER: Buy YES, gap={gap:.2%}"
                    self._record_opportunity(log)
                    return log
        
        elif spike and spike < 0:
            # Spot went DOWN - we want to buy NO if it's lagging
            if pm_data.no_ask < 0.55:  # Still cheap despite drop
                gap = abs(spike) - (pm_data.no_ask - 0.5)
                if gap > self.MIN_ARB_GAP_PCT:
                    log.potential_gap_pct = gap
                    log.net_virtual_pnl = gap * self._capital_usd * 0.1
                    log.notes = f"FEE-FREE SNIPER: Buy NO, gap={gap:.2%}"
                    self._record_opportunity(log)
                    return log
        
        log.rejection_reason = "No actionable divergence"
        return None
    
    async def _check_extreme_sniper_opportunity(
        self,
        log: VirtualTradeLog,
        consensus: ConsensusData,
        pm_data: PolymarketData,
        spike: Optional[float],
    ) -> Optional[VirtualTradeLog]:
        """
        Extreme Sniper for 15m markets when price is <$0.15 or >$0.85.
        
        Fees are minimal at extremes, so sniping can still work.
        """
        log.strategy = "extreme_sniper"
        
        yes_price = pm_data.yes_ask
        no_price = pm_data.no_ask
        
        # Which side is extreme?
        extreme_side = None
        extreme_price = 0.0
        
        if yes_price < self.EXTREME_PRICE_THRESHOLD:
            extreme_side = "YES"
            extreme_price = yes_price
        elif no_price < self.EXTREME_PRICE_THRESHOLD:
            extreme_side = "NO"
            extreme_price = no_price
        elif yes_price > (1 - self.EXTREME_PRICE_THRESHOLD):
            extreme_side = "NO"  # NO is cheap
            extreme_price = no_price
        elif no_price > (1 - self.EXTREME_PRICE_THRESHOLD):
            extreme_side = "YES"  # YES is cheap
            extreme_price = yes_price
        
        if not extreme_side:
            log.rejection_reason = "No extreme price"
            return None
        
        # Check if there's a spike that could push this extreme further
        if spike:
            # Calculate net profit after reduced fees
            potential_profit = abs(spike) * 0.5  # Conservative estimate
            net_after_fee = potential_profit - log.dynamic_fee_pct
            
            if net_after_fee > 0.01:  # At least 1% net profit
                log.potential_gap_pct = potential_profit
                log.net_virtual_pnl = net_after_fee * self._capital_usd * 0.1
                log.notes = f"EXTREME SNIPER: Buy {extreme_side} at ${extreme_price:.3f}, fee only {log.dynamic_fee_pct:.2%}"
                self._record_opportunity(log)
                return log
        
        log.rejection_reason = "No profitable extreme opportunity"
        return None
    
    async def _check_maker_opportunity(
        self,
        log: VirtualTradeLog,
        consensus: ConsensusData,
        pm_data: PolymarketData,
        spike: Optional[float],
    ) -> Optional[VirtualTradeLog]:
        """
        Maker mode for 15m markets near $0.50.
        
        Strategy: Place limit bids on BOTH sides, earn rebates.
        """
        log.strategy = "maker_dual"
        
        combined_cost = pm_data.yes_ask + pm_data.no_ask
        discount = 1.0 - combined_cost
        
        # Check for discount (rare on shared orderbook)
        if discount < self.MIN_DISCOUNT_PCT:
            log.rejection_reason = f"No discount: combined={combined_cost:.3f}"
            return None
        
        # If there's a discount, we can dual-entry
        log.potential_gap_pct = discount
        
        # Calculate rebate estimate
        # Assume we provide 1% of daily liquidity
        estimated_daily_fees = 10000 * 0.0315  # $10k volume * 3.15% = $315
        our_share = estimated_daily_fees * 0.01 * self._current_rebate_multiplier
        log.maker_rebate_est = our_share
        
        # Net P&L = discount profit + rebate
        log.net_virtual_pnl = (discount * self._capital_usd * 0.1) + log.maker_rebate_est
        
        log.notes = f"MAKER DUAL: Buy both at ${combined_cost:.3f}, discount={discount:.2%}, rebate=${log.maker_rebate_est:.2f}/day"
        
        self._record_opportunity(log)
        return log
    
    def _update_price_history(self, asset: str, price: float, timestamp_ms: int) -> None:
        """Update price history for spike detection."""
        if asset not in self._price_history:
            self._price_history[asset] = []
        
        self._price_history[asset].append((timestamp_ms, price))
        
        # Keep only last 2 minutes
        cutoff_ms = timestamp_ms - (120 * 1000)
        self._price_history[asset] = [
            (ts, p) for ts, p in self._price_history[asset]
            if ts > cutoff_ms
        ]
    
    def _simulate_execution(self, log: VirtualTradeLog) -> VirtualTradeLog:
        """
        Simulate realistic execution for accurate virtual testing.
        
        This makes virtual mode match real trading by accounting for:
        1. Network/processing latency (150-300ms)
        2. Maker fill probability (~35%)
        3. Price slippage during execution
        """
        if not self._simulate_fill_probability:
            log.fill_type = "both" if log.strategy == "maker_dual" else "yes_only"
            return log
        
        # Simulate based on strategy type
        if log.strategy == "sniper" or log.strategy == "extreme_sniper":
            # Taker orders: 95% fill rate, but price may slip
            if random.random() < self._taker_fill_rate:
                log.fill_type = "yes_only"  # Simplified - one side fills
                
                # Simulate slippage (0.1-0.5%)
                slippage = random.uniform(0.001, 0.005)
                log.net_virtual_pnl *= (1 - slippage)
                log.notes += f" [Simulated: filled with {slippage:.2%} slippage]"
            else:
                log.fill_type = "none"
                log.net_virtual_pnl = 0
                log.notes += " [Simulated: order failed - no liquidity]"
        
        elif log.strategy == "maker_dual":
            # Maker orders: 35% both fill, 40% one side, 25% neither
            roll = random.random()
            if roll < 0.35:
                log.fill_type = "both"
                log.notes += " [Simulated: dual fill - BEST CASE]"
            elif roll < 0.75:
                log.fill_type = "yes_only" if random.random() < 0.5 else "no_only"
                # Partial fill = exposed to directional risk
                log.net_virtual_pnl *= 0.3  # Assume we hedge but lose some
                log.notes += f" [Simulated: {log.fill_type} - hedged]"
            else:
                log.fill_type = "none"
                log.net_virtual_pnl = 0
                log.notes += " [Simulated: no fills - price moved]"
        
        return log
    
    def _record_opportunity(self, log: VirtualTradeLog) -> None:
        """Record an opportunity to the trade log."""
        # Apply realistic execution simulation
        log = self._simulate_execution(log)
        
        self._trade_logs.append(log)

        # Update daily stats
        date_key = log.timestamp.strftime("%Y-%m-%d")
        if date_key not in self._daily_stats:
            self._daily_stats[date_key] = DailyStats(date=date_key)
        
        stats = self._daily_stats[date_key]
        
        if log.strategy == "sniper" or log.strategy == "extreme_sniper":
            stats.sniper_trades += 1
            stats.sniper_pnl += log.net_virtual_pnl
        else:
            stats.maker_trades += 1
            stats.maker_pnl += log.net_virtual_pnl
        
        # Log the opportunity
        self.logger.info(
            f"💰 OPPORTUNITY: {log.strategy.upper()}",
            asset=log.asset,
            market_type=log.market_type,
            gap=f"{log.potential_gap_pct:.2%}",
            fee=f"{log.dynamic_fee_pct:.2%}",
            net_pnl=f"${log.net_virtual_pnl:.2f}",
            fill_type=log.fill_type,
            notes=log.notes,
        )
        
        # Send Discord alert (non-blocking)
        if self._discord:
            import asyncio
            asyncio.create_task(self._send_discord_alert(log))
        
        if self._on_opportunity:
            self._on_opportunity(log)
    
    async def _send_discord_alert(self, log: VirtualTradeLog) -> None:
        """Send Discord alert for opportunity (non-blocking)."""
        try:
            await self._discord.send_maker_arb_opportunity(log)
        except Exception as e:
            self.logger.debug("Discord alert failed", error=str(e))
    
    async def _export_trade_logs(self) -> None:
        """Export trade logs to CSV for analysis."""
        if not self._trade_logs:
            return
        
        csv_path = self._log_dir / f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            
            # Header
            writer.writerow([
                "timestamp", "asset", "market_type", "strategy",
                "yes_price", "no_price", "combined_cost",
                "base_fee_pct", "dynamic_fee_pct",
                "potential_gap_pct", "net_virtual_pnl",
                "fill_type", "maker_rebate_est", "rebate_multiplier",
                "rejection_reason", "notes"
            ])
            
            # Data
            for log in self._trade_logs:
                writer.writerow([
                    log.timestamp.isoformat(),
                    log.asset,
                    log.market_type,
                    log.strategy,
                    f"{log.yes_price:.4f}",
                    f"{log.no_price:.4f}",
                    f"{log.combined_cost:.4f}",
                    f"{log.base_fee_pct:.4f}",
                    f"{log.dynamic_fee_pct:.4f}",
                    f"{log.potential_gap_pct:.4f}",
                    f"{log.net_virtual_pnl:.4f}",
                    log.fill_type,
                    f"{log.maker_rebate_est:.4f}",
                    f"{log.rebate_multiplier:.2f}",
                    log.rejection_reason,
                    log.notes,
                ])
        
        self.logger.info(f"📊 Exported {len(self._trade_logs)} trades to {csv_path}")
    
    def get_summary(self) -> dict:
        """Get summary of all trading activity."""
        total_opportunities = len(self._trade_logs)
        
        sniper_opps = [l for l in self._trade_logs if "sniper" in l.strategy]
        maker_opps = [l for l in self._trade_logs if l.strategy == "maker_dual"]
        
        total_virtual_pnl = sum(l.net_virtual_pnl for l in self._trade_logs)
        
        return {
            "total_opportunities": total_opportunities,
            "sniper_opportunities": len(sniper_opps),
            "maker_opportunities": len(maker_opps),
            "total_virtual_pnl": f"${total_virtual_pnl:.2f}",
            "sniper_pnl": f"${sum(l.net_virtual_pnl for l in sniper_opps):.2f}",
            "maker_pnl": f"${sum(l.net_virtual_pnl for l in maker_opps):.2f}",
            "days_tracked": len(self._daily_stats),
            "avg_daily_pnl": f"${total_virtual_pnl / max(1, len(self._daily_stats)):.2f}",
        }
    
    def get_daily_breakdown(self) -> list[DailyStats]:
        """Get daily breakdown of stats."""
        return list(self._daily_stats.values())

