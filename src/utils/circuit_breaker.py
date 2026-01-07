"""
Circuit Breaker for risk management.

Automatically pauses trading when daily losses exceed threshold.
Prevents catastrophic losses during adverse market conditions.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

import structlog

logger = structlog.get_logger()


@dataclass
class CircuitBreakerState:
    """Current state of the circuit breaker."""
    is_tripped: bool = False
    trip_time: Optional[datetime] = None
    trip_reason: str = ""
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_wins: int = 0
    daily_losses: int = 0
    last_reset: datetime = None
    
    def __post_init__(self):
        if self.last_reset is None:
            self.last_reset = datetime.now(timezone.utc)


class CircuitBreaker:
    """
    Trading circuit breaker that pauses trading on excessive losses.
    
    Features:
    - Daily loss limit (-2% default)
    - Automatic midnight UTC reset
    - Cooldown period after trip (1 hour)
    - Discord alerts on trip/reset
    """
    
    def __init__(
        self,
        daily_loss_limit: float = -0.02,  # -2% max daily loss
        cooldown_seconds: int = 3600,  # 1 hour cooldown after trip
        balance: float = 1000.0,  # Starting balance for % calculation
        on_trip_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.daily_loss_limit = daily_loss_limit
        self.cooldown_seconds = cooldown_seconds
        self.balance = balance
        self.on_trip_callback = on_trip_callback
        
        self.state = CircuitBreakerState()
        self.logger = logger.bind(component="circuit_breaker")
        
        self.logger.info(
            "Circuit breaker initialized",
            daily_loss_limit=f"{daily_loss_limit:.1%}",
            cooldown_seconds=cooldown_seconds,
            balance=f"€{balance:.2f}",
        )
    
    def _should_reset_daily(self) -> bool:
        """Check if we should reset daily counters (midnight UTC)."""
        now = datetime.now(timezone.utc)
        return now.date() > self.state.last_reset.date()
    
    def _reset_daily(self) -> None:
        """Reset daily counters."""
        old_pnl = self.state.daily_pnl
        
        self.state.daily_pnl = 0.0
        self.state.daily_trades = 0
        self.state.daily_wins = 0
        self.state.daily_losses = 0
        self.state.last_reset = datetime.now(timezone.utc)
        
        # Also reset trip if cooldown passed
        if self.state.is_tripped:
            self.state.is_tripped = False
            self.state.trip_time = None
            self.state.trip_reason = ""
            self.logger.info("Circuit breaker reset after daily rollover")
        
        self.logger.info(
            "Daily counters reset",
            previous_pnl=f"€{old_pnl:.2f}",
        )
    
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        # Reset daily counters if needed
        if self._should_reset_daily():
            self._reset_daily()
        
        # Check cooldown
        if self.state.is_tripped and self.state.trip_time:
            elapsed = (datetime.now(timezone.utc) - self.state.trip_time).total_seconds()
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                self.logger.debug(
                    "Trading paused - circuit breaker active",
                    remaining=f"{remaining/60:.1f} min",
                )
                return False
            else:
                # Cooldown passed, reset
                self.logger.info("Circuit breaker cooldown complete, trading resumed")
                self.state.is_tripped = False
                self.state.trip_time = None
                self.state.trip_reason = ""
        
        return not self.state.is_tripped
    
    def record_trade(self, pnl: float, is_virtual: bool = True) -> bool:
        """
        Record a trade result and check if circuit breaker should trip.
        
        Args:
            pnl: Profit/loss in EUR
            is_virtual: Whether this is a virtual trade
            
        Returns:
            True if trading should continue, False if circuit breaker tripped
        """
        # Reset daily if needed
        if self._should_reset_daily():
            self._reset_daily()
        
        # Update counters
        self.state.daily_pnl += pnl
        self.state.daily_trades += 1
        
        if pnl > 0:
            self.state.daily_wins += 1
        else:
            self.state.daily_losses += 1
        
        # Calculate loss percentage
        loss_pct = self.state.daily_pnl / self.balance
        
        self.logger.debug(
            "Trade recorded",
            pnl=f"€{pnl:.2f}",
            daily_pnl=f"€{self.state.daily_pnl:.2f}",
            loss_pct=f"{loss_pct:.2%}",
            is_virtual=is_virtual,
        )
        
        # Check if we should trip
        if loss_pct < self.daily_loss_limit and not self.state.is_tripped:
            return self._trip(f"Daily loss limit exceeded: {loss_pct:.2%}")
        
        return True
    
    def _trip(self, reason: str) -> bool:
        """Trip the circuit breaker."""
        self.state.is_tripped = True
        self.state.trip_time = datetime.now(timezone.utc)
        self.state.trip_reason = reason
        
        self.logger.critical(
            "🚨 CIRCUIT BREAKER TRIPPED",
            reason=reason,
            daily_pnl=f"€{self.state.daily_pnl:.2f}",
            daily_trades=self.state.daily_trades,
            win_rate=f"{self.state.daily_wins / max(1, self.state.daily_trades):.1%}",
            cooldown=f"{self.cooldown_seconds / 60:.0f} min",
        )
        
        # Fire callback if set
        if self.on_trip_callback:
            asyncio.create_task(self.on_trip_callback(reason))
        
        return False
    
    def manual_trip(self, reason: str = "Manual trip") -> None:
        """Manually trip the circuit breaker."""
        self._trip(reason)
    
    def manual_reset(self) -> None:
        """Manually reset the circuit breaker."""
        self.state.is_tripped = False
        self.state.trip_time = None
        self.state.trip_reason = ""
        self.logger.info("Circuit breaker manually reset")
    
    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        loss_pct = self.state.daily_pnl / self.balance if self.balance > 0 else 0
        
        remaining_cooldown = 0
        if self.state.is_tripped and self.state.trip_time:
            elapsed = (datetime.now(timezone.utc) - self.state.trip_time).total_seconds()
            remaining_cooldown = max(0, self.cooldown_seconds - elapsed)
        
        return {
            "is_tripped": self.state.is_tripped,
            "trip_reason": self.state.trip_reason,
            "daily_pnl": self.state.daily_pnl,
            "daily_pnl_pct": loss_pct,
            "daily_trades": self.state.daily_trades,
            "daily_wins": self.state.daily_wins,
            "daily_losses": self.state.daily_losses,
            "win_rate": self.state.daily_wins / max(1, self.state.daily_trades),
            "loss_limit": self.daily_loss_limit,
            "remaining_cooldown_seconds": remaining_cooldown,
            "last_reset": self.state.last_reset.isoformat() if self.state.last_reset else None,
        }


# Global instance for easy access
_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> Optional[CircuitBreaker]:
    """Get the global circuit breaker instance."""
    return _circuit_breaker


def init_circuit_breaker(
    daily_loss_limit: float = -0.02,
    cooldown_seconds: int = 3600,
    balance: float = 1000.0,
    on_trip_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> CircuitBreaker:
    """Initialize the global circuit breaker."""
    global _circuit_breaker
    _circuit_breaker = CircuitBreaker(
        daily_loss_limit=daily_loss_limit,
        cooldown_seconds=cooldown_seconds,
        balance=balance,
        on_trip_callback=on_trip_callback,
    )
    return _circuit_breaker

