"""
Base mode class for operating modes.
"""

from abc import ABC, abstractmethod
from typing import Optional

import structlog

from src.models.schemas import SignalCandidate, ActionData, OutcomeData

logger = structlog.get_logger()


class BaseMode(ABC):
    """
    Abstract base class for operating modes.
    
    Modes:
    - Shadow: Simulate trades, log everything
    - Alert: Send notifications for human decision
    - Night Auto: Automated conservative trading
    """
    
    def __init__(self, name: str):
        self.name = name
        self.logger = logger.bind(mode=name)
        self._active = False
    
    @property
    def is_active(self) -> bool:
        """Check if mode is currently active."""
        return self._active
    
    def activate(self) -> None:
        """Activate this mode."""
        self._active = True
        self.logger.info("Mode activated")
    
    def deactivate(self) -> None:
        """Deactivate this mode."""
        self._active = False
        self.logger.info("Mode deactivated")
    
    @abstractmethod
    async def process_signal(
        self,
        signal: SignalCandidate,
        asset: str = "BTC",
    ) -> tuple[ActionData, Optional[OutcomeData]]:
        """
        Process a validated signal according to mode rules.
        
        Args:
            signal: Validated signal candidate
            
        Returns:
            Tuple of (ActionData, optional OutcomeData)
        """
        pass
    
    @abstractmethod
    def should_process(self, signal: SignalCandidate) -> bool:
        """
        Check if this mode should process the signal.
        
        Args:
            signal: Signal candidate to check
            
        Returns:
            True if mode should handle this signal
        """
        pass
    
    @abstractmethod
    def get_metrics(self) -> dict:
        """Get mode-specific metrics."""
        pass

