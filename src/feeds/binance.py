"""
Binance WebSocket feed for BTC/USDT real-time price data.
"""

import json
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from src.feeds.base import BaseFeed, PriceBuffer
from src.models.schemas import ExchangeTick, ExchangeMetrics

logger = structlog.get_logger()


@dataclass
class BinanceTick:
    """Parsed Binance trade tick."""
    symbol: str
    price: float
    quantity: float
    trade_time_ms: int
    is_buyer_maker: bool


class BinanceFeed(BaseFeed):
    """
    Binance WebSocket feed for real-time BTC/USDT trade data.
    
    Uses the trade stream for individual trade events.
    Reference: https://binance-docs.github.io/apidocs/spot/en/#trade-streams
    """
    
    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://stream.binance.com:9443/ws",
    ):
        self.symbol = symbol.lower()
        self.stream_url = f"{ws_url}/{self.symbol}@trade"
        
        super().__init__(
            name="binance",
            ws_url=self.stream_url,
        )
        
        self._last_tick: Optional[BinanceTick] = None
        self._volume_1m: float = 0.0
        self._volume_window_start_ms: int = 0
    
    async def _subscribe(self) -> None:
        """
        Binance streams are subscribed via URL path.
        No explicit subscription message needed for single stream.
        """
        self.logger.info("Subscribed to trade stream", symbol=self.symbol)
    
    async def _handle_message(self, message: str) -> None:
        """Parse and process Binance trade message."""
        try:
            data = json.loads(message)
            
            # Handle trade event
            if data.get("e") == "trade":
                tick = BinanceTick(
                    symbol=data["s"],
                    price=float(data["p"]),
                    quantity=float(data["q"]),
                    trade_time_ms=data["T"],
                    is_buyer_maker=data["m"],
                )
                
                self._last_tick = tick
                local_time_ms = int(time.time() * 1000)
                
                # Update rolling volume
                self._update_volume(tick.quantity, tick.trade_time_ms)
                
                # Add to price buffer
                self.price_buffer.add(
                    price=tick.price,
                    timestamp_ms=tick.trade_time_ms,
                    volume=tick.quantity * tick.price,  # Volume in USDT
                )
                
                # Calculate latency
                self.health.latency_ms = local_time_ms - tick.trade_time_ms
                
                # Create tick event for callbacks
                exchange_tick = ExchangeTick(
                    exchange="binance",
                    symbol=self.symbol,
                    price=tick.price,
                    timestamp_ms=tick.trade_time_ms,
                    local_timestamp_ms=local_time_ms,
                    volume_1m=self._volume_1m,
                )
                
                self._notify_callbacks(exchange_tick)
                
        except json.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e), message=message[:100])
        except KeyError as e:
            self.logger.error("Missing key in message", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    def _update_volume(self, quantity: float, timestamp_ms: int) -> None:
        """Update 1-minute rolling volume."""
        current_window_start = timestamp_ms - (timestamp_ms % 60000)
        
        if current_window_start != self._volume_window_start_ms:
            # New minute window
            self._volume_1m = quantity
            self._volume_window_start_ms = current_window_start
        else:
            self._volume_1m += quantity
    
    def get_metrics(self) -> ExchangeMetrics:
        """Get current exchange metrics."""
        current_price = self.price_buffer.current_price or 0.0
        current_ts = self.price_buffer.current_timestamp or 0
        
        return ExchangeMetrics(
            exchange="binance",
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


# Alternative: Use aggregated trade stream for lower message volume
class BinanceAggTradeFeed(BaseFeed):
    """
    Binance WebSocket feed using aggregated trades.
    Lower message volume but still real-time.
    
    Use this if individual trades are too frequent.
    """
    
    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://stream.binance.com:9443/ws",
    ):
        self.symbol = symbol.lower()
        self.stream_url = f"{ws_url}/{self.symbol}@aggTrade"
        
        super().__init__(
            name="binance_agg",
            ws_url=self.stream_url,
        )
        
        self._volume_1m: float = 0.0
        self._volume_window_start_ms: int = 0
    
    async def _subscribe(self) -> None:
        """No explicit subscription needed for URL-based streams."""
        self.logger.info("Subscribed to aggTrade stream", symbol=self.symbol)
    
    async def _handle_message(self, message: str) -> None:
        """Parse aggregated trade message."""
        try:
            data = json.loads(message)
            
            if data.get("e") == "aggTrade":
                price = float(data["p"])
                quantity = float(data["q"])
                trade_time_ms = data["T"]
                local_time_ms = int(time.time() * 1000)
                
                # Update rolling volume
                current_window_start = trade_time_ms - (trade_time_ms % 60000)
                if current_window_start != self._volume_window_start_ms:
                    self._volume_1m = quantity
                    self._volume_window_start_ms = current_window_start
                else:
                    self._volume_1m += quantity
                
                # Add to price buffer
                self.price_buffer.add(
                    price=price,
                    timestamp_ms=trade_time_ms,
                    volume=quantity * price,
                )
                
                self.health.latency_ms = local_time_ms - trade_time_ms
                
                # Notify callbacks
                tick = ExchangeTick(
                    exchange="binance",
                    symbol=self.symbol,
                    price=price,
                    timestamp_ms=trade_time_ms,
                    local_timestamp_ms=local_time_ms,
                    volume_1m=self._volume_1m,
                )
                self._notify_callbacks(tick)
                
        except Exception as e:
            self.logger.error("Error handling aggTrade", error=str(e))
    
    def get_metrics(self) -> ExchangeMetrics:
        """Get current exchange metrics."""
        current_price = self.price_buffer.current_price or 0.0
        current_ts = self.price_buffer.current_timestamp or 0
        
        return ExchangeMetrics(
            exchange="binance",
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

