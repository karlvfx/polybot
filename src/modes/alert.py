"""
Alert Mode - Human-in-the-loop trading.
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
    SignalType,
)
from src.utils.alerts import DiscordAlerter
from config.settings import settings

logger = structlog.get_logger()


class AlertMode(BaseMode):
    """
    Alert mode for human-in-the-loop trading.
    
    Sends Discord notifications when high-confidence signals are detected,
    allowing the human to decide whether to trade.
    
    Features:
    - Configurable confidence threshold (default 0.70)
    - Rich Discord embeds with signal details
    - Cooldown between alerts
    - Manual trade tracking
    """
    
    def __init__(self, discord_webhook_url: Optional[str] = None):
        super().__init__("alert")
        
        # Discord alerter
        webhook_url = discord_webhook_url or settings.alerts.discord_webhook_url
        self._alerter = DiscordAlerter(webhook_url) if webhook_url else None
        
        # Alert tracking
        self._alerts_sent = 0
        self._last_alert_time_ms = 0
        
        # Manual trade tracking
        self._pending_alerts: dict[str, SignalCandidate] = {}
        self._manual_trades: list[dict] = []
    
    def should_process(self, signal: SignalCandidate) -> bool:
        """
        Check if signal meets alert threshold.
        """
        if not signal.scoring:
            return False
        
        # Check confidence threshold
        if signal.scoring.confidence < settings.alerts.alert_confidence_threshold:
            return False
        
        # Check cooldown
        now_ms = int(time.time() * 1000)
        cooldown_ms = settings.alerts.alert_cooldown_seconds * 1000
        if now_ms - self._last_alert_time_ms < cooldown_ms:
            self.logger.debug("Alert cooldown active")
            return False
        
        return True
    
    async def process_signal(
        self,
        signal: SignalCandidate,
    ) -> tuple[ActionData, Optional[OutcomeData]]:
        """
        Send alert for the signal.
        """
        if not signal.scoring or not signal.consensus or not signal.oracle or not signal.polymarket:
            return ActionData(mode="alert", decision=ActionDecision.ALERT), None
        
        # Send Discord alert
        if self._alerter:
            await self._send_alert(signal)
        
        # Track pending alert
        self._pending_alerts[signal.signal_id] = signal
        self._alerts_sent += 1
        self._last_alert_time_ms = int(time.time() * 1000)
        
        self.logger.info(
            "Alert sent",
            signal_id=signal.signal_id,
            confidence=signal.scoring.confidence,
        )
        
        action = ActionData(
            mode="alert",
            decision=ActionDecision.ALERT,
            position_size_eur=settings.risk.night_mode_max_position_eur,
            entry_price=signal.polymarket.yes_bid,
        )
        
        return action, None
    
    async def _send_alert(self, signal: SignalCandidate) -> None:
        """Send Discord alert with signal details."""
        if not self._alerter:
            return
        
        # Build alert content
        confidence_stars = self._get_confidence_stars(signal.scoring.confidence)
        
        # Determine volatility regime emoji
        vol_regime = signal.consensus.volatility_regime.value.upper()
        
        # Signal type indicator
        if signal.signal_type == SignalType.ESCAPE_CLAUSE:
            signal_type_text = "ESCAPE CLAUSE"
            move_emoji = "⚠️"
        else:
            signal_type_text = "STANDARD"
            move_emoji = "⚡"
        
        # Calculate estimated profit
        entry_price = signal.polymarket.yes_bid
        expected_exit = signal.polymarket.implied_probability + 0.06  # Assume 6% convergence
        position_size = settings.risk.night_mode_max_position_eur
        estimated_profit = (expected_exit - entry_price) * position_size / entry_price - 0.30
        
        # Build embed
        embed = {
            "title": "🔔 SIGNAL DETECTED",
            "color": self._get_confidence_color(signal.scoring.confidence),
            "fields": [
                {
                    "name": "Confidence",
                    "value": f"{signal.scoring.confidence:.2f} {confidence_stars}",
                    "inline": True,
                },
                {
                    "name": "Direction",
                    "value": signal.direction.value.upper(),
                    "inline": True,
                },
                {
                    "name": "Signal Type",
                    "value": f"{signal_type_text} {move_emoji}",
                    "inline": True,
                },
                {
                    "name": "Oracle Age",
                    "value": f"{signal.oracle.oracle_age_seconds:.1f}s (optimal window)",
                    "inline": True,
                },
                {
                    "name": "Volatility Regime",
                    "value": f"{vol_regime} (ATR: {signal.consensus.atr_5m*100:.2f}%)",
                    "inline": True,
                },
                {
                    "name": "Consensus Move",
                    "value": f"{signal.consensus.move_30s_pct*100:+.2f}% (30s)",
                    "inline": True,
                },
                {
                    "name": "Move Type",
                    "value": f"{'SPIKE' if signal.consensus.spike_concentration > 0.6 else 'DRIFT'} ({signal.consensus.spike_concentration*100:.0f}% in 10s)",
                    "inline": True,
                },
                {
                    "name": "Volume Surge",
                    "value": f"{signal.consensus.volume_surge_ratio:.1f}x average",
                    "inline": True,
                },
                {
                    "name": "PM Mispricing",
                    "value": f"YES @ {signal.polymarket.yes_bid:.2f}",
                    "inline": True,
                },
                {
                    "name": "Liquidity",
                    "value": f"€{signal.polymarket.yes_liquidity_best:.2f} available",
                    "inline": True,
                },
                {
                    "name": "Historical Win Rate",
                    "value": f"{signal.validation.historical_win_rate*100:.0f}%" if signal.validation else "N/A",
                    "inline": True,
                },
                {
                    "name": "Estimated Profit",
                    "value": f"€{estimated_profit:.2f} (after gas)",
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"Signal ID: {signal.signal_id[:8]} | Market: {signal.market_id[:20]}",
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        
        # Add escape clause details if applicable
        if signal.signal_type == SignalType.ESCAPE_CLAUSE:
            escape_fields = [
                {
                    "name": "⚠️ Escape Clause Active",
                    "value": (
                        f"• Move: {abs(signal.consensus.move_30s_pct)*100:.2f}% (below threshold)\n"
                        f"• Confidence penalty: -10%\n"
                        f"• Compensating factors verified ✓"
                    ),
                    "inline": False,
                },
            ]
            embed["fields"].extend(escape_fields)
        
        await self._alerter.send_embed(embed)
    
    def _get_confidence_stars(self, confidence: float) -> str:
        """Get star rating for confidence level."""
        if confidence >= 0.85:
            return "★★★★★"
        elif confidence >= 0.75:
            return "★★★★☆"
        elif confidence >= 0.65:
            return "★★★☆☆"
        elif confidence >= 0.55:
            return "★★☆☆☆"
        else:
            return "★☆☆☆☆"
    
    def _get_confidence_color(self, confidence: float) -> int:
        """Get Discord embed color based on confidence."""
        if confidence >= 0.85:
            return 0x00FF00  # Green
        elif confidence >= 0.75:
            return 0x90EE90  # Light green
        elif confidence >= 0.65:
            return 0xFFFF00  # Yellow
        else:
            return 0xFFA500  # Orange
    
    def record_manual_trade(
        self,
        signal_id: str,
        traded: bool,
        profit_eur: Optional[float] = None,
        notes: str = "",
    ) -> None:
        """
        Record outcome of a manual trade decision.
        
        Args:
            signal_id: ID of the alerted signal
            traded: Whether the human executed the trade
            profit_eur: Actual profit if traded
            notes: Any notes about the trade
        """
        signal = self._pending_alerts.pop(signal_id, None)
        
        self._manual_trades.append({
            "signal_id": signal_id,
            "timestamp_ms": int(time.time() * 1000),
            "traded": traded,
            "profit_eur": profit_eur,
            "confidence": signal.scoring.confidence if signal and signal.scoring else None,
            "notes": notes,
        })
        
        self.logger.info(
            "Manual trade recorded",
            signal_id=signal_id,
            traded=traded,
            profit=profit_eur,
        )
    
    def get_metrics(self) -> dict:
        """Get alert mode metrics."""
        traded_count = sum(1 for t in self._manual_trades if t["traded"])
        profitable_count = sum(1 for t in self._manual_trades if t["traded"] and (t["profit_eur"] or 0) > 0)
        total_profit = sum(t["profit_eur"] or 0 for t in self._manual_trades if t["traded"])
        
        return {
            "alerts_sent": self._alerts_sent,
            "pending_alerts": len(self._pending_alerts),
            "manual_trades_recorded": len(self._manual_trades),
            "trades_executed": traded_count,
            "profitable_trades": profitable_count,
            "total_manual_profit": total_profit,
            "manual_win_rate": profitable_count / traded_count if traded_count > 0 else 0,
        }

