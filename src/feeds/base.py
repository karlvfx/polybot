"""
Base classes for WebSocket feed connections.
Provides common functionality for all data feeds.
"""

import asyncio
import ssl
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import certifi
import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.utils.session_tracker import session_tracker

logger = structlog.get_logger()


@dataclass
class FeedHealth:
    """Health status of a data feed."""
    connected: bool = False
    last_message_ms: int = 0
    last_heartbeat_ms: int = 0
    reconnect_count: int = 0
    error_count: int = 0
    latency_ms: float = 0.0
    
    @property
    def is_stale(self) -> bool:
        """Check if feed data is stale (>60 seconds old)."""
        if self.last_message_ms == 0:
            return True
        age_ms = int(time.time() * 1000) - self.last_message_ms
        return age_ms > 60000  # 60 seconds (increased for low-volume/quiet periods)
    
    @property
    def age_ms(self) -> int:
        """Get age of last message in milliseconds."""
        if self.last_message_ms == 0:
            return -1
        return int(time.time() * 1000) - self.last_message_ms


@dataclass
class PriceBuffer:
    """
    Rolling buffer for price data with time-based windowing.
    Used for calculating metrics like volatility, velocity, ATR.
    """
    max_age_seconds: float = 300  # 5 minutes
    prices: deque = field(default_factory=deque)
    timestamps: deque = field(default_factory=deque)
    volumes: deque = field(default_factory=deque)
    
    def add(self, price: float, timestamp_ms: int, volume: float = 0.0) -> None:
        """Add a new price point."""
        self.prices.append(price)
        self.timestamps.append(timestamp_ms)
        self.volumes.append(volume)
        self._cleanup()
    
    def _cleanup(self) -> None:
        """Remove old data points."""
        cutoff_ms = int(time.time() * 1000) - int(self.max_age_seconds * 1000)
        while self.timestamps and self.timestamps[0] < cutoff_ms:
            self.prices.popleft()
            self.timestamps.popleft()
            self.volumes.popleft()
    
    def get_window(self, seconds: float) -> tuple[list[float], list[int], list[float]]:
        """Get prices, timestamps, and volumes within a time window."""
        cutoff_ms = int(time.time() * 1000) - int(seconds * 1000)
        prices, timestamps, volumes = [], [], []
        for p, t, v in zip(self.prices, self.timestamps, self.volumes):
            if t >= cutoff_ms:
                prices.append(p)
                timestamps.append(t)
                volumes.append(v)
        return prices, timestamps, volumes
    
    def get_move_pct(self, seconds: float) -> float:
        """Get percentage price move over the window."""
        prices, _, _ = self.get_window(seconds)
        if len(prices) < 2:
            return 0.0
        return (prices[-1] - prices[0]) / prices[0]
    
    def get_volatility(self, seconds: float) -> float:
        """Get price volatility (std dev of returns) over window."""
        prices, _, _ = self.get_window(seconds)
        if len(prices) < 3:
            return 0.0
        returns = [(prices[i] - prices[i-1]) / prices[i-1] 
                   for i in range(1, len(prices))]
        if not returns:
            return 0.0
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        return variance ** 0.5
    
    def get_velocity(self, seconds: float) -> float:
        """Get price velocity (momentum) over window."""
        prices, timestamps, _ = self.get_window(seconds)
        if len(prices) < 2:
            return 0.0
        time_delta_s = (timestamps[-1] - timestamps[0]) / 1000
        if time_delta_s == 0:
            return 0.0
        price_change_pct = (prices[-1] - prices[0]) / prices[0]
        return price_change_pct / time_delta_s
    
    def get_volume_sum(self, seconds: float) -> float:
        """Get total volume over window."""
        _, _, volumes = self.get_window(seconds)
        return sum(volumes)
    
    def get_volume_avg(self, seconds: float) -> float:
        """Get average volume rate over window (volume per second)."""
        prices, timestamps, volumes = self.get_window(seconds)
        if len(timestamps) < 2:
            return 0.0
        
        total_volume = sum(volumes)
        time_span_s = (timestamps[-1] - timestamps[0]) / 1000.0
        
        if time_span_s <= 0:
            return 0.0
        
        return total_volume / time_span_s
    
    def get_atr(self, seconds: float, period_seconds: float = 60) -> float:
        """
        Calculate Average True Range (ATR) over the window.
        Uses simplified high-low range since we only have trade prices.
        """
        prices, timestamps, _ = self.get_window(seconds)
        if len(prices) < 10:
            return 0.0
        
        # Group prices into periods and calculate ranges
        ranges = []
        period_ms = int(period_seconds * 1000)
        
        if not timestamps:
            return 0.0
            
        current_period_start = timestamps[0]
        period_prices = []
        
        for p, t in zip(prices, timestamps):
            if t - current_period_start >= period_ms:
                if period_prices:
                    high = max(period_prices)
                    low = min(period_prices)
                    avg = (high + low) / 2
                    if avg > 0:
                        ranges.append((high - low) / avg)
                period_prices = [p]
                current_period_start = t
            else:
                period_prices.append(p)
        
        # Process final period
        if period_prices:
            high = max(period_prices)
            low = min(period_prices)
            avg = (high + low) / 2
            if avg > 0:
                ranges.append((high - low) / avg)
        
        if not ranges:
            return 0.0
        
        return sum(ranges) / len(ranges)
    
    def get_max_move_in_subwindow(self, total_seconds: float, subwindow_seconds: float) -> float:
        """
        Get the maximum absolute move within any subwindow.
        Used for spike concentration detection.
        """
        prices, timestamps, _ = self.get_window(total_seconds)
        if len(prices) < 2:
            return 0.0
        
        max_move = 0.0
        subwindow_ms = int(subwindow_seconds * 1000)
        
        for i, (start_price, start_time) in enumerate(zip(prices, timestamps)):
            for end_price, end_time in zip(prices[i+1:], timestamps[i+1:]):
                if end_time - start_time <= subwindow_ms:
                    move = abs(end_price - start_price) / start_price
                    max_move = max(max_move, move)
        
        return max_move
    
    @property
    def current_price(self) -> Optional[float]:
        """Get most recent price."""
        return self.prices[-1] if self.prices else None
    
    @property
    def current_timestamp(self) -> Optional[int]:
        """Get most recent timestamp."""
        return self.timestamps[-1] if self.timestamps else None


class BaseFeed(ABC):
    """
    Abstract base class for WebSocket data feeds.
    Handles connection management, reconnection, and health monitoring.
    """
    
    def __init__(
        self,
        name: str,
        ws_url: str,
        heartbeat_interval: float = 2.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ):
        self.name = name
        self.ws_url = ws_url
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        
        self.health = FeedHealth()
        self.price_buffer = PriceBuffer()
        
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._callbacks: list[Callable[[Any], None]] = []
        
        self.logger = logger.bind(feed=name)
    
    def add_callback(self, callback: Callable[[Any], None]) -> None:
        """Register a callback to receive data updates."""
        self._callbacks.append(callback)
    
    def _notify_callbacks(self, data: Any) -> None:
        """Notify all registered callbacks of new data."""
        for callback in self._callbacks:
            try:
                callback(data)
            except Exception as e:
                self.logger.error("Callback error", error=str(e))
    
    @abstractmethod
    async def _subscribe(self) -> None:
        """Send subscription message after connection. Override in subclass."""
        pass
    
    @abstractmethod
    async def _handle_message(self, message: str) -> None:
        """Process incoming message. Override in subclass."""
        pass
    
    async def _heartbeat(self) -> None:
        """Send periodic heartbeat pings."""
        while self._running:
            try:
                if self._ws:
                    # Check if connection is still open (websockets API changed)
                    try:
                        await self._ws.ping()
                        self.health.last_heartbeat_ms = int(time.time() * 1000)
                    except (ConnectionClosed, AttributeError, Exception):
                        # Connection closed or invalid - don't log, just skip
                        # Connection will be re-established by receive loop
                        pass
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                # Normal shutdown
                break
            except Exception:
                # Silently continue - heartbeat errors are not critical
                pass
    
    async def _connect(self) -> bool:
        """Establish WebSocket connection with fast timeout."""
        try:
            # Create SSL context with certifi certificates for wss:// connections
            ssl_context = None
            if self.ws_url.startswith('wss://'):
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            
            # Connection timeout (10s) - balance between fast and reliable
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.ws_url,
                    ping_interval=30,  # Less frequent pings
                    ping_timeout=20,   # More time to respond
                    close_timeout=5,
                    ssl=ssl_context,
                ),
                timeout=10.0  # 10s for VPS connections
            )
            self.health.connected = True
            self.logger.info("Connected to WebSocket")
            
            # Record connection event for session tracking
            session_tracker.record_connection_event(
                feed_name=self.name,
                event_type="connected" if self.health.reconnect_count == 0 else "reconnected",
            )
            
            await self._subscribe()
            return True
        except asyncio.TimeoutError:
            self.health.connected = False
            self.health.error_count += 1
            self.logger.warning("Connection timeout (5s)")
            return False
        except Exception as e:
            self.health.connected = False
            self.health.error_count += 1
            self.logger.error("Connection failed", error=str(e))
            return False
    
    async def _reconnect(self) -> None:
        """Handle reconnection with exponential backoff."""
        # Record disconnection event
        session_tracker.record_connection_event(
            feed_name=self.name,
            event_type="disconnected",
        )
        
        delay = self.reconnect_delay
        while self._running:
            self.health.reconnect_count += 1
            self.logger.info("Reconnecting", attempt=self.health.reconnect_count, delay=delay)
            
            # Record reconnecting event
            session_tracker.record_connection_event(
                feed_name=self.name,
                event_type="reconnecting",
                attempt=self.health.reconnect_count,
            )
            
            if await self._connect():
                return
            
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_reconnect_delay)
    
    async def _receive_loop(self) -> None:
        """Main loop for receiving messages."""
        while self._running:
            try:
                if not self._ws:
                    await self._reconnect()
                    continue
                
                # Check if connection is still valid by trying to receive
                # Use longer timeout (60s) since exchange feeds may have quiet periods
                try:
                    message = await asyncio.wait_for(
                        self._ws.recv(),
                        timeout=60.0
                    )
                    
                    receive_time = int(time.time() * 1000)
                    self.health.last_message_ms = receive_time
                    
                    await self._handle_message(message)
                except (ConnectionClosed, AttributeError) as e:
                    # Connection closed, reconnect
                    self.logger.debug("Connection lost, will reconnect", error=type(e).__name__)
                    self.health.connected = False
                    self._ws = None  # Clear the stale connection
                    if self._running:
                        await self._reconnect()
                    continue
                
            except asyncio.CancelledError:
                self.logger.info("Receive loop cancelled")
                break
                
            except asyncio.TimeoutError:
                # 60s timeout - check if connection is still alive via ping
                if self._ws and self._running:
                    try:
                        await self._ws.ping()
                        # Ping succeeded, connection is still alive, just no data
                        self.logger.debug("No data for 60s but connection alive")
                        continue
                    except Exception:
                        # Ping failed, reconnect
                        self.logger.warning("Connection appears dead, reconnecting")
                        self.health.connected = False
                        self._ws = None
                        await self._reconnect()
                continue
                
            except ConnectionClosed as e:
                self.logger.warning("Connection closed", code=e.code, reason=e.reason)
                self.health.connected = False
                self._ws = None
                
            except WebSocketException as e:
                self.logger.error("WebSocket error", error=str(e))
                self.health.connected = False
                self._ws = None
                self.health.error_count += 1
                
            except Exception as e:
                self.logger.error("Unexpected error in receive loop", error=str(e))
                self.health.error_count += 1
                await asyncio.sleep(1)
    
    async def start(self) -> None:
        """Start the feed (non-blocking initial connection)."""
        self._running = True
        self._tasks = []
        self.logger.info("Starting feed")
        
        # Try initial connection but DON'T block if it fails
        # The receive loop will handle reconnection
        # This allows all feeds to start simultaneously
        asyncio.create_task(self._initial_connect())
        
        # Start receive loop and heartbeat immediately
        self._tasks = [
            asyncio.create_task(self._receive_loop()),
            asyncio.create_task(self._heartbeat()),
        ]
        
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            self.logger.info("Feed cancelled")
    
    async def _initial_connect(self) -> None:
        """Attempt initial connection (non-blocking helper)."""
        try:
            await self._connect()
        except Exception as e:
            self.logger.warning("Initial connection failed, will retry", error=str(e))
    
    async def stop(self) -> None:
        """Stop the feed gracefully."""
        self._running = False
        self.logger.info("Stopping feed")
        
        if self._ws:
            try:
                await self._ws.close()
            except (ConnectionClosed, AttributeError, Exception):
                pass
        
        self.health.connected = False
    
    def get_metrics(self) -> dict:
        """Get current metrics for this feed."""
        return {
            "name": self.name,
            "connected": self.health.connected,
            "is_stale": self.health.is_stale,
            "age_ms": self.health.age_ms,
            "reconnect_count": self.health.reconnect_count,
            "error_count": self.health.error_count,
            "current_price": self.price_buffer.current_price,
        }

