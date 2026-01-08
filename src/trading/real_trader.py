"""
Real Trade Executor - Executes actual trades on Polymarket.

Uses maker-only strategy for 0% fees + rebates.
Falls back to virtual trading if maker order fails.

IMPORTANT: This module handles real money. Use with caution.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Any
from uuid import uuid4

import structlog

from src.models.schemas import SignalCandidate, PolymarketData
from src.trading.maker_orders import (
    MakerOrderExecutor,
    MakerOrderResult,
    OrderStatus,
    PY_CLOB_AVAILABLE,
)
from src.utils.session_tracker import session_tracker
from config.settings import settings

logger = structlog.get_logger()


@dataclass
class RealPosition:
    """Represents a real trading position."""
    position_id: str
    signal_id: str
    market_id: str
    token_id: str  # Actual token ID for the position
    direction: str  # "UP" or "DOWN"
    asset: str
    
    # Order details
    order_id: Optional[str] = None
    entry_price: float = 0.0
    size_shares: float = 0.0
    size_eur: float = 0.0
    entry_time_ms: int = 0
    
    # Status
    is_filled: bool = False
    is_closed: bool = False
    
    # Exit details
    exit_order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time_ms: Optional[int] = None
    exit_reason: Optional[str] = None
    
    # P&L tracking
    realized_pnl_eur: Optional[float] = None
    fees_paid_eur: float = 0.0
    rebates_earned_eur: float = 0.0
    
    @property
    def is_open(self) -> bool:
        return self.is_filled and not self.is_closed
    
    @property
    def duration_seconds(self) -> float:
        if not self.entry_time_ms:
            return 0.0
        end_time = self.exit_time_ms or int(time.time() * 1000)
        return (end_time - self.entry_time_ms) / 1000


class RealTrader:
    """
    Executes real trades on Polymarket using maker-only strategy.
    
    Features:
    - Maker-only orders (0% fees + rebates)
    - 3.5s timeout for fills
    - NO fallback to taker (missed trade is better than paying 3% fee)
    - Position monitoring and exit management
    - Full P&L tracking
    
    Usage:
        trader = RealTrader(polymarket_feed)
        await trader.initialize()
        
        # Open position
        position = await trader.open_position(signal, pm_data, asset)
        
        # Monitor positions (runs in background)
        # Positions auto-close on TP/SL/time limits
    """
    
    def __init__(
        self,
        polymarket_feed,
        position_size_eur: float = 20.0,
    ):
        self.polymarket_feed = polymarket_feed
        self.position_size_eur = position_size_eur
        self.logger = logger.bind(component="real_trader")
        
        # Maker executor
        self._executor: Optional[MakerOrderExecutor] = None
        self._initialized = False
        
        # State
        self.open_positions: List[RealPosition] = []
        self.closed_positions: List[RealPosition] = []
        
        # Callbacks
        self._on_position_opened: Optional[Callable] = None
        self._on_position_closed: Optional[Callable] = None
        
        # Monitoring
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._running = True
        
        # Stats
        self.total_trades = 0
        self.successful_fills = 0
        self.missed_trades = 0
        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.total_rebates = 0.0
    
    async def initialize(self) -> bool:
        """Initialize the real trader and maker executor."""
        if not PY_CLOB_AVAILABLE:
            self.logger.error(
                "py-clob-client not available. Install with: pip install py-clob-client"
            )
            return False
        
        if not settings.private_key:
            self.logger.error("Private key not configured in settings")
            return False
        
        self._executor = MakerOrderExecutor(
            private_key=settings.private_key,
        )
        
        if not await self._executor.initialize():
            self.logger.error("Failed to initialize maker executor")
            return False
        
        self._initialized = True
        self.logger.info("Real trader initialized successfully")
        return True
    
    def set_callbacks(
        self,
        on_opened: Optional[Callable] = None,
        on_closed: Optional[Callable] = None,
    ) -> None:
        """Set callbacks for position events."""
        self._on_position_opened = on_opened
        self._on_position_closed = on_closed
    
    async def open_position(
        self,
        signal: SignalCandidate,
        pm_data: PolymarketData,
        asset: str = "BTC",
    ) -> Optional[RealPosition]:
        """
        Open a real position using maker-only order.
        
        Returns None if order doesn't fill (we don't chase with taker).
        """
        if not self._initialized:
            self.logger.error("Trader not initialized")
            return None
        
        self.total_trades += 1
        
        # Determine side and token
        if signal.direction.value.upper() == "UP":
            side = "BUY"
            token_id = pm_data.yes_token_id
            entry_price = pm_data.yes_ask
        else:
            side = "BUY"  # Buying NO token
            token_id = pm_data.no_token_id
            entry_price = pm_data.no_ask
        
        if not token_id:
            self.logger.error("No token ID available", direction=signal.direction)
            return None
        
        # Get asset-specific settings
        asset_config = settings.asset_configs.get(asset)
        
        # Create position record
        position = RealPosition(
            position_id=f"real_{str(uuid4())[:8]}",
            signal_id=signal.signal_id,
            market_id=pm_data.market_id,
            token_id=token_id,
            direction=signal.direction.value.upper(),
            asset=asset,
            size_eur=self.position_size_eur,
        )
        
        self.logger.info(
            "Attempting maker order",
            position_id=position.position_id,
            asset=asset,
            direction=position.direction,
            target_price=f"${entry_price:.3f}",
        )
        
        # Execute maker order
        result = await self._executor.place_maker_order(
            token_id=token_id,
            side=side,
            size=self.position_size_eur / entry_price,  # Convert EUR to shares
            target_price=entry_price,
            best_bid=pm_data.yes_bid if position.direction == "UP" else pm_data.no_bid,
            best_ask=pm_data.yes_ask if position.direction == "UP" else pm_data.no_ask,
        )
        
        if result.success:
            self.successful_fills += 1
            
            position.is_filled = True
            position.order_id = result.order_id
            position.entry_price = result.fill_price or entry_price
            position.size_shares = result.filled_size
            position.entry_time_ms = int(time.time() * 1000)
            position.rebates_earned_eur = result.rebate_earned
            
            self.open_positions.append(position)
            
            # Record in session tracker
            session_tracker.record_trade_opened(
                position_id=position.position_id,
                asset=asset,
                direction=position.direction,
                entry_price=position.entry_price,
                confidence=signal.scoring.confidence if signal.scoring else 0,
                divergence_at_entry=0,  # Could calculate from signal
            )
            
            self.logger.info(
                "✅ REAL POSITION OPENED (MAKER)",
                position_id=position.position_id,
                asset=asset,
                direction=position.direction,
                entry_price=f"${position.entry_price:.3f}",
                size_eur=f"€{position.size_eur:.2f}",
                fill_time_ms=result.time_to_fill_ms,
            )
            
            # Trigger callback
            if self._on_position_opened:
                try:
                    await self._on_position_opened(position, signal, pm_data)
                except Exception as e:
                    self.logger.error("Error in opened callback", error=str(e))
            
            # Start monitoring
            task = asyncio.create_task(self._monitor_position(position))
            self._monitor_tasks[position.position_id] = task
            
            return position
        else:
            self.missed_trades += 1
            
            self.logger.info(
                "⏱️ Maker order not filled - SKIPPING trade",
                position_id=position.position_id,
                asset=asset,
                reason=result.error or "timeout",
                time_waited_ms=result.time_to_fill_ms,
            )
            
            # Don't fallback to taker - missed trade is better than 3% fee
            return None
    
    async def _monitor_position(self, position: RealPosition) -> None:
        """Monitor position and close when exit conditions met."""
        
        # Get asset-specific settings
        asset_config = settings.asset_configs.get(position.asset)
        time_limit = asset_config.time_limit_s or 90
        take_profit = asset_config.take_profit_pct or 0.08
        stop_loss_eur = asset_config.stop_loss_eur or 0.03
        
        while position.is_open and self._running:
            try:
                pm_data = self.polymarket_feed.get_data() if self.polymarket_feed else None
                
                if not pm_data:
                    await asyncio.sleep(1)
                    continue
                
                # Get current price (bid for selling)
                if position.direction == "UP":
                    current_price = pm_data.yes_bid
                else:
                    current_price = pm_data.no_bid
                
                # Calculate P&L
                pnl_pct = (current_price - position.entry_price) / position.entry_price
                price_drop = position.entry_price - current_price
                
                # Check exit conditions
                exit_reason = None
                
                # Take profit
                if pnl_pct >= take_profit:
                    exit_reason = "take_profit"
                
                # Stop loss (absolute price move)
                elif price_drop >= stop_loss_eur:
                    exit_reason = "stop_loss"
                
                # Time limit
                elif position.duration_seconds > time_limit:
                    exit_reason = "time_limit"
                
                # Liquidity collapse
                elif pm_data.liquidity_collapsing:
                    exit_reason = "liquidity_collapse"
                
                if exit_reason:
                    await self._close_position(position, current_price, exit_reason)
                    break
                
                await asyncio.sleep(0.5)  # Check every 500ms
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error monitoring position", error=str(e))
                await asyncio.sleep(1)
    
    async def _close_position(
        self,
        position: RealPosition,
        exit_price: float,
        exit_reason: str,
    ) -> None:
        """Close a position with maker order for exit."""
        
        self.logger.info(
            "Closing position",
            position_id=position.position_id,
            exit_reason=exit_reason,
            exit_price=f"${exit_price:.3f}",
        )
        
        # Place maker sell order
        result = await self._executor.place_maker_order(
            token_id=position.token_id,
            side="SELL",
            size=position.size_shares,
            target_price=exit_price,
            best_bid=exit_price,  # Use exit price as reference
            best_ask=exit_price + 0.02,
        )
        
        # Mark position as closed
        position.is_closed = True
        position.exit_price = result.fill_price if result.success else exit_price
        position.exit_time_ms = int(time.time() * 1000)
        position.exit_reason = exit_reason
        position.exit_order_id = result.order_id
        
        # Calculate P&L
        pnl_pct = (position.exit_price - position.entry_price) / position.entry_price
        gross_pnl = position.size_eur * pnl_pct
        
        position.rebates_earned_eur += result.rebate_earned if result.success else 0
        position.realized_pnl_eur = gross_pnl + position.rebates_earned_eur - position.fees_paid_eur
        
        # Update stats
        self.total_pnl += position.realized_pnl_eur
        self.total_rebates += position.rebates_earned_eur
        
        # Move to closed
        if position in self.open_positions:
            self.open_positions.remove(position)
        self.closed_positions.append(position)
        
        # Remove monitoring task
        if position.position_id in self._monitor_tasks:
            del self._monitor_tasks[position.position_id]
        
        # Record in session tracker
        session_tracker.record_trade_closed(
            position_id=position.position_id,
            asset=position.asset,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=position.exit_price,
            exit_reason=exit_reason,
            duration_seconds=position.duration_seconds,
            gross_pnl_eur=gross_pnl,
            total_fees_eur=position.fees_paid_eur,
            net_pnl_eur=position.realized_pnl_eur,
        )
        
        self.logger.info(
            "✅ REAL POSITION CLOSED",
            position_id=position.position_id,
            asset=position.asset,
            exit_reason=exit_reason,
            entry=f"${position.entry_price:.3f}",
            exit=f"${position.exit_price:.3f}",
            pnl=f"€{position.realized_pnl_eur:.2f}",
            duration=f"{position.duration_seconds:.0f}s",
            rebates=f"€{position.rebates_earned_eur:.4f}",
        )
        
        # Trigger callback
        if self._on_position_closed:
            try:
                await self._on_position_closed(position)
            except Exception as e:
                self.logger.error("Error in closed callback", error=str(e))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get trader statistics."""
        fill_rate = self.successful_fills / self.total_trades if self.total_trades > 0 else 0
        
        return {
            "initialized": self._initialized,
            "total_trades_attempted": self.total_trades,
            "successful_fills": self.successful_fills,
            "missed_trades": self.missed_trades,
            "fill_rate": fill_rate,
            "open_positions": len(self.open_positions),
            "closed_positions": len(self.closed_positions),
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "total_rebates": self.total_rebates,
            "net_pnl": self.total_pnl,  # Already net (fees = 0 for maker)
            "executor_stats": self._executor.get_stats() if self._executor else {},
        }
    
    async def stop(self) -> None:
        """Stop the trader and all monitoring tasks."""
        self._running = False
        
        for task in self._monitor_tasks.values():
            if not task.done():
                task.cancel()
        
        if self._monitor_tasks:
            await asyncio.gather(*self._monitor_tasks.values(), return_exceptions=True)
        
        self._monitor_tasks.clear()
        self.logger.info("Real trader stopped", stats=self.get_stats())

