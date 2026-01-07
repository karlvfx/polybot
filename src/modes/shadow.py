"""
Shadow Mode - Simulate trades and collect data.
"""

import time
from typing import Optional

import structlog

from src.modes.base import BaseMode
from src.models.schemas import (
    SignalCandidate,
    ActionData,
    OutcomeData,
    ActionDecision,
    ExitReason,
)
from config.settings import settings

logger = structlog.get_logger()


class ShadowMode(BaseMode):
    """
    Shadow trading mode for backtesting and data collection.
    
    Purpose:
    - Simulate all trades without risk
    - Collect comprehensive logs
    - Track would-be performance
    - Build oracle lag distribution
    
    Run for 2-4 weeks before live trading.
    """
    
    def __init__(self):
        super().__init__("shadow")
        
        # Simulated positions
        self._positions: dict[str, dict] = {}
        
        # Statistics
        self._signals_processed = 0
        self._would_be_wins = 0
        self._would_be_losses = 0
        self._total_would_be_profit = 0.0
        self._total_would_be_gas = 0.0
        
        # Oracle timing data
        self._oracle_update_delays: list[float] = []
    
    def should_process(self, signal: SignalCandidate) -> bool:
        """Shadow mode processes all signals."""
        return True
    
    async def process_signal(
        self,
        signal: SignalCandidate,
        asset: str = "BTC",
    ) -> tuple[ActionData, Optional[OutcomeData]]:
        """
        Simulate trade execution and record results.
        """
        self._signals_processed += 1
        
        if not signal.polymarket or not signal.scoring:
            return ActionData(
                mode="shadow",
                decision=ActionDecision.SHADOW,
            ), None
        
        # Simulate position
        entry_price = signal.polymarket.yes_bid
        position_size = 20.0  # Simulated €20 position
        estimated_gas = 0.30
        
        # Create simulated position
        position_id = signal.signal_id
        self._positions[position_id] = {
            "signal_id": signal.signal_id,
            "entry_price": entry_price,
            "entry_time_ms": signal.timestamp_ms,
            "size_eur": position_size,
            "direction": signal.direction.value,
            "oracle_age_at_entry": signal.oracle.oracle_age_seconds if signal.oracle else 0,
        }
        
        self.logger.info(
            "Shadow position opened",
            signal_id=signal.signal_id,
            direction=signal.direction.value,
            entry_price=entry_price,
            confidence=signal.scoring.confidence,
        )
        
        action = ActionData(
            mode="shadow",
            decision=ActionDecision.SHADOW,
            position_size_eur=position_size,
            entry_price=entry_price,
            gas_cost_eur=estimated_gas,
        )
        
        return action, None
    
    def simulate_exit(
        self,
        signal_id: str,
        exit_price: float,
        oracle_updated_at_ms: Optional[int] = None,
    ) -> Optional[OutcomeData]:
        """
        Simulate closing a shadow position.
        
        Args:
            signal_id: ID of the position to close
            exit_price: Price at exit
            oracle_updated_at_ms: When oracle actually updated
            
        Returns:
            OutcomeData with simulated results
        """
        position = self._positions.get(signal_id)
        if not position:
            return None
        
        entry_price = position["entry_price"]
        size = position["size_eur"]
        entry_time = position["entry_time_ms"]
        
        now_ms = int(time.time() * 1000)
        duration_s = (now_ms - entry_time) / 1000
        
        # Calculate P&L
        gross_profit = (exit_price - entry_price) * size / entry_price
        gas_cost = 0.60  # Entry + exit gas
        net_profit = gross_profit - gas_cost
        profit_pct = (exit_price - entry_price) / entry_price
        
        # Track oracle timing
        if oracle_updated_at_ms:
            oracle_delay = (oracle_updated_at_ms - entry_time) / 1000
            self._oracle_update_delays.append(oracle_delay)
        
        # Update statistics
        if net_profit > 0:
            self._would_be_wins += 1
        else:
            self._would_be_losses += 1
        
        self._total_would_be_profit += net_profit
        self._total_would_be_gas += gas_cost
        
        # Remove position
        del self._positions[signal_id]
        
        # Determine exit reason
        if profit_pct >= 0.08:
            exit_reason = ExitReason.TAKE_PROFIT
        elif duration_s > 90:
            exit_reason = ExitReason.TIME_EXIT
        else:
            exit_reason = ExitReason.SPREAD_CONVERGED
        
        self.logger.info(
            "Shadow position closed",
            signal_id=signal_id,
            net_profit=net_profit,
            duration_s=duration_s,
            exit_reason=exit_reason.value,
        )
        
        return OutcomeData(
            filled=True,
            fill_price=entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_profit_eur=gross_profit,
            net_profit_eur=net_profit,
            profit_pct=profit_pct,
            oracle_updated_at_ms=oracle_updated_at_ms,
            oracle_update_delay_after_signal_s=self._oracle_update_delays[-1] if self._oracle_update_delays else 0,
            position_duration_seconds=duration_s,
        )
    
    def get_win_rate(self) -> float:
        """Get current would-be win rate."""
        total = self._would_be_wins + self._would_be_losses
        if total == 0:
            return 0.0
        return self._would_be_wins / total
    
    def get_avg_profit(self) -> float:
        """Get average profit per trade."""
        total = self._would_be_wins + self._would_be_losses
        if total == 0:
            return 0.0
        return self._total_would_be_profit / total
    
    def get_oracle_timing_stats(self) -> dict:
        """Get oracle timing distribution statistics."""
        if not self._oracle_update_delays:
            return {
                "count": 0,
                "mean": 0,
                "median": 0,
                "p90": 0,
            }
        
        sorted_delays = sorted(self._oracle_update_delays)
        n = len(sorted_delays)
        
        return {
            "count": n,
            "mean": sum(sorted_delays) / n,
            "median": sorted_delays[n // 2],
            "p90": sorted_delays[int(n * 0.9)] if n >= 10 else sorted_delays[-1],
            "min": sorted_delays[0],
            "max": sorted_delays[-1],
        }
    
    def get_metrics(self) -> dict:
        """Get comprehensive shadow mode metrics."""
        oracle_stats = self.get_oracle_timing_stats()
        
        return {
            "signals_processed": self._signals_processed,
            "active_positions": len(self._positions),
            "would_be_wins": self._would_be_wins,
            "would_be_losses": self._would_be_losses,
            "win_rate": self.get_win_rate(),
            "total_profit": self._total_would_be_profit,
            "total_gas": self._total_would_be_gas,
            "net_profit": self._total_would_be_profit - self._total_would_be_gas,
            "avg_profit_per_trade": self.get_avg_profit(),
            "oracle_timing": oracle_stats,
            "meets_target_win_rate": self.get_win_rate() >= settings.target_win_rate,
            "meets_target_profit": self.get_avg_profit() >= settings.target_avg_profit_eur,
        }
    
    def generate_report(self) -> str:
        """Generate human-readable performance report."""
        metrics = self.get_metrics()
        oracle = metrics["oracle_timing"]
        
        report = f"""
╔══════════════════════════════════════════════════════════════╗
║             SHADOW MODE PERFORMANCE REPORT                   ║
╠══════════════════════════════════════════════════════════════╣
║ Signals Processed:  {metrics['signals_processed']:>6}                                ║
║ Active Positions:   {metrics['active_positions']:>6}                                ║
╠══════════════════════════════════════════════════════════════╣
║ WOULD-BE RESULTS                                             ║
║ ──────────────────                                           ║
║ Wins:              {metrics['would_be_wins']:>6}                                 ║
║ Losses:            {metrics['would_be_losses']:>6}                                 ║
║ Win Rate:          {metrics['win_rate']*100:>6.1f}%  {'✓' if metrics['meets_target_win_rate'] else '✗'} (target: 65%)          ║
║ Net Profit:        €{metrics['net_profit']:>6.2f}                              ║
║ Avg Profit/Trade:  €{metrics['avg_profit_per_trade']:>6.2f}  {'✓' if metrics['meets_target_profit'] else '✗'} (target: €1.50)      ║
╠══════════════════════════════════════════════════════════════╣
║ ORACLE TIMING (seconds)                                      ║
║ ──────────────────────                                       ║
║ Sample Size:       {oracle['count']:>6}                                 ║
║ Mean Delay:        {oracle['mean']:>6.1f}s                                ║
║ Median Delay:      {oracle['median']:>6.1f}s                                ║
║ P90 Delay:         {oracle['p90']:>6.1f}s                                ║
╚══════════════════════════════════════════════════════════════╝
"""
        return report

