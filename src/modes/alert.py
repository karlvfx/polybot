"""
Alert Mode - Human-in-the-loop trading with virtual simulation.

Supports both virtual trading (simulation) and real trading when enabled.
"""

import asyncio
import time
from typing import Optional

import structlog

from src.modes.base import BaseMode
from src.modes.virtual_trader import VirtualTrader, VirtualPosition
from src.trading.real_trader import RealTrader, RealPosition
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
        
        # Real trader (initialized when real_trading_enabled is True)
        self._real_trader: Optional[RealTrader] = None
        self._real_trading_enabled = settings.real_trading_enabled
        self._real_daily_loss = 0.0  # Track daily loss for circuit breaker
        self._real_trades_today = 0
        
        if self._real_trading_enabled:
            self.logger.warning(
                "🔴 REAL TRADING MODE ENABLED - Real money will be used!",
                position_size=f"€{settings.real_trading_position_size_eur}",
                max_daily_loss=f"€{settings.real_trading_max_daily_loss_eur}",
            )
        
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
        
        # Initialize real trader if enabled
        if self._real_trading_enabled:
            asyncio.create_task(self._initialize_real_trader())
        
        # Start hourly summary task
        if self._alerter:
            self._summary_task = asyncio.create_task(self._performance_summary_loop())
    
    async def _initialize_real_trader(self) -> None:
        """Initialize real trader asynchronously."""
        if not settings.private_key:
            self.logger.error(
                "🔴 REAL TRADING DISABLED - No private key configured!",
                hint="Set PRIVATE_KEY in .env file"
            )
            self._real_trading_enabled = False
            return
        
        self._real_trader = RealTrader(
            polymarket_feed=self._polymarket_feed,
            position_size_eur=settings.real_trading_position_size_eur,
        )
        
        if await self._real_trader.initialize():
            self._real_trader.set_callbacks(
                on_opened=self._on_real_position_opened,
                on_closed=self._on_real_position_closed,
            )
            self.logger.info(
                "🟢 Real trader initialized successfully",
                position_size=f"€{settings.real_trading_position_size_eur}",
            )
        else:
            self.logger.error("Failed to initialize real trader - falling back to virtual")
            self._real_trading_enabled = False
            self._real_trader = None
    
    async def deactivate(self) -> None:
        """Deactivate alert mode and cleanup."""
        self._running = False
        
        # Stop virtual trader
        if self._virtual_trader:
            await self._virtual_trader.stop()
        
        # Stop real trader
        if self._real_trader:
            await self._real_trader.stop()
        
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
        breakdown = signal.scoring.breakdown
        
        # HIGH DIVERGENCE OVERRIDE: If divergence score is very high, bypass threshold
        # This catches obvious opportunities that might have low scores on other factors
        HIGH_DIV_OVERRIDE = 0.80  # 80% divergence score = definitely process
        if breakdown and breakdown.divergence >= HIGH_DIV_OVERRIDE:
            self.logger.info(
                "🚀 HIGH DIVERGENCE OVERRIDE - Processing despite low confidence",
                asset=signal.asset,
                confidence=f"{confidence:.1%}",
                divergence_score=f"{breakdown.divergence:.1%}",
            )
            # Skip confidence check, go straight to cooldown check
        elif confidence < threshold:
            # Log rejection with breakdown
            self.logger.info(
                "📊 Signal below threshold",
                asset=signal.asset,
                confidence=f"{confidence:.1%}",
                threshold=f"{threshold:.1%}",
                divergence=f"{breakdown.divergence:.1%}" if breakdown else "N/A",
                pm_staleness=f"{breakdown.pm_staleness:.1%}" if breakdown else "N/A",
                consensus=f"{breakdown.consensus_strength:.1%}" if breakdown else "N/A",
                liquidity=f"{breakdown.liquidity:.1%}" if breakdown else "N/A",
            )
            return False
        else:
            # Signal meets threshold!
            self.logger.info(
                "✅ Signal meets threshold!",
                asset=signal.asset,
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
        
        # ======================================================================
        # REAL TRADING (if enabled)
        # ======================================================================
        real_position_opened = False
        
        # Debug: Log real trading state
        self.logger.debug(
            "Real trading check",
            enabled=self._real_trading_enabled,
            trader_exists=self._real_trader is not None,
            asset=asset,
        )
        
        if self._real_trading_enabled and self._real_trader:
            # Safety checks
            can_trade = self._check_real_trading_limits()
            self.logger.info(
                "🔴 Real trade attempt",
                asset=asset,
                can_trade=can_trade,
                direction=signal.direction.value,
            )
            
            if can_trade:
                try:
                    pm_data = signal.polymarket
                    if pm_data and pm_data.yes_ask > 0:
                        self.logger.info(
                            "🔴 Calling real_trader.open_position",
                            asset=asset,
                            yes_ask=pm_data.yes_ask,
                        )
                        real_position = await self._real_trader.open_position(
                            signal=signal,
                            pm_data=pm_data,
                            asset=asset,
                        )
                        if real_position:
                            real_position_opened = True
                            self._real_trades_today += 1
                            self.logger.info(
                                "🔴 REAL POSITION OPENED",
                                asset=asset,
                                direction=signal.direction.value,
                                entry_price=f"${real_position.entry_price:.3f}",
                                size=f"€{real_position.size_eur:.2f}",
                            )
                        else:
                            self.logger.warning(
                                "🔴 Real position returned None (maker order not filled)",
                                asset=asset,
                            )
                    else:
                        self.logger.warning(
                            "🔴 Skipping real trade - no PM data or invalid ask",
                            has_pm_data=pm_data is not None,
                            yes_ask=pm_data.yes_ask if pm_data else None,
                        )
                except Exception as e:
                    self.logger.error("Failed to open real position", error=str(e), exc_info=True)
        else:
            if not self._real_trading_enabled:
                self.logger.debug("Real trading disabled")
            elif not self._real_trader:
                self.logger.warning("Real trader not initialized yet")
        
        # ======================================================================
        # VIRTUAL TRADING (always runs alongside real for tracking)
        # ======================================================================
        virtual_position_opened = False
        if self._virtual_trader:
            try:
                # Use PM data from the signal (correct asset), not from primary feed
                pm_data = signal.polymarket
                if pm_data and pm_data.yes_ask > 0:
                    position = await self._virtual_trader.open_virtual_position(
                        signal=signal,
                        market_id=signal.market_id,
                        pm_data=pm_data,
                        asset=asset,
                    )
                    if position is not None:  # Handle invalid entry price case
                        virtual_position_opened = True
                        self.logger.info(
                            "Virtual position opened",
                            asset=asset,
                            direction=signal.direction.value,
                            entry_price=f"${position.entry_price:.3f}",
                            confidence=f"{signal.scoring.confidence:.1%}" if signal.scoring else "N/A",
                        )
                    else:
                        self.logger.warning(
                            "Virtual position not opened - invalid entry price",
                            asset=asset,
                            yes_ask=pm_data.yes_ask,
                            no_ask=pm_data.no_ask,
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
    # Real Trading Callbacks & Helpers
    # ==========================================================================
    
    def _check_real_trading_limits(self) -> bool:
        """Check if real trading limits allow opening a new position."""
        # Check daily loss limit
        if self._real_daily_loss >= settings.real_trading_max_daily_loss_eur:
            self.logger.warning(
                "🛑 Real trading paused - daily loss limit reached",
                daily_loss=f"€{self._real_daily_loss:.2f}",
                limit=f"€{settings.real_trading_max_daily_loss_eur:.2f}",
            )
            return False
        
        # Check concurrent positions
        if self._real_trader:
            open_positions = len(self._real_trader.open_positions)
            if open_positions >= settings.real_trading_max_concurrent_positions:
                self.logger.info(
                    "Max concurrent real positions reached",
                    open=open_positions,
                    max=settings.real_trading_max_concurrent_positions,
                )
                return False
        
        return True
    
    async def _on_real_position_opened(
        self,
        position: RealPosition,
        signal: SignalCandidate,
        pm_data: PolymarketData,
    ) -> None:
        """Callback when real position is opened."""
        if not self._alerter:
            return
        
        # Send Discord alert for real trade
        await self._alerter.send_message(
            title="🔴 REAL TRADE OPENED",
            description=f"**{position.asset} {position.direction}**",
            color=0xFF0000,  # Red for real money
            fields=[
                ("Entry Price", f"${position.entry_price:.3f}", True),
                ("Size", f"€{position.size_eur:.2f}", True),
                ("Order ID", position.order_id[:8] if position.order_id else "N/A", True),
            ],
        )
    
    async def _on_real_position_closed(
        self,
        position: RealPosition,
    ) -> None:
        """Callback when real position is closed."""
        # Update daily P&L tracking
        if position.realized_pnl_eur:
            if position.realized_pnl_eur < 0:
                self._real_daily_loss += abs(position.realized_pnl_eur)
        
        if not self._alerter:
            return
        
        # Determine emoji and color
        pnl = position.realized_pnl_eur or 0
        if pnl >= 0:
            emoji = "🟢"
            color = 0x00FF00
        else:
            emoji = "🔴"
            color = 0xFF0000
        
        # Send Discord alert for real trade close
        await self._alerter.send_message(
            title=f"{emoji} REAL TRADE CLOSED",
            description=f"**{position.asset} {position.direction}** - {position.exit_reason}",
            color=color,
            fields=[
                ("Entry", f"${position.entry_price:.3f}", True),
                ("Exit", f"${position.exit_price:.3f}" if position.exit_price else "N/A", True),
                ("P&L", f"€{pnl:+.2f}", True),
                ("Duration", f"{position.duration_seconds:.0f}s", True),
                ("Rebates", f"€{position.rebates_earned_eur:.4f}", True),
                ("Today's Loss", f"€{self._real_daily_loss:.2f}", True),
            ],
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
