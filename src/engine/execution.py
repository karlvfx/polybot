"""
Execution Engine for trade management.
Handles order placement, nonce management, and position tracking.

Enhanced features:
- Pre-trade slippage simulation
- Adaptive take profit based on oracle age and mispricing
- Stop loss protection
- Partial exits to lock in gains
- Oracle update imminent detection
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

import structlog
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from eth_account import Account

from src.models.schemas import (
    SignalCandidate,
    ActionData,
    OutcomeData,
    ActionDecision,
    ExitReason,
)
from config.settings import settings

if TYPE_CHECKING:
    from src.feeds.polymarket import PolymarketFeed
    from src.feeds.chainlink import ChainlinkFeed

logger = structlog.get_logger()


class OrderStatus(str, Enum):
    """Order status tracking."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Position:
    """Active trading position with enhanced tracking."""
    signal_id: str
    market_id: str
    direction: str
    entry_price: float
    size_eur: float
    entry_time_ms: int
    order_id: Optional[str] = None
    tx_hash: Optional[str] = None
    filled: bool = False
    fill_price: float = 0.0
    max_adverse_move: float = 0.0
    # Enhanced fields for adaptive exits
    initial_mispricing: float = 0.0  # Mispricing at entry time
    initial_oracle_age: float = 0.0  # Oracle age at entry
    partial_exit_done: bool = False  # Track if partial exit was taken
    partial_exit_size: float = 0.0   # Size exited in partial
    remaining_size: float = 0.0      # Remaining position size


@dataclass
class NonceTracker:
    """Track nonces to prevent collisions."""
    confirmed_nonce: int = 0
    pending_nonces: set = field(default_factory=set)
    
    def get_next(self, chain_nonce: int) -> int:
        """Get next safe nonce."""
        # Start from chain nonce
        nonce = max(chain_nonce, self.confirmed_nonce)
        
        # Skip any pending nonces
        while nonce in self.pending_nonces:
            nonce += 1
        
        self.pending_nonces.add(nonce)
        return nonce
    
    def confirm(self, nonce: int) -> None:
        """Confirm a nonce was used."""
        self.pending_nonces.discard(nonce)
        self.confirmed_nonce = max(self.confirmed_nonce, nonce + 1)
    
    def release(self, nonce: int) -> None:
        """Release a nonce that wasn't used."""
        self.pending_nonces.discard(nonce)


class ExecutionEngine:
    """
    Handles trade execution on Polymarket via Polygon.
    
    Features:
    - Limit order only (no market orders)
    - Nonce management for MEV protection
    - Gas optimization
    - Position tracking
    - Automatic exit handling
    - Pre-trade slippage simulation
    - Enhanced adaptive exit logic
    """
    
    # Exit condition thresholds
    STOP_LOSS_PCT = -0.03  # -3% stop loss
    PARTIAL_EXIT_TRIGGER_PCT = 0.05  # +5% triggers partial exit
    PARTIAL_EXIT_SIZE_PCT = 0.5  # Exit 50% of position
    ORACLE_UPDATE_IMMINENT_AGE = 65  # Exit if oracle age > 65s
    
    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        private_key: str,
        polymarket_feed: Optional["PolymarketFeed"] = None,
        chainlink_feed: Optional["ChainlinkFeed"] = None,
    ):
        self.rpc_url = rpc_url
        self.wallet_address = wallet_address
        self.private_key = private_key
        
        # Feed references for pre-trade checks and position monitoring
        self._polymarket_feed = polymarket_feed
        self._chainlink_feed = chainlink_feed
        
        self.logger = logger.bind(component="execution")
        
        # Web3 instance
        self._w3: Optional[AsyncWeb3] = None
        self._account: Optional[Account] = None
        
        # Nonce tracking
        self._nonce_tracker = NonceTracker()
        
        # Active positions
        self._positions: dict[str, Position] = {}
        
        # Circuit breaker state
        self._consecutive_failed_fills = 0
        self._paused = False
        self._pause_reason: Optional[str] = None
        
        # Stats
        self._total_gas_spent_eur = 0.0
        self._trades_today = 0
    
    def set_feeds(
        self,
        polymarket_feed: Optional["PolymarketFeed"] = None,
        chainlink_feed: Optional["ChainlinkFeed"] = None,
    ) -> None:
        """Set feed references after initialization."""
        if polymarket_feed:
            self._polymarket_feed = polymarket_feed
        if chainlink_feed:
            self._chainlink_feed = chainlink_feed
    
    async def initialize(self) -> bool:
        """Initialize web3 connection."""
        try:
            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.rpc_url))
            
            if not await self._w3.is_connected():
                self.logger.error("Failed to connect to RPC")
                return False
            
            # Setup account
            self._account = Account.from_key(self.private_key)
            
            if self._account.address.lower() != self.wallet_address.lower():
                self.logger.error("Private key doesn't match wallet address")
                return False
            
            # Get initial nonce
            nonce = await self._w3.eth.get_transaction_count(
                self.wallet_address,
                "pending"
            )
            self._nonce_tracker.confirmed_nonce = nonce
            
            self.logger.info(
                "Execution engine initialized",
                wallet=self.wallet_address[:10] + "...",
                initial_nonce=nonce,
            )
            return True
            
        except Exception as e:
            self.logger.error("Failed to initialize", error=str(e))
            return False
    
    async def _get_gas_price(self) -> tuple[int, int]:
        """Get current gas prices (maxFeePerGas, maxPriorityFeePerGas)."""
        try:
            # Get base fee from latest block
            block = await self._w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas", 30_000_000_000)  # 30 gwei default
            
            # Priority fee
            priority_fee = min(
                settings.execution.max_priority_fee_gwei * 10**9,
                35 * 10**9,
            )
            
            # Max fee = 2 * base fee + priority
            max_fee = min(
                2 * base_fee + priority_fee,
                settings.execution.max_fee_per_gas_gwei * 10**9,
            )
            
            return max_fee, priority_fee
            
        except Exception as e:
            self.logger.error("Failed to get gas price", error=str(e))
            return 100 * 10**9, 35 * 10**9  # Safe defaults
    
    def _check_circuit_breakers(self) -> tuple[bool, Optional[str]]:
        """Check if trading should be paused."""
        # Check pause flag
        if self._paused:
            return False, self._pause_reason
        
        # Check consecutive failed fills
        if self._consecutive_failed_fills >= settings.risk.max_consecutive_failed_fills:
            return False, f"Too many failed fills: {self._consecutive_failed_fills}"
        
        # Check gas spending
        if self._total_gas_spent_eur >= settings.risk.max_daily_gas_spend_eur:
            return False, f"Gas limit reached: €{self._total_gas_spent_eur:.2f}"
        
        # Check active positions
        if len(self._positions) >= settings.risk.max_concurrent_positions:
            return False, "Max concurrent positions reached"
        
        return True, None
    
    async def _simulate_pre_trade_slippage(
        self,
        signal: SignalCandidate,
        position_size: float,
    ) -> tuple[bool, float, str]:
        """
        Simulate order fill using FRESH orderbook to check slippage.
        
        Args:
            signal: The signal to execute
            position_size: Size of position in EUR
            
        Returns:
            (can_fill, avg_price, rejection_reason)
        """
        if not self._polymarket_feed:
            # No feed reference - skip simulation
            return True, signal.polymarket.yes_bid if signal.polymarket else 0.0, ""
        
        try:
            # Get FRESH orderbook (not cached)
            live_orderbook = await self._polymarket_feed.get_live_orderbook()
            
            if not live_orderbook.get('bids'):
                return False, 0.0, "No bids in live orderbook"
            
            # Simulate fill
            side = 'YES' if signal.direction.value == 'up' else 'NO'
            fill_result = self._polymarket_feed.simulate_fill(side, position_size)
            
            # Check if we can fill
            if not fill_result.get('can_fill', False):
                unfilled = fill_result.get('unfilled_size', position_size)
                return False, 0.0, f"Cannot fill: {unfilled:.2f}€ unfilled"
            
            # Check slippage
            slippage = fill_result.get('slippage', 1.0)
            if slippage > settings.execution.max_slippage_pct:
                return False, 0.0, f"Slippage too high: {slippage:.2%} > {settings.execution.max_slippage_pct:.2%}"
            
            avg_price = fill_result.get('avg_price', 0.0)
            self.logger.debug(
                "Pre-trade slippage simulation passed",
                avg_price=avg_price,
                slippage=slippage,
                filled_shares=fill_result.get('filled_shares', 0),
            )
            
            return True, avg_price, ""
            
        except Exception as e:
            self.logger.warning("Pre-trade slippage simulation failed", error=str(e))
            # If simulation fails, proceed with caution using cached data
            return True, signal.polymarket.yes_bid if signal.polymarket else 0.0, ""
    
    async def execute_signal(
        self,
        signal: SignalCandidate,
        mode: str,
    ) -> ActionData:
        """
        Execute a trading signal with pre-trade slippage check.
        
        Args:
            signal: Validated signal to execute
            mode: Operating mode (shadow, alert, night_auto)
            
        Returns:
            ActionData with execution details
        """
        self.logger.info(
            "Executing signal",
            signal_id=signal.signal_id,
            mode=mode,
            direction=signal.direction.value,
        )
        
        # Shadow mode - simulate only
        if mode == "shadow":
            return self._simulate_execution(signal)
        
        # Check circuit breakers
        can_trade, reason = self._check_circuit_breakers()
        if not can_trade:
            self.logger.warning("Circuit breaker active", reason=reason)
            return ActionData(
                mode=mode,
                decision=ActionDecision.REJECT,
            )
        
        # Check gas price
        max_fee, priority_fee = await self._get_gas_price()
        gas_price_gwei = max_fee / 10**9
        
        if gas_price_gwei > settings.execution.max_gas_price_gwei:
            self.logger.warning(
                "Gas price too high",
                current=gas_price_gwei,
                max=settings.execution.max_gas_price_gwei,
            )
            return ActionData(
                mode=mode,
                decision=ActionDecision.REJECT,
            )
        
        # Calculate position size
        if mode == "night_auto":
            position_size = settings.risk.night_mode_max_position_eur
        else:
            bankroll = settings.risk.starting_capital_eur
            position_size = bankroll * settings.risk.max_position_pct
        
        # Get entry price (best bid for YES buys)
        if not signal.polymarket:
            return ActionData(mode=mode, decision=ActionDecision.REJECT)
        
        entry_price = signal.polymarket.yes_bid
        
        # For alert mode, just log the opportunity
        if mode == "alert":
            return ActionData(
                mode=mode,
                decision=ActionDecision.ALERT,
                position_size_eur=position_size,
                entry_price=entry_price,
                gas_price_gwei=gas_price_gwei,
            )
        
        # ============================================
        # PRE-TRADE SLIPPAGE SIMULATION
        # Get fresh orderbook and simulate fill before committing
        # ============================================
        can_fill, simulated_price, rejection_reason = await self._simulate_pre_trade_slippage(
            signal=signal,
            position_size=position_size,
        )
        
        if not can_fill:
            self.logger.warning(
                "Pre-trade slippage check failed",
                reason=rejection_reason,
                signal_id=signal.signal_id,
            )
            return ActionData(
                mode=mode,
                decision=ActionDecision.REJECT,
            )
        
        # Use simulated price if available (more accurate than cached)
        if simulated_price > 0:
            entry_price = simulated_price
        
        # Night auto mode - actually execute
        return await self._execute_trade(
            signal=signal,
            position_size=position_size,
            entry_price=entry_price,
            max_fee=max_fee,
            priority_fee=priority_fee,
        )
    
    def _simulate_execution(self, signal: SignalCandidate) -> ActionData:
        """Simulate execution for shadow mode."""
        if not signal.polymarket:
            return ActionData(mode="shadow", decision=ActionDecision.SHADOW)
        
        position_size = 20.0  # Simulated size
        entry_price = signal.polymarket.yes_bid
        
        return ActionData(
            mode="shadow",
            decision=ActionDecision.SHADOW,
            position_size_eur=position_size,
            entry_price=entry_price,
            gas_price_gwei=35.0,  # Estimated
            gas_cost_eur=0.30,  # Estimated
        )
    
    async def _execute_trade(
        self,
        signal: SignalCandidate,
        position_size: float,
        entry_price: float,
        max_fee: int,
        priority_fee: int,
    ) -> ActionData:
        """Execute actual trade on-chain with enhanced position tracking."""
        start_time = time.time()
        
        try:
            # Get nonce
            chain_nonce = await self._w3.eth.get_transaction_count(
                self.wallet_address,
                "pending"
            )
            nonce = self._nonce_tracker.get_next(chain_nonce)
            
            # Calculate initial mispricing for adaptive exit logic
            initial_mispricing = 0.0
            if signal.polymarket and signal.oracle and signal.consensus:
                # Mispricing = divergence between spot-implied and PM-implied probability
                spot_oracle_div = (signal.consensus.consensus_price - signal.oracle.current_value) / signal.oracle.current_value
                spot_implied = 0.5 + (spot_oracle_div * 5)  # Simplified model
                spot_implied = max(0, min(1, spot_implied))
                initial_mispricing = abs(spot_implied - signal.polymarket.implied_probability)
            
            # Get oracle age at entry
            initial_oracle_age = signal.oracle.oracle_age_seconds if signal.oracle else 0.0
            
            # Build transaction
            # NOTE: This is a placeholder - actual implementation would use
            # Polymarket's CLOB API for order placement
            
            # For MVP, we'll log what we would do
            self.logger.info(
                "Would execute trade",
                signal_id=signal.signal_id,
                market_id=signal.market_id,
                direction=signal.direction.value,
                size_eur=position_size,
                price=entry_price,
                nonce=nonce,
                initial_mispricing=initial_mispricing,
                oracle_age=initial_oracle_age,
            )
            
            # Create position tracking with enhanced fields
            position = Position(
                signal_id=signal.signal_id,
                market_id=signal.market_id,
                direction=signal.direction.value,
                entry_price=entry_price,
                size_eur=position_size,
                entry_time_ms=int(time.time() * 1000),
                initial_mispricing=initial_mispricing,
                initial_oracle_age=initial_oracle_age,
                remaining_size=position_size,  # Full size initially
            )
            
            # Simulate order submission
            # In production, this would call Polymarket's API
            await asyncio.sleep(0.1)  # Simulate network latency
            
            # Confirm nonce
            self._nonce_tracker.confirm(nonce)
            
            # Track position
            self._positions[signal.signal_id] = position
            self._trades_today += 1
            
            # Calculate execution time
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            # Estimate gas cost
            gas_used = 120000  # Estimated
            gas_cost_eur = (gas_used * max_fee / 10**18) * 0.50  # Assuming MATIC ~$0.50
            self._total_gas_spent_eur += gas_cost_eur
            
            return ActionData(
                mode="night_auto",
                decision=ActionDecision.TRADE,
                order_id=f"sim-{signal.signal_id[:8]}",
                position_size_eur=position_size,
                entry_price=entry_price,
                gas_used=gas_used,
                gas_price_gwei=max_fee / 10**9,
                gas_cost_eur=gas_cost_eur,
                fill_delay_ms=execution_time_ms,
                nonce=nonce,
            )
            
        except Exception as e:
            self.logger.error("Trade execution failed", error=str(e))
            self._consecutive_failed_fills += 1
            return ActionData(
                mode="night_auto",
                decision=ActionDecision.REJECT,
            )
    
    def _calculate_adaptive_take_profit(
        self,
        position: Position,
        current_oracle_age: float,
    ) -> float:
        """
        Calculate adaptive take profit target based on conditions.
        
        Lower TP when oracle is stale (exit faster)
        Raise TP when initial mispricing was high (more potential)
        """
        base_tp = settings.execution.take_profit_pct  # 8% default
        
        # If oracle is very stale (>50s), lower TP to exit faster
        if current_oracle_age > 50:
            base_tp *= 0.7  # Reduce to ~5.6%
        
        # If initial mispricing was high (>5%), raise TP
        if position.initial_mispricing > 0.05:
            base_tp *= 1.3  # Increase to ~10.4%
        
        return base_tp
    
    async def manage_position(
        self,
        signal_id: str,
        current_price: float,
        spread: float,
        oracle_age: Optional[float] = None,
    ) -> Optional[OutcomeData]:
        """
        Enhanced position management with adaptive exit logic.
        
        Exit conditions (in priority order):
        1. Oracle update imminent (age > 65s)
        2. Spread converged (market corrected)
        3. Adaptive take profit (based on oracle age + initial mispricing)
        4. Stop loss (-3%)
        5. Partial exit at +5% (locks in gains)
        6. Time-based exit (90s)
        7. Emergency exit (120s)
        
        Args:
            signal_id: ID of the signal/position
            current_price: Current market price
            spread: Current bid-ask spread
            oracle_age: Current oracle age in seconds (optional, will fetch if not provided)
            
        Returns:
            OutcomeData if position was closed, None otherwise
        """
        position = self._positions.get(signal_id)
        if not position:
            return None
        
        now_ms = int(time.time() * 1000)
        position_age_s = (now_ms - position.entry_time_ms) / 1000
        
        # Get oracle age if not provided
        if oracle_age is None and self._chainlink_feed:
            oracle_data = self._chainlink_feed.get_data()
            oracle_age = oracle_data.oracle_age_seconds if oracle_data else 0.0
        oracle_age = oracle_age or 0.0
        
        # Calculate current P&L
        profit_pct = (current_price - position.entry_price) / position.entry_price
        
        # Track max adverse move
        if current_price < position.entry_price:
            adverse_move = (position.entry_price - current_price) / position.entry_price
            position.max_adverse_move = max(position.max_adverse_move, adverse_move)
        
        # ============================================
        # EXIT CONDITION 1: Oracle Update Imminent (HIGHEST PRIORITY)
        # Exit before oracle updates to avoid adverse price movement
        # ============================================
        if oracle_age > self.ORACLE_UPDATE_IMMINENT_AGE:
            self.logger.info(
                "Exiting: Oracle update imminent",
                oracle_age=oracle_age,
                threshold=self.ORACLE_UPDATE_IMMINENT_AGE,
            )
            return await self._close_position(position, current_price, ExitReason.TIME_EXIT, "oracle_imminent")
        
        # ============================================
        # EXIT CONDITION 2: Spread Converged (market already corrected)
        # ============================================
        if spread < settings.execution.take_profit_spread_threshold:
            self.logger.info(
                "Exiting: Spread converged",
                spread=spread,
                threshold=settings.execution.take_profit_spread_threshold,
            )
            return await self._close_position(position, current_price, ExitReason.SPREAD_CONVERGED)
        
        # ============================================
        # EXIT CONDITION 3: Adaptive Take Profit
        # Adjusts target based on oracle age and initial mispricing
        # ============================================
        adaptive_tp = self._calculate_adaptive_take_profit(position, oracle_age)
        if profit_pct >= adaptive_tp:
            self.logger.info(
                "Exiting: Adaptive take profit hit",
                profit_pct=profit_pct,
                target=adaptive_tp,
            )
            return await self._close_position(position, current_price, ExitReason.TAKE_PROFIT)
        
        # ============================================
        # EXIT CONDITION 4: Stop Loss (-3%)
        # Protect against adverse moves
        # ============================================
        if profit_pct < self.STOP_LOSS_PCT:
            self.logger.warning(
                "Exiting: Stop loss hit",
                profit_pct=profit_pct,
                stop_loss=self.STOP_LOSS_PCT,
            )
            return await self._close_position(position, current_price, ExitReason.STOP_LOSS)
        
        # ============================================
        # EXIT CONDITION 5: Partial Exit at +5%
        # Lock in gains on half the position
        # ============================================
        if profit_pct >= self.PARTIAL_EXIT_TRIGGER_PCT and not position.partial_exit_done:
            self.logger.info(
                "Taking partial profit",
                profit_pct=profit_pct,
                exit_size_pct=self.PARTIAL_EXIT_SIZE_PCT,
            )
            await self._partial_exit(position, current_price, self.PARTIAL_EXIT_SIZE_PCT)
            # Don't return - continue monitoring remaining position
        
        # ============================================
        # EXIT CONDITION 6: Time-Based Exit (90s)
        # ============================================
        if position_age_s > settings.execution.time_based_exit_seconds:
            self.logger.info(
                "Exiting: Time limit reached",
                position_age=position_age_s,
            )
            return await self._close_position(position, current_price, ExitReason.TIME_EXIT)
        
        # ============================================
        # EXIT CONDITION 7: Emergency Exit (120s)
        # ============================================
        if position_age_s > settings.execution.max_position_duration_seconds:
            self.logger.warning(
                "EMERGENCY EXIT: Max position duration exceeded",
                position_age=position_age_s,
            )
            return await self._close_position(position, current_price, ExitReason.EMERGENCY)
        
        return None
    
    async def _partial_exit(
        self,
        position: Position,
        current_price: float,
        exit_pct: float,
    ) -> None:
        """
        Execute partial exit to lock in gains.
        
        Args:
            position: The position to partially close
            current_price: Current market price
            exit_pct: Percentage of position to exit (0.0 - 1.0)
        """
        exit_size = position.remaining_size * exit_pct
        
        # Update position tracking
        position.partial_exit_done = True
        position.partial_exit_size = exit_size
        position.remaining_size -= exit_size
        
        # Calculate partial P&L
        partial_profit = (current_price - position.entry_price) * exit_size / position.entry_price
        
        self.logger.info(
            "Partial exit executed",
            signal_id=position.signal_id,
            exit_size=exit_size,
            remaining_size=position.remaining_size,
            partial_profit=partial_profit,
        )
        
        # In production, this would call Polymarket's API to close part of the position
    
    async def _close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: ExitReason,
        notes: str = "",
    ) -> OutcomeData:
        """
        Close an open position (full or remaining after partial exit).
        
        Args:
            position: The position to close
            exit_price: Exit price
            exit_reason: Reason for exit
            notes: Additional notes about the exit
        """
        self.logger.info(
            "Closing position",
            signal_id=position.signal_id,
            exit_price=exit_price,
            reason=exit_reason.value,
            notes=notes,
            remaining_size=position.remaining_size,
        )
        
        # Use remaining size (accounts for partial exits)
        effective_size = position.remaining_size if position.remaining_size > 0 else position.size_eur
        
        # Calculate P&L for remaining position
        gross_profit = (exit_price - position.entry_price) * effective_size / position.entry_price
        
        # Add partial exit profit if applicable
        if position.partial_exit_done and position.partial_exit_size > 0:
            partial_profit = (exit_price - position.entry_price) * position.partial_exit_size / position.entry_price
            gross_profit += partial_profit
        
        gas_cost = 0.30  # Estimated exit gas
        net_profit = gross_profit - gas_cost
        profit_pct = (exit_price - position.entry_price) / position.entry_price
        
        # Calculate position duration
        now_ms = int(time.time() * 1000)
        duration_s = (now_ms - position.entry_time_ms) / 1000
        
        # Remove from active positions
        del self._positions[position.signal_id]
        
        # Reset consecutive failures on successful close
        if net_profit > 0:
            self._consecutive_failed_fills = 0
        
        # Build notes string
        exit_notes = notes
        if position.partial_exit_done:
            exit_notes += f" (partial_exit: {position.partial_exit_size:.2f}€)"
        
        return OutcomeData(
            filled=True,
            fill_price=position.entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_profit_eur=gross_profit,
            net_profit_eur=net_profit,
            profit_pct=profit_pct,
            position_duration_seconds=duration_s,
            max_adverse_move_pct=position.max_adverse_move,
            notes=exit_notes.strip(),
        )
    
    def pause(self, reason: str) -> None:
        """Pause trading."""
        self._paused = True
        self._pause_reason = reason
        self.logger.warning("Trading paused", reason=reason)
    
    def resume(self) -> None:
        """Resume trading."""
        self._paused = False
        self._pause_reason = None
        self.logger.info("Trading resumed")
    
    def reset_daily_stats(self) -> None:
        """Reset daily statistics."""
        self._total_gas_spent_eur = 0.0
        self._trades_today = 0
        self._consecutive_failed_fills = 0
    
    def get_metrics(self) -> dict:
        """Get execution engine metrics."""
        return {
            "connected": self._w3 is not None and self._w3.is_connected() if self._w3 else False,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "active_positions": len(self._positions),
            "trades_today": self._trades_today,
            "gas_spent_today_eur": self._total_gas_spent_eur,
            "consecutive_failed_fills": self._consecutive_failed_fills,
        }

