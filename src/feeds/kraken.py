"""
Kraken WebSocket feed for BTC/USD real-time price data.
"""

import asyncio
import orjson  # 2-3x faster than stdlib json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import structlog
from websockets.exceptions import ConnectionClosed

from src.feeds.base import BaseFeed
from src.models.schemas import ExchangeTick, ExchangeMetrics

logger = structlog.get_logger()


@dataclass
class VolumeEntry:
    """Volume entry for rolling window."""
    timestamp_ms: int
    volume: float


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
        
        # Enhanced volume tracking with rolling 5-minute buffer
        self._volume_buffer: deque[VolumeEntry] = deque()
        self._volume_buffer_max_age_ms = 300_000  # 5 minutes
        self._volume_1m: float = 0.0
        self._volume_5m_avg: float = 0.0
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
        
        await self._ws.send(orjson.dumps(subscribe_msg).decode())
        self.logger.info("Sent subscription request", pair=self.pair)
    
    async def _handle_message(self, message: str) -> None:
        """Parse and process Kraken trade message."""
        try:
            data = orjson.loads(message)
            
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
                    
        except orjson.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    def _update_volume(self, volume: float, timestamp_ms: int) -> None:
        """Update rolling volume with 1-minute current and 5-minute average."""
        # Add to rolling buffer
        self._volume_buffer.append(VolumeEntry(
            timestamp_ms=timestamp_ms,
            volume=volume,
        ))
        
        # Clean up old entries
        cutoff_ms = timestamp_ms - self._volume_buffer_max_age_ms
        while self._volume_buffer and self._volume_buffer[0].timestamp_ms < cutoff_ms:
            self._volume_buffer.popleft()
        
        # Calculate 1-minute volume (last 60 seconds)
        cutoff_1m = timestamp_ms - 60_000
        self._volume_1m = sum(
            e.volume for e in self._volume_buffer 
            if e.timestamp_ms >= cutoff_1m
        )
        
        # Calculate 5-minute average (volume per minute over 5 minutes)
        if len(self._volume_buffer) >= 2:
            total_volume = sum(e.volume for e in self._volume_buffer)
            time_span_ms = self._volume_buffer[-1].timestamp_ms - self._volume_buffer[0].timestamp_ms
            if time_span_ms > 0:
                minutes = time_span_ms / 60_000
                self._volume_5m_avg = total_volume / max(minutes, 1.0)
            else:
                self._volume_5m_avg = total_volume
        else:
            self._volume_5m_avg = self._volume_1m
    
    async def _heartbeat(self) -> None:
        """Send periodic ping messages (Kraken specific)."""
        while self._running:
            try:
                if self._ws:
                    try:
                        # Kraken uses JSON ping
                        ping_msg = {"event": "ping"}
                        await self._ws.send(orjson.dumps(ping_msg).decode())
                        self.health.last_heartbeat_ms = int(time.time() * 1000)
                    except (ConnectionClosed, AttributeError, Exception):
                        # Connection closed or invalid - silently skip
                        pass
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                # Normal shutdown
                break
            except Exception:
                # Silently continue - heartbeat errors are not critical
                pass
    
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
            volume_5m_avg=self._volume_5m_avg,
            atr_5m=self.price_buffer.get_atr(300, 60),
            max_move_10s_pct=self.price_buffer.get_max_move_in_subwindow(30, 10),
        )

