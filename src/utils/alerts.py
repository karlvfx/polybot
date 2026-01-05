"""
Discord alerting system.
"""

import asyncio
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()


class DiscordAlerter:
    """
    Discord webhook alerter for trading notifications.
    
    Sends:
    - Signal alerts
    - Trade notifications
    - Error alerts
    - Performance summaries
    """
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.logger = logger.bind(component="discord_alerter")
        self._rate_limit_until: float = 0
    
    async def send_message(self, content: str) -> bool:
        """
        Send a simple text message.
        
        Args:
            content: Message text
            
        Returns:
            True if sent successfully
        """
        if not self.webhook_url:
            return False
        
        # Check rate limit
        import time
        if time.time() < self._rate_limit_until:
            self.logger.warning("Rate limited, skipping message")
            return False
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.webhook_url,
                    json={"content": content},
                    timeout=10.0,
                )
                
                if response.status_code == 429:
                    # Rate limited
                    retry_after = response.json().get("retry_after", 5)
                    self._rate_limit_until = time.time() + retry_after
                    self.logger.warning("Rate limited", retry_after=retry_after)
                    return False
                
                response.raise_for_status()
                return True
                
        except Exception as e:
            self.logger.error("Failed to send message", error=str(e))
            return False
    
    async def send_embed(self, embed: dict) -> bool:
        """
        Send a rich embed message.
        
        Args:
            embed: Discord embed object
            
        Returns:
            True if sent successfully
        """
        if not self.webhook_url:
            return False
        
        import time
        if time.time() < self._rate_limit_until:
            self.logger.warning("Rate limited, skipping embed")
            return False
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.webhook_url,
                    json={"embeds": [embed]},
                    timeout=10.0,
                )
                
                if response.status_code == 429:
                    retry_after = response.json().get("retry_after", 5)
                    self._rate_limit_until = time.time() + retry_after
                    self.logger.warning("Rate limited", retry_after=retry_after)
                    return False
                
                response.raise_for_status()
                return True
                
        except Exception as e:
            self.logger.error("Failed to send embed", error=str(e))
            return False
    
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
        """
        Send a trading signal alert.
        """
        # Confidence stars
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
        """Send notification when trade is opened."""
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
        """Send notification when trade is closed."""
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
    
    async def send_performance_summary(
        self,
        period: str,
        signals_processed: int,
        trades_executed: int,
        win_rate: float,
        total_profit: float,
        avg_profit_per_trade: float,
    ) -> bool:
        """Send performance summary."""
        color = 0x00FF00 if total_profit > 0 else 0xFF0000
        
        embed = {
            "title": f"📊 {period} Performance Summary",
            "color": color,
            "fields": [
                {"name": "Signals", "value": str(signals_processed), "inline": True},
                {"name": "Trades", "value": str(trades_executed), "inline": True},
                {"name": "Win Rate", "value": f"{win_rate*100:.1f}%", "inline": True},
                {"name": "Total Profit", "value": f"€{total_profit:+.2f}", "inline": True},
                {"name": "Avg/Trade", "value": f"€{avg_profit_per_trade:+.2f}", "inline": True},
            ],
        }
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

