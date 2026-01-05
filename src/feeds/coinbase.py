"""
Coinbase Exchange WebSocket feed for BTC/USD real-time price data.
"""

import json
import time
from typing import Optional

import structlog

from src.feeds.base import BaseFeed
from src.models.schemas import ExchangeTick, ExchangeMetrics

logger = structlog.get_logger()


class CoinbaseFeed(BaseFeed):
    """
    Coinbase Exchange WebSocket feed for real-time BTC/USD trade data.
    
    Uses the matches channel for individual trade events.
    Reference: https://docs.cloud.coinbase.com/exchange/docs/websocket-channels#match
    """
    
    def __init__(
        self,
        product_id: str = "BTC-USD",
        ws_url: str = "wss://ws-feed.exchange.coinbase.com",
    ):
        self.product_id = product_id
        
        super().__init__(
            name="coinbase",
            ws_url=ws_url,
        )
        
        self._volume_1m: float = 0.0
        self._volume_window_start_ms: int = 0
    
    async def _subscribe(self) -> None:
        """Subscribe to matches channel."""
        subscribe_msg = {
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["matches"]
        }
        
        await self._ws.send(json.dumps(subscribe_msg))
        self.logger.info("Sent subscription request", product_id=self.product_id)
    
    async def _handle_message(self, message: str) -> None:
        """Parse and process Coinbase match message."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            # Handle subscription confirmation
            if msg_type == "subscriptions":
                self.logger.info("Subscription confirmed", channels=data.get("channels"))
                return
            
            # Handle match (trade) events
            if msg_type == "match" or msg_type == "last_match":
                price = float(data["price"])
                size = float(data["size"])
                
                # Parse timestamp (ISO format)
                time_str = data.get("time", "")
                if time_str:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        trade_time_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        trade_time_ms = int(time.time() * 1000)
                else:
                    trade_time_ms = int(time.time() * 1000)
                
                local_time_ms = int(time.time() * 1000)
                
                # Update rolling volume
                self._update_volume(size, trade_time_ms)
                
                # Add to price buffer
                self.price_buffer.add(
                    price=price,
                    timestamp_ms=trade_time_ms,
                    volume=size * price,  # Volume in USD
                )
                
                # Calculate latency
                self.health.latency_ms = local_time_ms - trade_time_ms
                
                # Create tick event for callbacks
                exchange_tick = ExchangeTick(
                    exchange="coinbase",
                    symbol=self.product_id,
                    price=price,
                    timestamp_ms=trade_time_ms,
                    local_timestamp_ms=local_time_ms,
                    volume_1m=self._volume_1m,
                )
                
                self._notify_callbacks(exchange_tick)
            
            # Handle errors
            elif msg_type == "error":
                self.logger.error(
                    "Coinbase error",
                    message=data.get("message"),
                    reason=data.get("reason"),
                )
                
        except json.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e))
        except KeyError as e:
            self.logger.error("Missing key in message", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    def _update_volume(self, size: float, timestamp_ms: int) -> None:
        """Update 1-minute rolling volume."""
        current_window_start = timestamp_ms - (timestamp_ms % 60000)
        
        if current_window_start != self._volume_window_start_ms:
            self._volume_1m = size
            self._volume_window_start_ms = current_window_start
        else:
            self._volume_1m += size
    
    def get_metrics(self) -> ExchangeMetrics:
        """Get current exchange metrics."""
        current_price = self.price_buffer.current_price or 0.0
        current_ts = self.price_buffer.current_timestamp or 0
        
        return ExchangeMetrics(
            exchange="coinbase",
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

