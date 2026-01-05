"""
Execution Engine for trade management.
Handles order placement, nonce management, and position tracking.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

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
    """Active trading position."""
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
    """
    
    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        private_key: str,
    ):
        self.rpc_url = rpc_url
        self.wallet_address = wallet_address
        self.private_key = private_key
        
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
    
    async def execute_signal(
        self,
        signal: SignalCandidate,
        mode: str,
    ) -> ActionData:
        """
        Execute a trading signal.
        
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
        """Execute actual trade on-chain."""
        start_time = time.time()
        
        try:
            # Get nonce
            chain_nonce = await self._w3.eth.get_transaction_count(
                self.wallet_address,
                "pending"
            )
            nonce = self._nonce_tracker.get_next(chain_nonce)
            
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
            )
            
            # Create position tracking
            position = Position(
                signal_id=signal.signal_id,
                market_id=signal.market_id,
                direction=signal.direction.value,
                entry_price=entry_price,
                size_eur=position_size,
                entry_time_ms=int(time.time() * 1000),
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
    
    async def manage_position(
        self,
        signal_id: str,
        current_price: float,
        spread: float,
    ) -> Optional[OutcomeData]:
        """
        Manage an open position (exit logic).
        
        Args:
            signal_id: ID of the signal/position
            current_price: Current market price
            spread: Current bid-ask spread
            
        Returns:
            OutcomeData if position was closed, None otherwise
        """
        position = self._positions.get(signal_id)
        if not position:
            return None
        
        now_ms = int(time.time() * 1000)
        position_age_s = (now_ms - position.entry_time_ms) / 1000
        
        # Track max adverse move
        if current_price < position.entry_price:
            adverse_move = (position.entry_price - current_price) / position.entry_price
            position.max_adverse_move = max(position.max_adverse_move, adverse_move)
        
        # Check exit conditions
        exit_reason = None
        
        # 1. Spread convergence
        if spread < settings.execution.take_profit_spread_threshold:
            exit_reason = ExitReason.SPREAD_CONVERGED
        
        # 2. Take profit
        profit_pct = (current_price - position.entry_price) / position.entry_price
        if profit_pct >= settings.execution.take_profit_pct:
            exit_reason = ExitReason.TAKE_PROFIT
        
        # 3. Time-based exit
        if position_age_s > settings.execution.time_based_exit_seconds:
            exit_reason = ExitReason.TIME_EXIT
        
        # 4. Emergency exit (max duration)
        if position_age_s > settings.execution.max_position_duration_seconds:
            exit_reason = ExitReason.EMERGENCY
        
        if exit_reason:
            return await self._close_position(position, current_price, exit_reason)
        
        return None
    
    async def _close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> OutcomeData:
        """Close an open position."""
        self.logger.info(
            "Closing position",
            signal_id=position.signal_id,
            exit_price=exit_price,
            reason=exit_reason.value,
        )
        
        # Calculate P&L
        gross_profit = (exit_price - position.entry_price) * position.size_eur / position.entry_price
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

