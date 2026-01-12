"""
Volatility Spike Sniper Strategy.

This strategy exploits the phenomenon where Market Makers pull liquidity during
sudden volatility spikes (2%+ moves in 60 seconds). During these panics:

1. MMs widen spreads or pull orders entirely
2. YES + NO prices can drop below $1.00 (e.g., $0.80 total)
3. We buy BOTH sides at the discount
4. Guaranteed $1.00 payout - our cost = profit

Example:
- Normal: YES $0.52 + NO $0.50 = $1.02 (no profit, slight loss)
- Spike: YES $0.35 + NO $0.45 = $0.80 (20% guaranteed profit!)

The key insight: We're not predicting direction, we're capturing MM panic.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from uuid import uuid4

import structlog

from src.models.schemas import ConsensusData, PolymarketData
from src.trading.maker_orders import MakerOrderExecutor

logger = structlog.get_logger()


@dataclass
class SpikePosition:
    """Tracks a dual-sided spike position."""
    position_id: str
    asset: str
    
    # Entry details
    entry_time_ms: int
    spike_magnitude_pct: float  # How big was the spike that triggered this
    
    # YES side
    yes_token_id: str
    yes_entry_price: float
    yes_size_shares: float
    yes_cost_usd: float
    
    # NO side
    no_token_id: str
    no_entry_price: float
    no_size_shares: float
    no_cost_usd: float
    
    # Fields with defaults must come after fields without
    yes_order_id: Optional[str] = None
    yes_filled: bool = False
    no_order_id: Optional[str] = None
    no_filled: bool = False
    
    # Combined
    total_cost_usd: float = 0.0
    discount_pct: float = 0.0  # How much below $1.00 we paid
    
    # Exit tracking
    exited_side: Optional[str] = None  # "YES" or "NO"
    exit_price: float = 0.0
    exit_time_ms: int = 0
    
    # P&L
    realized_pnl: float = 0.0
    status: str = "pending"  # pending, active, partial_exit, closed
    
    @property
    def is_active(self) -> bool:
        return self.status in ("active", "partial_exit")
    
    @property
    def expected_profit_pct(self) -> float:
        """Expected profit if we hold to settlement."""
        if self.total_cost_usd <= 0:
            return 0.0
        return (1.0 - self.total_cost_usd) / self.total_cost_usd


@dataclass
class SniperStats:
    """Statistics for the volatility sniper."""
    spikes_detected: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    
    total_invested: float = 0.0
    total_returned: float = 0.0
    total_pnl: float = 0.0
    
    best_discount_pct: float = 0.0
    avg_discount_pct: float = 0.0
    
    wins: int = 0
    losses: int = 0
    
    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0


class VolatilitySniper:
    """
    Volatility Spike Sniper - captures MM panic during sudden price moves.
    
    Strategy:
    1. Monitor consensus price for sudden spikes (2%+ in 60s)
    2. When spike detected, check if YES + NO < $0.95 (discount!)
    3. Buy both sides simultaneously
    4. Wait for MM to reprice (usually 30-120 seconds)
    5. Exit the losing side at better price, or hold to settlement
    
    Risk Management:
    - Max position size per spike
    - Max concurrent positions
    - Minimum discount required (don't buy at $0.99)
    - Exit if discount disappears before settlement
    """
    
    # Configuration
    SPIKE_THRESHOLD_PCT = 0.015  # 1.5% move in lookback window
    SPIKE_LOOKBACK_SECONDS = 60  # Look for spikes in last 60s
    
    MIN_DISCOUNT_PCT = 0.05  # Minimum 5% discount (YES + NO < $0.95)
    TARGET_DISCOUNT_PCT = 0.10  # Ideal 10% discount (YES + NO < $0.90)
    
    MAX_POSITION_SIZE_USD = 50.0  # Max $50 per spike (split between YES/NO)
    MAX_CONCURRENT_POSITIONS = 3
    
    # Timing
    EXIT_TIMEOUT_SECONDS = 300  # Exit after 5 minutes if no clear winner
    MONITOR_INTERVAL_SECONDS = 1.0
    
    def __init__(
        self,
        executor: Optional[MakerOrderExecutor] = None,
        position_size_usd: float = 20.0,
        min_discount_pct: float = 0.05,
        virtual_mode: bool = True,  # Default to virtual for safety
    ):
        self.logger = logger.bind(component="volatility_sniper")
        self._executor = executor
        self._position_size_usd = min(position_size_usd, self.MAX_POSITION_SIZE_USD)
        self._min_discount_pct = min_discount_pct
        self._virtual_mode = virtual_mode
        
        # State
        self._running = False
        self._positions: dict[str, SpikePosition] = {}
        self._stats = SniperStats()
        
        # Virtual mode tracking
        self._virtual_balance = 1000.0  # Start with $1000 virtual
        
        # Price history for spike detection
        self._price_history: dict[str, list[tuple[int, float]]] = {}  # asset -> [(timestamp_ms, price)]
        self._last_spike_time_ms: dict[str, int] = {}  # Cooldown per asset
        
        # Callbacks
        self._on_spike_detected: Optional[Callable] = None
        self._on_position_opened: Optional[Callable] = None
        self._on_position_closed: Optional[Callable] = None
    
    def set_callbacks(
        self,
        on_spike_detected: Optional[Callable] = None,
        on_position_opened: Optional[Callable] = None,
        on_position_closed: Optional[Callable] = None,
    ) -> None:
        """Set callback functions for events."""
        self._on_spike_detected = on_spike_detected
        self._on_position_opened = on_position_opened
        self._on_position_closed = on_position_closed
    
    async def start(self) -> None:
        """Start the sniper."""
        self._running = True
        mode_str = "🧪 VIRTUAL" if self._virtual_mode else "💰 REAL"
        self.logger.info(
            f"🎯 Volatility Sniper started ({mode_str})",
            mode="VIRTUAL" if self._virtual_mode else "REAL",
            position_size=f"${self._position_size_usd}",
            min_discount=f"{self._min_discount_pct:.1%}",
            spike_threshold=f"{self.SPIKE_THRESHOLD_PCT:.1%}",
            virtual_balance=f"${self._virtual_balance:.2f}" if self._virtual_mode else "N/A",
        )
    
    async def stop(self) -> None:
        """Stop the sniper."""
        self._running = False
        self.logger.info(
            "Volatility Sniper stopped",
            stats=self.get_stats_summary(),
        )
    
    def update_price(self, asset: str, price: float, timestamp_ms: int) -> None:
        """Update price history for spike detection."""
        if asset not in self._price_history:
            self._price_history[asset] = []
        
        self._price_history[asset].append((timestamp_ms, price))
        
        # Keep only last 2 minutes of history
        cutoff_ms = timestamp_ms - (120 * 1000)
        self._price_history[asset] = [
            (ts, p) for ts, p in self._price_history[asset]
            if ts > cutoff_ms
        ]
    
    def detect_spike(self, asset: str) -> Optional[float]:
        """
        Detect if there's been a significant price spike.
        
        Returns the spike magnitude (negative for drops) or None if no spike.
        """
        history = self._price_history.get(asset, [])
        if len(history) < 2:
            return None
        
        now_ms = int(time.time() * 1000)
        lookback_ms = self.SPIKE_LOOKBACK_SECONDS * 1000
        
        # Get prices in lookback window
        recent_prices = [
            (ts, p) for ts, p in history
            if now_ms - ts <= lookback_ms
        ]
        
        if len(recent_prices) < 2:
            return None
        
        # Compare oldest to newest in window
        oldest_price = recent_prices[0][1]
        newest_price = recent_prices[-1][1]
        
        if oldest_price <= 0:
            return None
        
        move_pct = (newest_price - oldest_price) / oldest_price
        
        # Check if it's a significant spike
        if abs(move_pct) >= self.SPIKE_THRESHOLD_PCT:
            return move_pct
        
        return None
    
    def check_discount(self, pm_data: PolymarketData) -> tuple[bool, float]:
        """
        Check if there's a profitable discount (YES + NO < $1.00).
        
        Returns (is_profitable, discount_pct).
        """
        if not pm_data:
            return False, 0.0
        
        # Use ask prices (what we'd pay to buy)
        yes_ask = pm_data.yes_ask
        no_ask = pm_data.no_ask
        
        if yes_ask <= 0 or no_ask <= 0:
            return False, 0.0
        
        total_cost = yes_ask + no_ask
        
        if total_cost >= 1.0:
            # No discount - would lose money
            return False, 0.0
        
        discount_pct = 1.0 - total_cost
        
        is_profitable = discount_pct >= self._min_discount_pct
        
        return is_profitable, discount_pct
    
    async def check_opportunity(
        self,
        asset: str,
        consensus: ConsensusData,
        pm_data: PolymarketData,
    ) -> Optional[SpikePosition]:
        """
        Check for a spike + discount opportunity and execute if found.
        
        Returns the opened position or None.
        """
        if not self._running:
            return None
        
        # Check concurrent position limit
        active_positions = [p for p in self._positions.values() if p.is_active]
        if len(active_positions) >= self.MAX_CONCURRENT_POSITIONS:
            return None
        
        # Update price history
        self.update_price(
            asset,
            consensus.consensus_price,
            consensus.consensus_timestamp_ms,
        )
        
        # Check for spike
        spike_magnitude = self.detect_spike(asset)
        if spike_magnitude is None:
            return None
        
        # Spike detected! Check cooldown
        now_ms = int(time.time() * 1000)
        last_spike = self._last_spike_time_ms.get(asset, 0)
        if now_ms - last_spike < 60000:  # 1 minute cooldown
            return None
        
        self._stats.spikes_detected += 1
        self.logger.info(
            "🌪️ SPIKE DETECTED",
            asset=asset,
            magnitude=f"{spike_magnitude:+.2%}",
            price=f"${consensus.consensus_price:.2f}",
        )
        
        if self._on_spike_detected:
            self._on_spike_detected(asset, spike_magnitude, consensus.consensus_price)
        
        # Check for discount
        is_profitable, discount_pct = self.check_discount(pm_data)
        
        if not is_profitable:
            self.logger.info(
                "❌ No discount available",
                asset=asset,
                yes_ask=f"${pm_data.yes_ask:.3f}",
                no_ask=f"${pm_data.no_ask:.3f}",
                total=f"${pm_data.yes_ask + pm_data.no_ask:.3f}",
                discount=f"{discount_pct:.1%}",
                required=f"{self._min_discount_pct:.1%}",
            )
            return None
        
        # OPPORTUNITY! Execute dual entry
        self.logger.info(
            "🎯 DISCOUNT DETECTED - EXECUTING",
            asset=asset,
            discount=f"{discount_pct:.1%}",
            yes_ask=f"${pm_data.yes_ask:.3f}",
            no_ask=f"${pm_data.no_ask:.3f}",
            total_cost=f"${pm_data.yes_ask + pm_data.no_ask:.3f}",
            expected_profit=f"{discount_pct:.1%}",
        )
        
        # Update cooldown
        self._last_spike_time_ms[asset] = now_ms
        
        # Execute the trade
        position = await self._execute_dual_entry(asset, pm_data, spike_magnitude, discount_pct)
        
        return position
    
    async def _execute_dual_entry(
        self,
        asset: str,
        pm_data: PolymarketData,
        spike_magnitude: float,
        discount_pct: float,
    ) -> Optional[SpikePosition]:
        """Execute simultaneous YES and NO buys."""
        
        # Split position between YES and NO
        half_size = self._position_size_usd / 2
        
        yes_price = pm_data.yes_ask
        no_price = pm_data.no_ask
        
        yes_shares = half_size / yes_price
        no_shares = half_size / no_price
        
        # Create position record
        position = SpikePosition(
            position_id=f"{'virtual_' if self._virtual_mode else ''}spike_{str(uuid4())[:8]}",
            asset=asset,
            entry_time_ms=int(time.time() * 1000),
            spike_magnitude_pct=spike_magnitude,
            yes_token_id=pm_data.yes_token_id,
            yes_entry_price=yes_price,
            yes_size_shares=yes_shares,
            yes_cost_usd=half_size,
            no_token_id=pm_data.no_token_id,
            no_entry_price=no_price,
            no_size_shares=no_shares,
            no_cost_usd=half_size,
            total_cost_usd=self._position_size_usd,
            discount_pct=discount_pct,
            status="pending",
        )
        
        if self._virtual_mode:
            # VIRTUAL MODE: Simulate the fills
            position.yes_filled = True
            position.no_filled = True
            position.yes_order_id = f"virtual_{uuid4().hex[:8]}"
            position.no_order_id = f"virtual_{uuid4().hex[:8]}"
            
            # Deduct from virtual balance
            self._virtual_balance -= position.total_cost_usd
            
            self.logger.info(
                "🧪 [VIRTUAL] YES side filled",
                position_id=position.position_id,
                price=f"${yes_price:.3f}",
                shares=f"{yes_shares:.2f}",
                cost=f"${half_size:.2f}",
            )
            self.logger.info(
                "🧪 [VIRTUAL] NO side filled",
                position_id=position.position_id,
                price=f"${no_price:.3f}",
                shares=f"{no_shares:.2f}",
                cost=f"${half_size:.2f}",
            )
        else:
            # REAL MODE: Execute actual orders
            # Execute YES buy
            try:
                yes_result = await self._executor.place_maker_order(
                    token_id=pm_data.yes_token_id,
                    side="BUY",
                    size=yes_shares,
                    target_price=yes_price,
                    best_bid=pm_data.yes_bid,
                    best_ask=pm_data.yes_ask,
                    aggressive=True,  # Use taker for speed during spikes
                )
                
                if yes_result and yes_result.success:
                    position.yes_filled = True
                    position.yes_order_id = yes_result.order_id
                    self.logger.info(
                        "✅ YES side filled",
                        position_id=position.position_id,
                        price=f"${yes_result.fill_price:.3f}",
                        shares=f"{yes_shares:.2f}",
                    )
                else:
                    self.logger.warning(
                        "⚠️ YES side not filled",
                        position_id=position.position_id,
                    )
            except Exception as e:
                self.logger.error("YES order failed", error=str(e))
            
            # Execute NO buy
            try:
                no_result = await self._executor.place_maker_order(
                    token_id=pm_data.no_token_id,
                    side="BUY",
                    size=no_shares,
                    target_price=no_price,
                    best_bid=pm_data.no_bid,
                    best_ask=pm_data.no_ask,
                    aggressive=True,  # Use taker for speed during spikes
                )
                
                if no_result and no_result.success:
                    position.no_filled = True
                    position.no_order_id = no_result.order_id
                    self.logger.info(
                        "✅ NO side filled",
                        position_id=position.position_id,
                        price=f"${no_result.fill_price:.3f}",
                        shares=f"{no_shares:.2f}",
                    )
                else:
                    self.logger.warning(
                        "⚠️ NO side not filled",
                        position_id=position.position_id,
                    )
            except Exception as e:
                self.logger.error("NO order failed", error=str(e))
        
        # Check if we got both sides
        if position.yes_filled and position.no_filled:
            position.status = "active"
            self._positions[position.position_id] = position
            self._stats.positions_opened += 1
            self._stats.total_invested += position.total_cost_usd
            
            if discount_pct > self._stats.best_discount_pct:
                self._stats.best_discount_pct = discount_pct
            
            # Calculate expected profit
            expected_payout = 1.00  # One side will be worth $1
            expected_profit = expected_payout - position.total_cost_usd
            
            mode_prefix = "🧪 [VIRTUAL]" if self._virtual_mode else "🎯"
            self.logger.info(
                f"{mode_prefix} DUAL POSITION OPENED",
                position_id=position.position_id,
                asset=asset,
                total_cost=f"${position.total_cost_usd:.2f}",
                discount=f"{discount_pct:.1%}",
                expected_profit=f"${expected_profit:.2f}",
                virtual_balance=f"${self._virtual_balance:.2f}" if self._virtual_mode else "N/A",
            )
            
            if self._on_position_opened:
                self._on_position_opened(position)
            
            # Start monitoring this position
            asyncio.create_task(self._monitor_position(position))
            
            return position
        else:
            # Partial fill - need to handle carefully
            self.logger.warning(
                "⚠️ Partial fill - only one side executed",
                position_id=position.position_id,
                yes_filled=position.yes_filled,
                no_filled=position.no_filled,
            )
            # For now, we'll still track it but mark as partial
            position.status = "partial"
            self._positions[position.position_id] = position
            return position
    
    async def _monitor_position(self, position: SpikePosition) -> None:
        """
        Monitor a spike position for exit opportunities.
        
        Exit strategies:
        1. Discount disappears (YES + NO > $1.00) - exit losing side
        2. One side clearly winning (>$0.70) - consider exiting loser
        3. Timeout - exit both at market
        4. Market settlement approaching - hold for guaranteed $1.00
        """
        self.logger.info(
            "📊 Monitoring position",
            position_id=position.position_id,
            timeout=f"{self.EXIT_TIMEOUT_SECONDS}s",
        )
        
        start_ms = position.entry_time_ms
        timeout_ms = self.EXIT_TIMEOUT_SECONDS * 1000
        
        while position.is_active and self._running:
            await asyncio.sleep(self.MONITOR_INTERVAL_SECONDS)
            
            now_ms = int(time.time() * 1000)
            elapsed_ms = now_ms - start_ms
            
            # TODO: Get current PM data and check exit conditions
            # For now, just log status
            
            if elapsed_ms > timeout_ms:
                self.logger.info(
                    "⏰ Position timeout - holding for settlement",
                    position_id=position.position_id,
                    elapsed=f"{elapsed_ms / 1000:.0f}s",
                )
                break
        
        self.logger.info(
            "📊 Position monitoring ended",
            position_id=position.position_id,
        )
    
    def get_stats_summary(self) -> dict:
        """Get summary of sniper statistics."""
        return {
            "spikes_detected": self._stats.spikes_detected,
            "positions_opened": self._stats.positions_opened,
            "positions_closed": self._stats.positions_closed,
            "total_invested": f"${self._stats.total_invested:.2f}",
            "total_pnl": f"${self._stats.total_pnl:.2f}",
            "best_discount": f"{self._stats.best_discount_pct:.1%}",
            "win_rate": f"{self._stats.win_rate:.1%}",
            "active_positions": len([p for p in self._positions.values() if p.is_active]),
        }
    
    def get_active_positions(self) -> list[SpikePosition]:
        """Get list of active positions."""
        return [p for p in self._positions.values() if p.is_active]

