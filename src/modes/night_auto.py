"""
Night Auto Mode - Conservative automated trading.
"""

import time
from datetime import datetime
from typing import Optional

import structlog

from src.modes.base import BaseMode
from src.models.schemas import (
    SignalCandidate,
    ActionData,
    OutcomeData,
    ActionDecision,
)
from src.engine.execution import ExecutionEngine
from src.utils.alerts import DiscordAlerter
from config.settings import settings

logger = structlog.get_logger()


class NightAutoMode(BaseMode):
    """
    Night Auto trading mode for conservative automated execution.
    
    Active hours: 02:00 - 06:00 local time (configurable)
    
    Features:
    - Strict entry rules (higher confidence threshold)
    - Fixed position sizing (€20)
    - Maximum 2 trades per night
    - Daily loss cap (€40)
    - Circuit breakers for consecutive losses
    
    Safety:
    - Requires 85%+ confidence
    - Only during low-competition hours
    - Automatic pause on errors
    """
    
    def __init__(
        self,
        execution_engine: ExecutionEngine,
        discord_webhook_url: Optional[str] = None,
    ):
        super().__init__("night_auto")
        
        self._execution = execution_engine
        
        # Discord for trade notifications
        webhook_url = discord_webhook_url or settings.alerts.discord_webhook_url
        self._alerter = DiscordAlerter(webhook_url) if webhook_url else None
        
        # Night session tracking
        self._session_date: Optional[str] = None
        self._trades_this_session = 0
        self._loss_this_session = 0.0
        self._consecutive_losses = 0
        
        # Trade history
        self._trades: list[dict] = []
        
        # Pause state
        self._paused = False
        self._pause_reason: Optional[str] = None
    
    def _is_night_hours(self) -> bool:
        """Check if current time is within night trading hours."""
        now = datetime.now()
        hour = now.hour
        
        start_hour = settings.risk.night_mode_start_hour
        end_hour = settings.risk.night_mode_end_hour
        
        return start_hour <= hour < end_hour
    
    def _check_session(self) -> None:
        """Check and reset session if new night."""
        today = datetime.now().strftime("%Y-%m-%d")
        
        if self._session_date != today:
            self._session_date = today
            self._trades_this_session = 0
            self._loss_this_session = 0.0
            self._consecutive_losses = 0
            self._paused = False
            self._pause_reason = None
            self.logger.info("New night session started", date=today)
    
    def should_process(self, signal: SignalCandidate) -> bool:
        """
        Check if signal meets night auto criteria.
        """
        # Check night hours
        if not self._is_night_hours():
            return False
        
        # Update session
        self._check_session()
        
        # Check pause state
        if self._paused:
            self.logger.debug("Night auto paused", reason=self._pause_reason)
            return False
        
        # Check trade limit
        if self._trades_this_session >= settings.risk.night_mode_max_trades:
            self.logger.debug("Night trade limit reached")
            return False
        
        # Check loss limit
        if self._loss_this_session >= settings.risk.night_mode_max_loss_eur:
            self.logger.debug("Night loss limit reached")
            return False
        
        # Check confidence threshold
        if not signal.scoring:
            return False
        
        if signal.scoring.confidence < settings.risk.night_mode_min_confidence:
            return False
        
        # Check execution engine status
        exec_metrics = self._execution.get_metrics()
        if exec_metrics.get("paused"):
            return False
        
        if exec_metrics.get("active_positions", 0) > 0:
            return False
        
        return True
    
    async def process_signal(
        self,
        signal: SignalCandidate,
    ) -> tuple[ActionData, Optional[OutcomeData]]:
        """
        Execute trade in night auto mode.
        """
        self._check_session()
        
        if not signal.scoring or not signal.polymarket:
            return ActionData(mode="night_auto", decision=ActionDecision.REJECT), None
        
        self.logger.info(
            "Night auto processing signal",
            signal_id=signal.signal_id,
            confidence=signal.scoring.confidence,
        )
        
        # Execute trade
        action = await self._execution.execute_signal(signal, "night_auto")
        
        if action.decision == ActionDecision.TRADE:
            self._trades_this_session += 1
            
            # Send trade notification
            await self._notify_trade_opened(signal, action)
            
            self.logger.info(
                "Night auto trade executed",
                signal_id=signal.signal_id,
                entry_price=action.entry_price,
                size=action.position_size_eur,
            )
        
        return action, None
    
    def record_outcome(
        self,
        signal_id: str,
        outcome: OutcomeData,
    ) -> None:
        """
        Record trade outcome and update session stats.
        """
        self._trades.append({
            "signal_id": signal_id,
            "timestamp_ms": int(time.time() * 1000),
            "profit_eur": outcome.net_profit_eur,
            "exit_reason": outcome.exit_reason.value if outcome.exit_reason else None,
        })
        
        # Update session stats
        if outcome.net_profit_eur < 0:
            self._loss_this_session += abs(outcome.net_profit_eur)
            self._consecutive_losses += 1
            
            # Check circuit breaker
            if self._consecutive_losses >= 2:
                self._pause("2 consecutive losses")
        else:
            self._consecutive_losses = 0
        
        self.logger.info(
            "Night auto outcome recorded",
            signal_id=signal_id,
            profit=outcome.net_profit_eur,
            consecutive_losses=self._consecutive_losses,
        )
        
        # Send outcome notification
        if self._alerter:
            asyncio.create_task(self._notify_trade_closed(signal_id, outcome))
    
    def _pause(self, reason: str) -> None:
        """Pause night auto trading."""
        self._paused = True
        self._pause_reason = reason
        self.logger.warning("Night auto paused", reason=reason)
        
        # Also pause execution engine
        self._execution.pause(f"Night auto: {reason}")
        
        # Send alert
        if self._alerter:
            asyncio.create_task(
                self._alerter.send_message(
                    f"⚠️ **Night Auto Paused**\nReason: {reason}\nManual review required."
                )
            )
    
    def resume(self) -> None:
        """Resume night auto trading."""
        self._paused = False
        self._pause_reason = None
        self._consecutive_losses = 0
        self._execution.resume()
        self.logger.info("Night auto resumed")
    
    async def _notify_trade_opened(
        self,
        signal: SignalCandidate,
        action: ActionData,
    ) -> None:
        """Send notification when trade is opened."""
        if not self._alerter:
            return
        
        embed = {
            "title": "🌙 Night Auto Trade Opened",
            "color": 0x0066FF,
            "fields": [
                {"name": "Direction", "value": signal.direction.value.upper(), "inline": True},
                {"name": "Entry Price", "value": f"{action.entry_price:.3f}", "inline": True},
                {"name": "Size", "value": f"€{action.position_size_eur:.2f}", "inline": True},
                {"name": "Confidence", "value": f"{signal.scoring.confidence:.2f}", "inline": True},
                {"name": "Session Trades", "value": f"{self._trades_this_session}/{settings.risk.night_mode_max_trades}", "inline": True},
                {"name": "Gas Cost", "value": f"€{action.gas_cost_eur:.2f}", "inline": True},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        
        await self._alerter.send_embed(embed)
    
    async def _notify_trade_closed(
        self,
        signal_id: str,
        outcome: OutcomeData,
    ) -> None:
        """Send notification when trade is closed."""
        if not self._alerter:
            return
        
        color = 0x00FF00 if outcome.net_profit_eur > 0 else 0xFF0000
        emoji = "✅" if outcome.net_profit_eur > 0 else "❌"
        
        embed = {
            "title": f"{emoji} Night Auto Trade Closed",
            "color": color,
            "fields": [
                {"name": "Exit Reason", "value": outcome.exit_reason.value if outcome.exit_reason else "Unknown", "inline": True},
                {"name": "Entry Price", "value": f"{outcome.fill_price:.3f}", "inline": True},
                {"name": "Exit Price", "value": f"{outcome.exit_price:.3f}", "inline": True},
                {"name": "Net Profit", "value": f"€{outcome.net_profit_eur:+.2f}", "inline": True},
                {"name": "Duration", "value": f"{outcome.position_duration_seconds:.0f}s", "inline": True},
                {"name": "Max Drawdown", "value": f"{outcome.max_adverse_move_pct*100:.1f}%", "inline": True},
            ],
            "footer": {"text": f"Signal: {signal_id[:8]}"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        
        await self._alerter.send_embed(embed)
    
    def get_metrics(self) -> dict:
        """Get night auto metrics."""
        total_profit = sum(t["profit_eur"] for t in self._trades)
        winning_trades = sum(1 for t in self._trades if t["profit_eur"] > 0)
        
        return {
            "is_night_hours": self._is_night_hours(),
            "session_date": self._session_date,
            "trades_this_session": self._trades_this_session,
            "loss_this_session": self._loss_this_session,
            "consecutive_losses": self._consecutive_losses,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "total_trades": len(self._trades),
            "total_profit": total_profit,
            "win_rate": winning_trades / len(self._trades) if self._trades else 0,
        }


# Import asyncio for task creation
import asyncio

