"""
Kraken WebSocket feed for BTC/USD real-time price data.
"""

import json
import time
from typing import Optional

import structlog

from src.feeds.base import BaseFeed
from src.models.schemas import ExchangeTick, ExchangeMetrics

logger = structlog.get_logger()


class KrakenFeed(BaseFeed):
    """
    Kraken WebSocket feed for real-time XBT/USD trade data.
    
    Uses the trade channel for individual trade events.
    Reference: https://docs.kraken.com/websockets/#message-trade
    """
    
    def __init__(
        self,
        pair: str = "XBT/USD",
        ws_url: str = "wss://ws.kraken.com",
    ):
        self.pair = pair
        
        super().__init__(
            name="kraken",
            ws_url=ws_url,
        )
        
        self._volume_1m: float = 0.0
        self._volume_window_start_ms: int = 0
        self._subscribed = False
    
    async def _subscribe(self) -> None:
        """Subscribe to trade channel."""
        subscribe_msg = {
            "event": "subscribe",
            "pair": [self.pair],
            "subscription": {
                "name": "trade"
            }
        }
        
        await self._ws.send(json.dumps(subscribe_msg))
        self.logger.info("Sent subscription request", pair=self.pair)
    
    async def _handle_message(self, message: str) -> None:
        """Parse and process Kraken trade message."""
        try:
            data = json.loads(message)
            
            # Handle event messages (dict format)
            if isinstance(data, dict):
                event = data.get("event")
                
                if event == "systemStatus":
                    self.logger.info("System status", status=data.get("status"))
                    return
                
                if event == "subscriptionStatus":
                    status = data.get("status")
                    if status == "subscribed":
                        self._subscribed = True
                        self.logger.info("Subscription confirmed", pair=data.get("pair"))
                    elif status == "error":
                        self.logger.error("Subscription error", error=data.get("errorMessage"))
                    return
                
                if event == "heartbeat":
                    return
                
                if event == "pong":
                    return
            
            # Handle trade data (array format)
            # Format: [channelID, [[price, volume, time, side, orderType, misc], ...], channelName, pair]
            if isinstance(data, list) and len(data) >= 4:
                channel_name = data[-2]
                pair = data[-1]
                
                if channel_name == "trade" and pair == self.pair:
                    trades = data[1]
                    
                    for trade in trades:
                        if len(trade) >= 3:
                            price = float(trade[0])
                            volume = float(trade[1])
                            trade_time = float(trade[2])
                            trade_time_ms = int(trade_time * 1000)
                            local_time_ms = int(time.time() * 1000)
                            
                            # Update rolling volume
                            self._update_volume(volume, trade_time_ms)
                            
                            # Add to price buffer
                            self.price_buffer.add(
                                price=price,
                                timestamp_ms=trade_time_ms,
                                volume=volume * price,  # Volume in USD
                            )
                            
                            # Calculate latency
                            self.health.latency_ms = local_time_ms - trade_time_ms
                            
                            # Create tick event for callbacks (only for last trade in batch)
                            if trade == trades[-1]:
                                exchange_tick = ExchangeTick(
                                    exchange="kraken",
                                    symbol=self.pair,
                                    price=price,
                                    timestamp_ms=trade_time_ms,
                                    local_timestamp_ms=local_time_ms,
                                    volume_1m=self._volume_1m,
                                )
                                self._notify_callbacks(exchange_tick)
                    
        except json.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    def _update_volume(self, volume: float, timestamp_ms: int) -> None:
        """Update 1-minute rolling volume."""
        current_window_start = timestamp_ms - (timestamp_ms % 60000)
        
        if current_window_start != self._volume_window_start_ms:
            self._volume_1m = volume
            self._volume_window_start_ms = current_window_start
        else:
            self._volume_1m += volume
    
    async def _heartbeat(self) -> None:
        """Send periodic ping messages (Kraken specific)."""
        while self._running:
            try:
                if self._ws and self._ws.open:
                    # Kraken uses JSON ping
                    ping_msg = {"event": "ping"}
                    await self._ws.send(json.dumps(ping_msg))
                    self.health.last_heartbeat_ms = int(time.time() * 1000)
                await asyncio.sleep(self.heartbeat_interval)
            except Exception as e:
                self.logger.warning("Heartbeat failed", error=str(e))
    
    def get_metrics(self) -> ExchangeMetrics:
        """Get current exchange metrics."""
        current_price = self.price_buffer.current_price or 0.0
        current_ts = self.price_buffer.current_timestamp or 0
        
        return ExchangeMetrics(
            exchange="kraken",
            current_price=current_price,
            exchange_timestamp_ms=current_ts,
            local_timestamp_ms=int(time.time() * 1000),
            move_30s_pct=self.price_buffer.get_move_pct(30),
            velocity_30s=self.price_buffer.get_velocity(30),
            volatility_30s=self.price_buffer.get_volatility(30),
            volume_1m=self._volume_1m,
            atr_5m=self.price_buffer.get_atr(300, 60),
            max_move_10s_pct=self.price_buffer.get_max_move_in_subwindow(30, 10),
        )


# Need to import asyncio for heartbeat override
import asyncio

