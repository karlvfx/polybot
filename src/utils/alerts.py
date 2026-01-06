"""
Discord alerting system with rich embeds for virtual trading.
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Optional, Dict

import httpx
import structlog

logger = structlog.get_logger()


class DiscordAlerter:
    """
    Discord webhook alerter for trading notifications.
    
    Sends rich embeds with:
    - Signal alerts with full context
    - Virtual position updates
    - Position closure summaries
    - Performance summaries
    - Error alerts
    """
    
    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 5]  # seconds between retries
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.logger = logger.bind(component="discord_alerter")
        self._rate_limit_until: float = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._consecutive_failures = 0
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a reusable HTTP client with proper timeouts."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=15.0,   # Connection timeout (increased for slow networks)
                    read=20.0,      # Read timeout
                    write=15.0,     # Write timeout
                    pool=10.0,      # Pool timeout
                ),
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                ),
            )
        return self._client
    
    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    # ==========================================================================
    # Core Methods
    # ==========================================================================
    
    async def _send_with_retry(self, payload: dict) -> bool:
        """
        Send payload to Discord with retry logic.
        
        Args:
            payload: JSON payload to send
            
        Returns:
            True if sent successfully
        """
        if not self.webhook_url:
            return False
        
        if time.time() < self._rate_limit_until:
            self.logger.warning("Rate limited, skipping message")
            return False
        
        last_error = None
        
        for attempt in range(self.MAX_RETRIES):
            try:
                client = await self._get_client()
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                )
                
                if response.status_code == 429:
                    retry_after = response.json().get("retry_after", 5)
                    self._rate_limit_until = time.time() + retry_after
                    self.logger.warning("Rate limited by Discord", retry_after=retry_after)
                    return False
                
                if response.status_code in (200, 204):
                    self._consecutive_failures = 0
                    return True
                
                response.raise_for_status()
                return True
                
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_error = e
                self._consecutive_failures += 1
                
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[attempt]
                    self.logger.warning(
                        "Discord connection failed, retrying",
                        attempt=attempt + 1,
                        max_retries=self.MAX_RETRIES,
                        delay=delay,
                        error_type=type(e).__name__,
                    )
                    await asyncio.sleep(delay)
                    
                    # Reset client on connection errors
                    await self.close()
                    
            except Exception as e:
                last_error = e
                self._consecutive_failures += 1
                break
        
        # All retries failed
        self.logger.error(
            "Failed to send to Discord after retries",
            error=str(last_error),
            error_type=type(last_error).__name__,
            consecutive_failures=self._consecutive_failures,
        )
        return False
    
    async def send_message(self, content: str) -> bool:
        """
        Send a simple text message.
        
        Args:
            content: Message text
            
        Returns:
            True if sent successfully
        """
        return await self._send_with_retry({"content": content})
    
    async def send_embed(self, embed: dict) -> bool:
        """
        Send a rich embed message.
        
        Args:
            embed: Discord embed object
            
        Returns:
            True if sent successfully
        """
        return await self._send_with_retry({"embeds": [embed]})
    
    # ==========================================================================
    # Virtual Trading Alerts
    # ==========================================================================
    
    async def send_virtual_position_opened(
        self,
        position,  # VirtualPosition
        signal,    # SignalCandidate
        pm_data,   # PolymarketData
        confidence_breakdown: Optional[Dict[str, float]] = None,
        performance: Optional[Dict] = None,
    ) -> bool:
        """
        Send detailed alert when virtual position is opened.
        """
        # Determine color based on confidence
        confidence = position.confidence_at_entry
        if confidence >= 0.85:
            color = 0x00FF00  # Green - High confidence
            confidence_emoji = "🟢"
            confidence_tier = "EXCELLENT"
        elif confidence >= 0.75:
            color = 0x90EE90  # Light green - Good confidence
            confidence_emoji = "🟡"
            confidence_tier = "GOOD"
        elif confidence >= 0.65:
            color = 0xFFFF00  # Yellow - Moderate confidence
            confidence_emoji = "🟠"
            confidence_tier = "MODERATE"
        else:
            color = 0xFFA500  # Orange - Low confidence
            confidence_emoji = "🔴"
            confidence_tier = "MARGINAL"
        
        # Direction emoji
        direction_emoji = "📈" if position.direction == "UP" else "📉"
        
        # Calculate mispricing
        if position.oracle_price_at_entry > 0 and position.spot_price_at_entry > 0:
            mispricing_pct = (position.spot_price_at_entry - position.oracle_price_at_entry) / position.oracle_price_at_entry * 100
        else:
            mispricing_pct = 0
        
        # Build confidence breakdown display
        breakdown_text = ""
        if confidence_breakdown:
            breakdown_text = self._format_confidence_breakdown(confidence_breakdown)
        elif signal.scoring and signal.scoring.breakdown:
            bd = signal.scoring.breakdown
            breakdown_text = self._format_confidence_breakdown({
                "oracle_age": bd.oracle_age,
                "consensus_strength": bd.consensus_strength,
                "misalignment": bd.misalignment,
                "liquidity": bd.liquidity,
                "spread_anomaly": bd.spread_anomaly,
                "volume_surge": bd.volume_surge,
                "spike_concentration": bd.spike_concentration,
            })
        
        # Performance stats
        perf_text = ""
        if performance and performance.get("total_trades", 0) > 0:
            wr = performance.get("win_rate", 0)
            pnl = performance.get("total_pnl", 0)
            streak = performance.get("current_streak", 0)
            streak_emoji = "🔥" if streak > 0 else "❄️" if streak < 0 else "➖"
            perf_text = (
                f"**Session Stats:** {performance['total_trades']} trades | "
                f"{wr:.0%} WR | €{pnl:+.2f} P/L | {streak_emoji} {abs(streak)}"
            )
        
        # Build embed
        embed = {
            "title": f"{direction_emoji} VIRTUAL TRADE OPENED {confidence_emoji}",
            "description": f"**{confidence_tier} CONFIDENCE** ({confidence:.0%})\n{perf_text}",
            "color": color,
            "fields": [
                {
                    "name": "📊 Signal Details",
                    "value": (
                        f"**Direction:** {position.direction}\n"
                        f"**Confidence:** {confidence:.1%} {self._get_stars(confidence)}\n"
                        f"**Signal Type:** {signal.signal_type.value.upper() if signal.signal_type else 'STANDARD'}"
                    ),
                    "inline": True,
                },
                {
                    "name": "💰 Entry Details",
                    "value": (
                        f"**Entry Price:** ${position.entry_price:.3f}\n"
                        f"**Position Size:** €{position.position_size_eur:.2f}\n"
                        f"**Spread:** {position.spread_at_entry:.2%}"
                    ),
                    "inline": True,
                },
                {
                    "name": "⏱️ Timing",
                    "value": (
                        f"**Oracle Age:** {position.oracle_age_at_entry:.1f}s\n"
                        f"**Volatility:** {signal.consensus.volatility_regime.value.upper() if signal.consensus else 'N/A'}\n"
                        f"**Entry Time:** <t:{position.entry_time_ms // 1000}:T>"
                    ),
                    "inline": False,
                },
                {
                    "name": "📈 Market Metrics",
                    "value": (
                        f"**Spot Price:** ${position.spot_price_at_entry:,.2f}\n"
                        f"**Oracle Price:** ${position.oracle_price_at_entry:,.2f}\n"
                        f"**Mispricing:** {mispricing_pct:+.2f}%\n"
                        f"**Volume Surge:** {position.volume_surge_at_entry:.1f}x\n"
                        f"**Spike Concentration:** {position.spike_concentration_at_entry:.0%}"
                    ),
                    "inline": True,
                },
                {
                    "name": "💧 Liquidity",
                    "value": (
                        f"**Available:** €{position.liquidity_at_entry:.0f}\n"
                        f"**Collapsing:** {'⚠️ YES' if pm_data.liquidity_collapsing else '✅ NO'}\n"
                        f"**OB Imbalance:** {position.orderbook_imbalance_at_entry:+.1%}"
                    ),
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"Position ID: {position.position_id} | Market: {position.market_id[:20]}...",
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        # Add confidence breakdown if available
        if breakdown_text:
            embed["fields"].append({
                "name": "🎯 Confidence Breakdown",
                "value": breakdown_text,
                "inline": False,
            })
        
        # Add expected outcome
        embed["fields"].append({
            "name": "🎲 Expected Outcome",
            "value": (
                f"**Target Profit:** 8% (€{position.position_size_eur * 0.08:.2f})\n"
                f"**Stop Loss:** -3% (€{position.position_size_eur * -0.03:.2f})\n"
                f"**Max Duration:** 90 seconds"
            ),
            "inline": False,
        })
        
        return await self.send_embed(embed)
    
    async def send_virtual_position_update(
        self,
        position,  # VirtualPosition
    ) -> bool:
        """Send periodic update on open position."""
        
        # Color based on current P&L
        pnl_pct = position.current_pnl_pct
        if pnl_pct > 0.02:
            color = 0x00FF00  # Green
            pnl_emoji = "📈"
        elif pnl_pct > 0:
            color = 0x90EE90  # Light green
            pnl_emoji = "↗️"
        elif pnl_pct > -0.01:
            color = 0xFFFF00  # Yellow
            pnl_emoji = "↔️"
        else:
            color = 0xFF0000  # Red
            pnl_emoji = "📉"
        
        pnl_eur = position.position_size_eur * pnl_pct
        
        embed = {
            "title": f"{pnl_emoji} Position Update ({position.direction})",
            "color": color,
            "fields": [
                {
                    "name": "📊 Current Status",
                    "value": (
                        f"**Duration:** {position.duration_seconds:.0f}s\n"
                        f"**Current Price:** ${position.current_price:.3f}\n"
                        f"**Entry Price:** ${position.entry_price:.3f}"
                    ),
                    "inline": True,
                },
                {
                    "name": "💰 P&L",
                    "value": (
                        f"**Current P&L:** {pnl_pct:+.2%} (€{pnl_eur:+.2f})\n"
                        f"**Max Profit:** {position.max_profit_pct:+.2%}\n"
                        f"**Max Drawdown:** {position.max_drawdown_pct:+.2%}"
                    ),
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"Position ID: {position.position_id}",
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        return await self.send_embed(embed)
    
    async def send_virtual_position_closed(
        self,
        position,  # VirtualPosition
        performance: Optional[Dict] = None,
    ) -> bool:
        """Send position closure alert with outcome."""
        
        pnl_eur = position.realized_pnl_eur or 0
        pnl_pct = position.realized_pnl_pct or 0
        
        # Determine outcome
        if pnl_eur > 0:
            color = 0x00FF00  # Green - Win
            outcome_emoji = "✅"
            outcome_text = "WIN"
        else:
            color = 0xFF0000  # Red - Loss
            outcome_emoji = "❌"
            outcome_text = "LOSS"
        
        # Exit reason emoji
        exit_emojis = {
            "spread_converged": "🎯",
            "take_profit": "💰",
            "stop_loss": "🛑",
            "time_limit": "⏰",
            "oracle_update_imminent": "🔮",
            "emergency_time": "🚨",
            "liquidity_collapse": "💧",
        }
        exit_emoji = exit_emojis.get(position.exit_reason, "❓")
        exit_reason_display = (position.exit_reason or "unknown").replace("_", " ").title()
        
        # Performance stats
        perf_text = ""
        if performance and performance.get("total_trades", 0) > 0:
            wr = performance.get("win_rate", 0)
            total_pnl = performance.get("total_pnl", 0)
            streak = performance.get("current_streak", 0)
            streak_emoji = "🔥" if streak > 0 else "❄️" if streak < 0 else "➖"
            perf_text = (
                f"\n**Session:** {performance['total_trades']} trades | "
                f"{wr:.0%} WR | €{total_pnl:+.2f} | {streak_emoji} {abs(streak)}"
            )
        
        embed = {
            "title": f"{outcome_emoji} VIRTUAL TRADE {outcome_text} {exit_emoji}",
            "description": f"**Exit Reason:** {exit_reason_display}{perf_text}",
            "color": color,
            "fields": [
                {
                    "name": "📊 Trade Summary",
                    "value": (
                        f"**Direction:** {position.direction}\n"
                        f"**Duration:** {position.duration_seconds:.1f}s\n"
                        f"**Confidence:** {position.confidence_at_entry:.0%}"
                    ),
                    "inline": True,
                },
                {
                    "name": "💰 P&L Details",
                    "value": (
                        f"**Realized P&L:** {pnl_pct:+.2%}\n"
                        f"**Profit/Loss:** €{pnl_eur:+.2f}\n"
                        f"**Entry:** ${position.entry_price:.3f}\n"
                        f"**Exit:** ${position.exit_price:.3f}"
                    ),
                    "inline": True,
                },
                {
                    "name": "📈 Trade Journey",
                    "value": (
                        f"**Max Profit Hit:** {position.max_profit_pct:+.2%}\n"
                        f"**Max Drawdown:** {position.max_drawdown_pct:+.2%}\n"
                        f"**Final P&L:** {pnl_pct:+.2%}"
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": f"Position ID: {position.position_id}",
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        return await self.send_embed(embed)
    
    async def send_performance_summary(
        self,
        performance: dict,
        period: str = "Session",
    ) -> bool:
        """Send periodic performance summary."""
        
        win_rate = performance.get("win_rate", 0)
        total_pnl = performance.get("total_pnl", 0)
        total_trades = performance.get("total_trades", 0)
        
        # Color based on performance
        if win_rate >= 0.65 and total_pnl > 0:
            color = 0x00FF00  # Green - Great
        elif win_rate >= 0.55 and total_pnl >= 0:
            color = 0xFFFF00  # Yellow - Okay
        else:
            color = 0xFF0000  # Red - Needs work
        
        # Build exit reasons breakdown
        exit_reasons = performance.get("exit_reasons", {})
        exit_text = ""
        if exit_reasons:
            exit_lines = []
            for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
                emoji = {
                    "spread_converged": "🎯",
                    "take_profit": "💰",
                    "stop_loss": "🛑",
                    "time_limit": "⏰",
                    "oracle_update_imminent": "🔮",
                }.get(reason, "❓")
                exit_lines.append(f"{emoji} {reason.replace('_', ' ').title()}: {count}")
            exit_text = "\n".join(exit_lines[:5])  # Top 5
        
        embed = {
            "title": f"📊 {period} Performance Summary",
            "color": color,
            "fields": [
                {
                    "name": "🎯 Overall Stats",
                    "value": (
                        f"**Total Trades:** {total_trades}\n"
                        f"**Wins:** {performance.get('winning_trades', 0)} ✅\n"
                        f"**Losses:** {performance.get('losing_trades', 0)} ❌\n"
                        f"**Win Rate:** {win_rate:.1%} {self._get_win_rate_emoji(win_rate)}"
                    ),
                    "inline": True,
                },
                {
                    "name": "💰 Profitability",
                    "value": (
                        f"**Total P&L:** €{total_pnl:+.2f}\n"
                        f"**Avg P&L/Trade:** €{performance.get('avg_profit_per_trade', 0):+.2f}\n"
                        f"**Best Trade:** €{performance.get('best_trade', 0):+.2f}\n"
                        f"**Worst Trade:** €{performance.get('worst_trade', 0):+.2f}"
                    ),
                    "inline": True,
                },
                {
                    "name": "🔥 Streaks",
                    "value": (
                        f"**Current:** {performance.get('current_streak', 0):+d}\n"
                        f"**Best Win Streak:** {performance.get('best_streak', 0)}\n"
                        f"**Worst Loss Streak:** {performance.get('worst_streak', 0)}"
                    ),
                    "inline": True,
                },
            ],
            "footer": {
                "text": "Virtual trading simulation - No real money at risk",
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        # Add exit reasons if available
        if exit_text:
            embed["fields"].append({
                "name": "🚪 Exit Reasons",
                "value": exit_text,
                "inline": False,
            })
        
        return await self.send_embed(embed)
    
    async def send_hourly_summary(
        self,
        performance: dict,
    ) -> bool:
        """Send hourly performance breakdown."""
        
        hourly_stats = performance.get("hourly_stats", {})
        if not hourly_stats:
            return False
        
        # Build hourly breakdown
        hourly_lines = []
        for hour in sorted(hourly_stats.keys()):
            stats = hourly_stats[hour]
            trades = stats.get("trades", 0)
            wr = stats.get("win_rate", 0)
            pnl = stats.get("pnl", 0)
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            hourly_lines.append(f"{hour:02d}:00 {emoji} {trades} trades | {wr:.0%} WR | €{pnl:+.2f}")
        
        hourly_text = "\n".join(hourly_lines) if hourly_lines else "No data yet"
        
        embed = {
            "title": "⏰ Hourly Performance Breakdown",
            "color": 0x3498DB,  # Blue
            "description": f"```\n{hourly_text}\n```",
            "footer": {
                "text": "Virtual trading simulation",
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        return await self.send_embed(embed)
    
    # ==========================================================================
    # Legacy Methods (kept for backwards compatibility)
    # ==========================================================================
    
    async def send_signal_alert(
        self,
        signal_id: str,
        direction: str,
        confidence: float,
        oracle_age: float,
        entry_price: float,
        estimated_profit: float,
        signal_type: str = "STANDARD",
        additional_fields: Optional[list] = None,
    ) -> bool:
        """Send a trading signal alert (legacy format)."""
        if confidence >= 0.85:
            stars = "★★★★★"
            color = 0x00FF00
        elif confidence >= 0.75:
            stars = "★★★★☆"
            color = 0x90EE90
        elif confidence >= 0.65:
            stars = "★★★☆☆"
            color = 0xFFFF00
        else:
            stars = "★★☆☆☆"
            color = 0xFFA500
        
        embed = {
            "title": "🔔 SIGNAL DETECTED",
            "color": color,
            "fields": [
                {"name": "Confidence", "value": f"{confidence:.2f} {stars}", "inline": True},
                {"name": "Direction", "value": direction.upper(), "inline": True},
                {"name": "Signal Type", "value": signal_type, "inline": True},
                {"name": "Oracle Age", "value": f"{oracle_age:.1f}s", "inline": True},
                {"name": "Entry Price", "value": f"{entry_price:.3f}", "inline": True},
                {"name": "Est. Profit", "value": f"€{estimated_profit:.2f}", "inline": True},
            ],
            "footer": {"text": f"Signal ID: {signal_id[:8]}"},
        }
        
        if additional_fields:
            embed["fields"].extend(additional_fields)
        
        return await self.send_embed(embed)
    
    async def send_trade_opened(
        self,
        signal_id: str,
        direction: str,
        entry_price: float,
        size_eur: float,
        mode: str,
    ) -> bool:
        """Send notification when trade is opened (legacy format)."""
        embed = {
            "title": "📈 Trade Opened",
            "color": 0x0066FF,
            "fields": [
                {"name": "Direction", "value": direction.upper(), "inline": True},
                {"name": "Entry Price", "value": f"{entry_price:.3f}", "inline": True},
                {"name": "Size", "value": f"€{size_eur:.2f}", "inline": True},
                {"name": "Mode", "value": mode, "inline": True},
            ],
            "footer": {"text": f"Signal: {signal_id[:8]}"},
        }
        return await self.send_embed(embed)
    
    async def send_trade_closed(
        self,
        signal_id: str,
        entry_price: float,
        exit_price: float,
        profit_eur: float,
        exit_reason: str,
        duration_s: float,
    ) -> bool:
        """Send notification when trade is closed (legacy format)."""
        color = 0x00FF00 if profit_eur > 0 else 0xFF0000
        emoji = "✅" if profit_eur > 0 else "❌"
        
        embed = {
            "title": f"{emoji} Trade Closed",
            "color": color,
            "fields": [
                {"name": "Entry", "value": f"{entry_price:.3f}", "inline": True},
                {"name": "Exit", "value": f"{exit_price:.3f}", "inline": True},
                {"name": "Profit", "value": f"€{profit_eur:+.2f}", "inline": True},
                {"name": "Reason", "value": exit_reason, "inline": True},
                {"name": "Duration", "value": f"{duration_s:.0f}s", "inline": True},
            ],
            "footer": {"text": f"Signal: {signal_id[:8]}"},
        }
        return await self.send_embed(embed)
    
    async def send_error_alert(
        self,
        error_type: str,
        message: str,
        details: Optional[str] = None,
    ) -> bool:
        """Send error alert."""
        embed = {
            "title": "⚠️ Error Alert",
            "color": 0xFF0000,
            "fields": [
                {"name": "Type", "value": error_type, "inline": False},
                {"name": "Message", "value": message, "inline": False},
            ],
        }
        
        if details:
            embed["fields"].append({"name": "Details", "value": details[:1000], "inline": False})
        
        return await self.send_embed(embed)
    
    async def send_circuit_breaker_alert(
        self,
        reason: str,
        action: str = "Trading paused",
    ) -> bool:
        """Send circuit breaker triggered alert."""
        embed = {
            "title": "🚨 Circuit Breaker Triggered",
            "color": 0xFF0000,
            "fields": [
                {"name": "Reason", "value": reason, "inline": False},
                {"name": "Action", "value": action, "inline": False},
            ],
            "description": "Manual review required before resuming.",
        }
        return await self.send_embed(embed)
    
    # ==========================================================================
    # Helper Methods
    # ==========================================================================
    
    def _get_stars(self, confidence: float) -> str:
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
    
    def _get_progress_bar(self, value: float, length: int = 10) -> str:
        """Create visual progress bar."""
        value = max(0, min(1, value))  # Clamp to 0-1
        filled = int(value * length)
        empty = length - filled
        return "█" * filled + "░" * empty
    
    def _get_win_rate_emoji(self, win_rate: float) -> str:
        """Get emoji for win rate."""
        if win_rate >= 0.70:
            return "🏆"
        elif win_rate >= 0.65:
            return "🎯"
        elif win_rate >= 0.55:
            return "📊"
        else:
            return "⚠️"
    
    def _format_confidence_breakdown(self, breakdown: Dict[str, float]) -> str:
        """Format confidence score breakdown with visual bars."""
        lines = []
        for component, score in breakdown.items():
            bar = self._get_progress_bar(score)
            name = component.replace("_", " ").title()
            lines.append(f"**{name}:** {bar} {score:.0%}")
        return "\n".join(lines)
