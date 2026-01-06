"""
Virtual Trade Simulator - Simulates trades without real execution.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Any
from collections import deque
from uuid import uuid4

import structlog

from src.models.schemas import SignalCandidate, PolymarketData, OracleData

logger = structlog.get_logger()


@dataclass
class VirtualPosition:
    """Represents a simulated position."""
    position_id: str
    signal_id: str
    market_id: str
    direction: str  # "UP" or "DOWN"
    
    # Entry details
    entry_price: float
    entry_time_ms: int
    position_size_eur: float
    
    # Market state at entry
    oracle_age_at_entry: float
    spread_at_entry: float
    liquidity_at_entry: float
    confidence_at_entry: float
    
    # Entry context for rich alerts
    spot_price_at_entry: float = 0.0
    oracle_price_at_entry: float = 0.0
    volume_surge_at_entry: float = 0.0
    spike_concentration_at_entry: float = 0.0
    orderbook_imbalance_at_entry: float = 0.0
    
    # Tracking (updated during monitoring)
    max_profit_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    current_price: Optional[float] = None
    
    # Exit details (filled when closed)
    exit_price: Optional[float] = None
    exit_time_ms: Optional[int] = None
    exit_reason: Optional[str] = None
    realized_pnl_eur: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    
    @property
    def is_open(self) -> bool:
        return self.exit_price is None
    
    @property
    def duration_seconds(self) -> float:
        if self.is_open:
            return (int(time.time() * 1000) - self.entry_time_ms) / 1000
        else:
            return (self.exit_time_ms - self.entry_time_ms) / 1000
    
    @property
    def current_pnl_pct(self) -> float:
        if not self.current_price or self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price
    
    @property
    def current_pnl_eur(self) -> float:
        return self.position_size_eur * self.current_pnl_pct


@dataclass
class VirtualPerformance:
    """Performance tracking for virtual trading."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_eur: float = 0.0
    
    # Streaks
    current_streak: int = 0  # Positive = wins, negative = losses
    best_streak: int = 0
    worst_streak: int = 0
    
    # Best/worst trades
    best_trade_pnl_eur: float = 0.0
    worst_trade_pnl_eur: float = 0.0
    
    # Exit reason breakdown
    exit_reasons: Dict[str, int] = field(default_factory=dict)
    
    # Hourly stats
    trades_by_hour: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
    
    @property
    def avg_profit_per_trade(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl_eur / self.total_trades
    
    def record_trade(self, position: VirtualPosition) -> None:
        """Record a closed trade."""
        self.total_trades += 1
        pnl = position.realized_pnl_eur or 0.0
        self.total_pnl_eur += pnl
        
        # Win/loss tracking
        if pnl > 0:
            self.winning_trades += 1
            if self.current_streak >= 0:
                self.current_streak += 1
            else:
                self.current_streak = 1
        else:
            self.losing_trades += 1
            if self.current_streak <= 0:
                self.current_streak -= 1
            else:
                self.current_streak = -1
        
        # Update streaks
        self.best_streak = max(self.best_streak, self.current_streak)
        self.worst_streak = min(self.worst_streak, self.current_streak)
        
        # Best/worst trades
        self.best_trade_pnl_eur = max(self.best_trade_pnl_eur, pnl)
        self.worst_trade_pnl_eur = min(self.worst_trade_pnl_eur, pnl)
        
        # Exit reason tracking
        exit_reason = position.exit_reason or "unknown"
        self.exit_reasons[exit_reason] = self.exit_reasons.get(exit_reason, 0) + 1
        
        # Hourly tracking
        hour = time.localtime(position.entry_time_ms / 1000).tm_hour
        if hour not in self.trades_by_hour:
            self.trades_by_hour[hour] = {"trades": 0, "wins": 0, "pnl": 0.0}
        self.trades_by_hour[hour]["trades"] += 1
        if pnl > 0:
            self.trades_by_hour[hour]["wins"] += 1
        self.trades_by_hour[hour]["pnl"] += pnl


class VirtualTrader:
    """
    Simulates trades without real execution.
    
    Features:
    - Opens virtual positions based on signals
    - Monitors positions and checks exit conditions
    - Tracks comprehensive performance statistics
    - Sends callbacks for Discord alerts
    """
    
    def __init__(
        self,
        polymarket_feed,
        chainlink_feed,
        position_size_eur: float = 20.0,
        take_profit_pct: float = 0.08,
        stop_loss_pct: float = -0.03,
        time_limit_seconds: float = 90.0,
        emergency_time_seconds: float = 120.0,
    ):
        self.polymarket_feed = polymarket_feed
        self.chainlink_feed = chainlink_feed
        self.logger = logger.bind(component="virtual_trader")
        
        # Position settings
        self.position_size_eur = position_size_eur
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.time_limit_seconds = time_limit_seconds
        self.emergency_time_seconds = emergency_time_seconds
        
        # State
        self.open_positions: List[VirtualPosition] = []
        self.closed_positions: deque = deque(maxlen=500)
        self.performance = VirtualPerformance()
        
        # Callbacks for alerts
        self._on_position_opened: Optional[Callable] = None
        self._on_position_update: Optional[Callable] = None
        self._on_position_closed: Optional[Callable] = None
        
        # Monitoring tasks
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._running = True
    
    def set_callbacks(
        self,
        on_opened: Optional[Callable] = None,
        on_update: Optional[Callable] = None,
        on_closed: Optional[Callable] = None,
    ) -> None:
        """Set callbacks for position events."""
        self._on_position_opened = on_opened
        self._on_position_update = on_update
        self._on_position_closed = on_closed
    
    async def open_virtual_position(
        self,
        signal: SignalCandidate,
        market_id: str,
        pm_data: PolymarketData,
    ) -> VirtualPosition:
        """Simulate opening a position."""
        
        # Determine entry price based on direction
        if signal.direction.value.upper() == "UP":
            entry_price = pm_data.yes_bid  # We'd buy YES token
        else:
            entry_price = pm_data.no_bid  # We'd buy NO token
        
        # Get additional context
        oracle = self.chainlink_feed.get_data() if self.chainlink_feed else None
        
        # Create virtual position
        position = VirtualPosition(
            position_id=f"virtual_{signal.signal_id[:8]}_{str(uuid4())[:4]}",
            signal_id=signal.signal_id,
            market_id=market_id,
            direction=signal.direction.value.upper(),
            entry_price=entry_price,
            entry_time_ms=int(time.time() * 1000),
            position_size_eur=self.position_size_eur,
            oracle_age_at_entry=oracle.oracle_age_seconds if oracle else 0,
            spread_at_entry=pm_data.spread,
            liquidity_at_entry=pm_data.yes_liquidity_best,
            confidence_at_entry=signal.scoring.confidence if signal.scoring else 0,
            spot_price_at_entry=signal.consensus.consensus_price if signal.consensus else 0,
            oracle_price_at_entry=oracle.current_value if oracle else 0,
            volume_surge_at_entry=signal.consensus.volume_surge_ratio if signal.consensus else 0,
            spike_concentration_at_entry=signal.consensus.spike_concentration if signal.consensus else 0,
            orderbook_imbalance_at_entry=pm_data.orderbook_imbalance_ratio,
            current_price=entry_price,
        )
        
        self.open_positions.append(position)
        
        self.logger.info(
            "Virtual position opened",
            position_id=position.position_id,
            direction=position.direction,
            entry_price=entry_price,
            confidence=position.confidence_at_entry,
        )
        
        # Trigger callback
        if self._on_position_opened:
            try:
                await self._on_position_opened(position, signal, pm_data)
            except Exception as e:
                self.logger.error("Error in position opened callback", error=str(e))
        
        # Start monitoring this position
        task = asyncio.create_task(self._monitor_virtual_position(position))
        self._monitor_tasks[position.position_id] = task
        
        return position
    
    async def _monitor_virtual_position(self, position: VirtualPosition) -> None:
        """Monitor virtual position and close when conditions met."""
        
        last_update_time = 0
        update_interval = 30  # Send updates every 30 seconds
        
        while position.is_open and self._running:
            try:
                # Get current market price
                pm_data = self.polymarket_feed.get_data() if self.polymarket_feed else None
                
                if not pm_data:
                    await asyncio.sleep(1)
                    continue
                
                # Update current price (what we could sell at)
                if position.direction == "UP":
                    current_price = pm_data.yes_ask  # We'd sell YES at ask
                else:
                    current_price = pm_data.no_ask
                
                position.current_price = current_price
                
                # Calculate current P&L
                current_pnl_pct = position.current_pnl_pct
                
                # Update max profit/drawdown tracking
                if current_pnl_pct > position.max_profit_pct:
                    position.max_profit_pct = current_pnl_pct
                
                if current_pnl_pct < position.max_drawdown_pct:
                    position.max_drawdown_pct = current_pnl_pct
                
                # Send periodic updates
                now = int(time.time())
                if now - last_update_time >= update_interval:
                    last_update_time = now
                    if self._on_position_update:
                        try:
                            await self._on_position_update(position)
                        except Exception as e:
                            self.logger.error("Error in position update callback", error=str(e))
                
                # Check exit conditions
                exit_reason = self._check_exit_conditions(position, pm_data)
                
                if exit_reason:
                    await self._close_virtual_position(position, exit_reason)
                    break
                
                await asyncio.sleep(1)  # Check every second
                
            except asyncio.CancelledError:
                self.logger.info("Position monitoring cancelled", position_id=position.position_id)
                break
            except Exception as e:
                self.logger.error("Error monitoring virtual position", error=str(e))
                await asyncio.sleep(1)
    
    def _check_exit_conditions(
        self,
        position: VirtualPosition,
        pm_data: PolymarketData,
    ) -> Optional[str]:
        """Check if virtual position should be closed."""
        
        current_pnl_pct = position.current_pnl_pct
        oracle = self.chainlink_feed.get_data() if self.chainlink_feed else None
        
        # EXIT 1: Oracle update imminent (oracle about to update)
        if oracle and oracle.oracle_age_seconds > 65:
            return "oracle_update_imminent"
        
        # EXIT 2: Spread converged (market corrected)
        if pm_data.spread < 0.015:  # 1.5% spread means opportunity closed
            return "spread_converged"
        
        # EXIT 3: Take profit
        if current_pnl_pct >= self.take_profit_pct:
            return "take_profit"
        
        # EXIT 4: Stop loss
        if current_pnl_pct <= self.stop_loss_pct:
            return "stop_loss"
        
        # EXIT 5: Time limit (standard)
        if position.duration_seconds > self.time_limit_seconds:
            return "time_limit"
        
        # EXIT 6: Emergency time limit
        if position.duration_seconds > self.emergency_time_seconds:
            return "emergency_time"
        
        # EXIT 7: Liquidity collapsed (danger signal)
        if pm_data.liquidity_collapsing:
            return "liquidity_collapse"
        
        return None
    
    async def _close_virtual_position(
        self,
        position: VirtualPosition,
        exit_reason: str,
    ) -> None:
        """Close virtual position and record results."""
        
        # Mark position as closed
        position.exit_price = position.current_price
        position.exit_time_ms = int(time.time() * 1000)
        position.exit_reason = exit_reason
        
        # Calculate realized P&L
        position.realized_pnl_pct = position.current_pnl_pct
        position.realized_pnl_eur = position.position_size_eur * position.realized_pnl_pct
        
        # Record in performance tracker
        self.performance.record_trade(position)
        
        # Move to closed positions
        if position in self.open_positions:
            self.open_positions.remove(position)
        self.closed_positions.append(position)
        
        # Remove monitoring task
        if position.position_id in self._monitor_tasks:
            del self._monitor_tasks[position.position_id]
        
        self.logger.info(
            "Virtual position closed",
            position_id=position.position_id,
            exit_reason=exit_reason,
            realized_pnl_pct=f"{position.realized_pnl_pct:.2%}",
            realized_pnl_eur=f"€{position.realized_pnl_eur:.2f}",
            duration_s=f"{position.duration_seconds:.1f}s",
        )
        
        # Trigger callback
        if self._on_position_closed:
            try:
                await self._on_position_closed(position)
            except Exception as e:
                self.logger.error("Error in position closed callback", error=str(e))
    
    def get_performance_summary(self) -> dict:
        """Get current virtual trading performance."""
        perf = self.performance
        
        return {
            "total_trades": perf.total_trades,
            "winning_trades": perf.winning_trades,
            "losing_trades": perf.losing_trades,
            "win_rate": perf.win_rate,
            "avg_profit_per_trade": perf.avg_profit_per_trade,
            "total_pnl": perf.total_pnl_eur,
            "best_trade": perf.best_trade_pnl_eur,
            "worst_trade": perf.worst_trade_pnl_eur,
            "current_streak": perf.current_streak,
            "best_streak": perf.best_streak,
            "worst_streak": perf.worst_streak,
            "exit_reasons": dict(perf.exit_reasons),
            "open_positions": len(self.open_positions),
        }
    
    def get_detailed_stats(self) -> dict:
        """Get detailed statistics including hourly breakdown."""
        summary = self.get_performance_summary()
        
        # Add hourly breakdown
        hourly_stats = {}
        for hour, stats in self.performance.trades_by_hour.items():
            trades = stats["trades"]
            wins = stats["wins"]
            pnl = stats["pnl"]
            hourly_stats[hour] = {
                "trades": trades,
                "win_rate": wins / trades if trades > 0 else 0,
                "pnl": pnl,
                "avg_pnl": pnl / trades if trades > 0 else 0,
            }
        
        summary["hourly_stats"] = hourly_stats
        
        # Recent trades summary
        recent = list(self.closed_positions)[-10:]
        summary["recent_trades"] = [
            {
                "position_id": p.position_id[:12],
                "direction": p.direction,
                "pnl_pct": p.realized_pnl_pct,
                "pnl_eur": p.realized_pnl_eur,
                "exit_reason": p.exit_reason,
                "duration_s": p.duration_seconds,
            }
            for p in recent
        ]
        
        return summary
    
    async def stop(self) -> None:
        """Stop all monitoring tasks."""
        self._running = False
        
        # Cancel all monitoring tasks
        for task in self._monitor_tasks.values():
            if not task.done():
                task.cancel()
        
        # Wait for tasks to finish
        if self._monitor_tasks:
            await asyncio.gather(*self._monitor_tasks.values(), return_exceptions=True)
        
        self._monitor_tasks.clear()
        self.logger.info("Virtual trader stopped")

