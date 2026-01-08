"""
Binance WebSocket feed for BTC/USDT real-time price data.
"""

import orjson  # 2-3x faster than stdlib json
import time
from collections import deque
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


@dataclass
class VolumeEntry:
    """Volume entry for rolling window."""
    timestamp_ms: int
    volume: float


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
        
        # Enhanced volume tracking with rolling 5-minute buffer
        self._volume_buffer: deque[VolumeEntry] = deque()
        self._volume_buffer_max_age_ms = 300_000  # 5 minutes
        self._volume_1m: float = 0.0
        self._volume_5m_avg: float = 0.0
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
            data = orjson.loads(message)
            
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
                
        except orjson.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e), message=message[:100])
        except KeyError as e:
            self.logger.error("Missing key in message", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    def _update_volume(self, quantity: float, timestamp_ms: int) -> None:
        """Update rolling volume with 1-minute current and 5-minute average."""
        # Add to rolling buffer
        self._volume_buffer.append(VolumeEntry(
            timestamp_ms=timestamp_ms,
            volume=quantity,
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
                # Convert to volume per minute
                minutes = time_span_ms / 60_000
                self._volume_5m_avg = total_volume / max(minutes, 1.0)
            else:
                self._volume_5m_avg = total_volume
        else:
            self._volume_5m_avg = self._volume_1m
    
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
            volume_5m_avg=self._volume_5m_avg,
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
        
        # Enhanced volume tracking with rolling 5-minute buffer
        self._volume_buffer: deque[VolumeEntry] = deque()
        self._volume_buffer_max_age_ms = 300_000  # 5 minutes
        self._volume_1m: float = 0.0
        self._volume_5m_avg: float = 0.0
    
    async def _subscribe(self) -> None:
        """No explicit subscription needed for URL-based streams."""
        self.logger.info("Subscribed to aggTrade stream", symbol=self.symbol)
    
    def _update_volume(self, quantity: float, timestamp_ms: int) -> None:
        """Update rolling volume with 1-minute current and 5-minute average."""
        # Add to rolling buffer
        self._volume_buffer.append(VolumeEntry(
            timestamp_ms=timestamp_ms,
            volume=quantity,
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
    
    async def _handle_message(self, message: str) -> None:
        """Parse aggregated trade message."""
        try:
            data = orjson.loads(message)
            
            if data.get("e") == "aggTrade":
                price = float(data["p"])
                quantity = float(data["q"])
                trade_time_ms = data["T"]
                local_time_ms = int(time.time() * 1000)
                
                # Update rolling volume
                self._update_volume(quantity, trade_time_ms)
                
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
            volume_5m_avg=self._volume_5m_avg,
            atr_5m=self.price_buffer.get_atr(300, 60),
            max_move_10s_pct=self.price_buffer.get_max_move_in_subwindow(30, 10),
        )

