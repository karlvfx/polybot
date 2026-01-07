"""
Alert Mode - Human-in-the-loop trading with virtual simulation.
"""

import asyncio
import time
from typing import Optional

import structlog

from src.modes.base import BaseMode
from src.modes.virtual_trader import VirtualTrader, VirtualPosition
from src.models.schemas import (
    SignalCandidate,
    ActionData,
    OutcomeData,
    ActionDecision,
    SignalType,
    PolymarketData,
)
from src.utils.alerts import DiscordAlerter
from config.settings import settings

logger = structlog.get_logger()


class AlertMode(BaseMode):
    """
    Alert mode for human-in-the-loop trading with virtual simulation.
    
    Features:
    - Sends Discord notifications when high-confidence signals are detected
    - Runs virtual trades to show what WOULD happen
    - Tracks virtual performance over time
    - Sends position updates and closure alerts
    - Hourly performance summaries
    """
    
    def __init__(
        self,
        discord_webhook_url: Optional[str] = None,
        polymarket_feed=None,
        chainlink_feed=None,
    ):
        super().__init__("alert")
        
        # Discord alerter
        webhook_url = discord_webhook_url or settings.alerts.discord_webhook_url
        self._alerter = DiscordAlerter(webhook_url) if webhook_url else None
        
        # Store feed references
        self._polymarket_feed = polymarket_feed
        self._chainlink_feed = chainlink_feed
        
        self.logger.info(
            "AlertMode initializing",
            polymarket_feed_provided=polymarket_feed is not None,
            chainlink_feed_provided=chainlink_feed is not None,
        )
        
        # Virtual trader (initialized when polymarket feed is available)
        # chainlink_feed is optional - virtual trader handles None gracefully
        self._virtual_trader: Optional[VirtualTrader] = None
        if polymarket_feed:
            try:
                self._initialize_virtual_trader()
                self.logger.info("Virtual trader initialized successfully")
            except Exception as e:
                self.logger.error("Failed to initialize virtual trader", error=str(e))
        else:
            self.logger.warning("No polymarket feed provided - virtual trader disabled")
        
        # Alert tracking
        self._alerts_sent = 0
        self._last_alert_time_ms = 0
        
        # Manual trade tracking
        self._pending_alerts: dict[str, SignalCandidate] = {}
        self._manual_trades: list[dict] = []
        
        # Performance summary task
        self._summary_task: Optional[asyncio.Task] = None
        self._running = True
    
    def set_feeds(self, polymarket_feed, chainlink_feed) -> None:
        """Set feed references for virtual trading."""
        self._polymarket_feed = polymarket_feed
        self._chainlink_feed = chainlink_feed
        if polymarket_feed and not self._virtual_trader:
            self._initialize_virtual_trader()
    
    def _initialize_virtual_trader(self) -> None:
        """Initialize the virtual trader with callbacks."""
        self._virtual_trader = VirtualTrader(
            polymarket_feed=self._polymarket_feed,
            chainlink_feed=self._chainlink_feed,
            position_size_eur=settings.risk.night_mode_max_position_eur,
            take_profit_pct=0.08,
            stop_loss_pct=-0.03,
            time_limit_seconds=90.0,
            emergency_time_seconds=120.0,
        )
        
        # Set callbacks for Discord alerts
        self._virtual_trader.set_callbacks(
            on_opened=self._on_position_opened,
            on_update=self._on_position_update,
            on_closed=self._on_position_closed,
        )
        
        self.logger.info(
            "Virtual trader initialized",
            has_polymarket_feed=self._polymarket_feed is not None,
            has_chainlink_feed=self._chainlink_feed is not None,
            has_alerter=self._alerter is not None,
        )
    
    def activate(self) -> None:
        """Activate alert mode and start periodic tasks."""
        super().activate()
        
        # Start hourly summary task
        if self._alerter:
            self._summary_task = asyncio.create_task(self._performance_summary_loop())
    
    async def deactivate(self) -> None:
        """Deactivate alert mode and cleanup."""
        self._running = False
        
        # Stop virtual trader
        if self._virtual_trader:
            await self._virtual_trader.stop()
        
        # Cancel summary task
        if self._summary_task and not self._summary_task.done():
            self._summary_task.cancel()
            try:
                await self._summary_task
            except asyncio.CancelledError:
                pass
        
        # Send final summary
        if self._alerter and self._virtual_trader:
            perf = self._virtual_trader.get_performance_summary()
            if perf["total_trades"] > 0:
                await self._alerter.send_performance_summary(perf, period="Final")
        
        super().deactivate()
    
    def should_process(self, signal: SignalCandidate) -> bool:
        """Check if signal meets alert threshold."""
        if not signal.scoring:
            self.logger.warning("Signal has no scoring data")
            return False
        
        confidence = signal.scoring.confidence
        threshold = settings.alerts.alert_confidence_threshold
        
        # Check confidence threshold
        if confidence < threshold:
            # Log rejection with breakdown
            breakdown = signal.scoring.breakdown
            self.logger.info(
                "📊 Signal below threshold",
                confidence=f"{confidence:.1%}",
                threshold=f"{threshold:.1%}",
                divergence=f"{breakdown.divergence:.1%}" if breakdown else "N/A",
                pm_staleness=f"{breakdown.pm_staleness:.1%}" if breakdown else "N/A",
                consensus=f"{breakdown.consensus_strength:.1%}" if breakdown else "N/A",
                liquidity=f"{breakdown.liquidity:.1%}" if breakdown else "N/A",
            )
            return False
        
        # Signal meets threshold!
        self.logger.info(
            "✅ Signal meets threshold!",
            confidence=f"{confidence:.1%}",
            threshold=f"{threshold:.1%}",
        )
        
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
        asset: str = "BTC",
    ) -> tuple[ActionData, Optional[OutcomeData]]:
        """Process signal: send alert and open virtual position."""
        # Oracle is optional for divergence strategy
        if not signal.scoring or not signal.consensus or not signal.polymarket:
            self.logger.warning(
                "Signal missing required data",
                has_scoring=signal.scoring is not None,
                has_consensus=signal.consensus is not None,
                has_polymarket=signal.polymarket is not None,
            )
            return ActionData(mode="alert", decision=ActionDecision.ALERT), None
        
        # Track pending alert
        self._pending_alerts[signal.signal_id] = signal
        self._alerts_sent += 1
        self._last_alert_time_ms = int(time.time() * 1000)
        
        # Get market title if available
        market_title = "BTC 15-minute Market"
        if self._polymarket_feed:
            market_info = getattr(self._polymarket_feed, "_discovered_market", None)
            if market_info and hasattr(market_info, "question"):
                market_title = market_info.question[:100]
        
        # Open virtual position if virtual trader is available
        virtual_position_opened = False
        if self._virtual_trader and self._polymarket_feed:
            try:
                pm_data = self._polymarket_feed.get_data()
                if pm_data:
                    await self._virtual_trader.open_virtual_position(
                        signal=signal,
                        market_id=signal.market_id,
                        pm_data=pm_data,
                        asset=asset,
                    )
                    virtual_position_opened = True
                    self.logger.info(
                        "Virtual position opened",
                        asset=asset,
                        direction=signal.direction.value,
                        confidence=f"{signal.scoring.confidence:.1%}" if signal.scoring else "N/A",
                    )
                else:
                    self.logger.warning("No Polymarket data available for virtual position")
            except Exception as e:
                self.logger.error("Failed to open virtual position", error=str(e), exc_info=True)
        
        # Fallback: Send basic alert if virtual trader not available or failed
        if not virtual_position_opened:
            if self._alerter:
                await self._send_basic_alert(signal)
        
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
    
    # ==========================================================================
    # Virtual Trader Callbacks
    # ==========================================================================
    
    async def _on_position_opened(
        self,
        position: VirtualPosition,
        signal: SignalCandidate,
        pm_data: PolymarketData,
    ) -> None:
        """Callback when virtual position is opened."""
        if not self._alerter:
            return
        
        # Get confidence breakdown
        breakdown = None
        if signal.scoring and signal.scoring.breakdown:
            bd = signal.scoring.breakdown
            breakdown = {
                "oracle_age": bd.oracle_age,
                "consensus_strength": bd.consensus_strength,
                "misalignment": bd.misalignment,
                "liquidity": bd.liquidity,
                "spread_anomaly": bd.spread_anomaly,
                "volume_surge": bd.volume_surge,
                "spike_concentration": bd.spike_concentration,
            }
        
        # Get performance stats
        perf = self._virtual_trader.get_performance_summary() if self._virtual_trader else None
        
        await self._alerter.send_virtual_position_opened(
            position=position,
            signal=signal,
            pm_data=pm_data,
            confidence_breakdown=breakdown,
            performance=perf,
        )
    
    async def _on_position_update(
        self,
        position: VirtualPosition,
    ) -> None:
        """Callback for position updates (every 30s)."""
        if not self._alerter:
            return
        
        await self._alerter.send_virtual_position_update(position)
    
    async def _on_position_closed(
        self,
        position: VirtualPosition,
    ) -> None:
        """Callback when virtual position is closed."""
        if not self._alerter:
            return
        
        # Get updated performance stats
        perf = self._virtual_trader.get_performance_summary() if self._virtual_trader else None
        
        await self._alerter.send_virtual_position_closed(
            position=position,
            performance=perf,
        )
    
    # ==========================================================================
    # Periodic Tasks
    # ==========================================================================
    
    async def _performance_summary_loop(self) -> None:
        """Send performance summary every hour."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # 1 hour
                
                if not self._running:
                    break
                
                if self._virtual_trader:
                    perf = self._virtual_trader.get_performance_summary()
                    if perf["total_trades"] > 0:
                        await self._alerter.send_performance_summary(perf, period="Hourly")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in performance summary loop", error=str(e))
                await asyncio.sleep(60)  # Wait a minute before retrying
    
    # ==========================================================================
    # Legacy Methods
    # ==========================================================================
    
    async def _send_basic_alert(self, signal: SignalCandidate) -> None:
        """Send basic Discord alert (fallback when virtual trader unavailable)."""
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
        expected_exit = signal.polymarket.implied_probability + 0.06
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
        """Get alert mode metrics including virtual trading stats."""
        traded_count = sum(1 for t in self._manual_trades if t["traded"])
        profitable_count = sum(1 for t in self._manual_trades if t["traded"] and (t["profit_eur"] or 0) > 0)
        total_profit = sum(t["profit_eur"] or 0 for t in self._manual_trades if t["traded"])
        
        metrics = {
            "alerts_sent": self._alerts_sent,
            "pending_alerts": len(self._pending_alerts),
            "manual_trades_recorded": len(self._manual_trades),
            "trades_executed": traded_count,
            "profitable_trades": profitable_count,
            "total_manual_profit": total_profit,
            "manual_win_rate": profitable_count / traded_count if traded_count > 0 else 0,
        }
        
        # Add virtual trading stats
        if self._virtual_trader:
            virtual_perf = self._virtual_trader.get_performance_summary()
            metrics["virtual_trading"] = virtual_perf
        
        return metrics
    
    def get_virtual_performance(self) -> Optional[dict]:
        """Get virtual trading performance summary."""
        if self._virtual_trader:
            return self._virtual_trader.get_performance_summary()
        return None
    
    def get_detailed_virtual_stats(self) -> Optional[dict]:
        """Get detailed virtual trading statistics."""
        if self._virtual_trader:
            return self._virtual_trader.get_detailed_stats()
        return None
