"""
Binance Futures Mark Price & Funding Rate Feed.

Provides leading signals:
- Mark Price: 1-3s lead time on volatility spikes
- Funding Rate: Indicates market sentiment/leverage
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Callable

import orjson
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

logger = structlog.get_logger()


@dataclass
class FuturesData:
    """Binance Futures data snapshot."""
    mark_price: float = 0.0
    index_price: float = 0.0
    funding_rate: float = 0.0
    next_funding_time_ms: int = 0
    timestamp_ms: int = 0
    
    # Derived metrics
    mark_index_spread: float = 0.0  # Mark - Index (positive = longs paying)
    
    @property
    def is_valid(self) -> bool:
        return self.mark_price > 0 and self.timestamp_ms > 0
    
    @property
    def age_seconds(self) -> float:
        if self.timestamp_ms == 0:
            return float('inf')
        return (int(time.time() * 1000) - self.timestamp_ms) / 1000.0


class BinanceFuturesFeed:
    """
    Binance Futures WebSocket feed for mark price and funding rate.
    
    Streams:
    - btcusdt@markPrice: Real-time mark price (every 3s)
    - Funding rate included in mark price updates
    
    Mark price leads spot during volatility because:
    - Futures react first to large orders
    - Leveraged traders are faster than spot traders
    - Index price is delayed spot average
    """
    
    WS_URL = "wss://fstream.binance.com/ws"
    
    def __init__(
        self,
        symbol: str = "btcusdt",
        on_update: Optional[Callable[[FuturesData], None]] = None,
    ):
        self.symbol = symbol.lower()
        self.on_update = on_update
        self.logger = logger.bind(feed="binance_futures", symbol=self.symbol)
        
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._data = FuturesData()
        
        # Metrics
        self._message_count = 0
        self._last_message_ms = 0
        self._reconnect_count = 0
    
    def get_data(self) -> FuturesData:
        """Get current futures data."""
        return self._data
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed
    
    @property
    def is_stale(self) -> bool:
        """Check if data is stale (>10 seconds old)."""
        if self._data.timestamp_ms == 0:
            return True
        age_ms = int(time.time() * 1000) - self._data.timestamp_ms
        return age_ms > 10000
    
    async def start(self) -> None:
        """Start the futures feed."""
        self._running = True
        self.logger.info("Starting Binance Futures feed")
        
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Futures feed error", error=str(e))
                await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the futures feed."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self.logger.info("Binance Futures feed stopped")
    
    async def _connect_and_stream(self) -> None:
        """Connect and stream mark price data."""
        stream = f"{self.symbol}@markPrice"
        url = f"{self.WS_URL}/{stream}"
        
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, ping_interval=20, ping_timeout=10),
                timeout=10.0
            )
            self.logger.info("Connected to Binance Futures", stream=stream)
            
            async for message in self._ws:
                if not self._running:
                    break
                await self._handle_message(message)
                
        except asyncio.TimeoutError:
            self.logger.warning("Connection timeout")
        except ConnectionClosed as e:
            self.logger.warning("Connection closed", code=e.code)
        except Exception as e:
            self.logger.error("Stream error", error=str(e))
        finally:
            self._reconnect_count += 1
            if self._running:
                await asyncio.sleep(1)
    
    async def _handle_message(self, message: str) -> None:
        """Parse mark price update."""
        try:
            data = orjson.loads(message)
            
            # Mark price stream format:
            # {
            #   "e": "markPriceUpdate",
            #   "E": 1234567890123,     # Event time
            #   "s": "BTCUSDT",         # Symbol
            #   "p": "11794.15000000",  # Mark price
            #   "i": "11784.62659091",  # Index price  
            #   "P": "11784.25641265",  # Estimated settle price
            #   "r": "0.00038167",      # Funding rate
            #   "T": 1234567890123      # Next funding time
            # }
            
            if data.get("e") == "markPriceUpdate":
                mark_price = float(data.get("p", 0))
                index_price = float(data.get("i", 0))
                funding_rate = float(data.get("r", 0))
                next_funding = int(data.get("T", 0))
                event_time = int(data.get("E", int(time.time() * 1000)))
                
                # Calculate mark-index spread (positive = longs paying premium)
                spread = mark_price - index_price if mark_price > 0 and index_price > 0 else 0
                
                self._data = FuturesData(
                    mark_price=mark_price,
                    index_price=index_price,
                    funding_rate=funding_rate,
                    next_funding_time_ms=next_funding,
                    timestamp_ms=event_time,
                    mark_index_spread=spread,
                )
                
                self._message_count += 1
                self._last_message_ms = event_time
                
                # Notify callback
                if self.on_update:
                    self.on_update(self._data)
                
                # Log periodically
                if self._message_count % 100 == 0:
                    self.logger.debug(
                        "Futures update",
                        mark=f"${mark_price:,.2f}",
                        spread=f"${spread:+.2f}",
                        funding=f"{funding_rate:.4%}",
                    )
                    
        except orjson.JSONDecodeError:
            self.logger.warning("Invalid JSON from futures stream")
        except Exception as e:
            self.logger.error("Error parsing futures message", error=str(e))
    
    def get_metrics(self) -> dict:
        """Get feed metrics."""
        return {
            "connected": self.is_connected,
            "is_stale": self.is_stale,
            "message_count": self._message_count,
            "reconnect_count": self._reconnect_count,
            "mark_price": self._data.mark_price,
            "funding_rate": self._data.funding_rate,
            "mark_index_spread": self._data.mark_index_spread,
        }


class FundingRateTracker:
    """
    Track funding rate changes across multiple time windows.
    
    Rapid funding rate changes predict volatility:
    - Rising funding = longs getting crowded = potential long squeeze
    - Falling funding = shorts getting crowded = potential short squeeze
    """
    
    def __init__(self):
        self.logger = logger.bind(component="funding_tracker")
        self._history: list[tuple[int, float]] = []  # (timestamp_ms, rate)
        self._max_history = 50  # Keep last 50 readings (~2.5 hours at 3s intervals)
    
    def record(self, timestamp_ms: int, funding_rate: float) -> None:
        """Record a funding rate observation."""
        self._history.append((timestamp_ms, funding_rate))
        
        # Trim old history
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
    
    def get_acceleration(self) -> float:
        """
        Get funding rate acceleration (change in rate over last 15 minutes).
        
        Returns:
            Acceleration in basis points. Positive = rates rising, Negative = rates falling.
        """
        if len(self._history) < 5:
            return 0.0
        
        # Compare recent vs older readings
        now_ms = int(time.time() * 1000)
        fifteen_min_ago = now_ms - (15 * 60 * 1000)
        
        recent = [r for ts, r in self._history if ts > now_ms - 60000]  # Last minute
        older = [r for ts, r in self._history if ts < fifteen_min_ago]
        
        if not recent or not older:
            return 0.0
        
        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)
        
        # Return acceleration in basis points (0.0001 = 1 bp)
        return (recent_avg - older_avg) * 10000
    
    def get_signal_boost(self) -> float:
        """
        Get confidence boost based on funding rate acceleration.
        
        Returns:
            0.0 to 0.12 (up to +12% confidence boost)
        """
        acceleration = self.get_acceleration()
        
        # Need significant acceleration (>10 basis points change)
        if abs(acceleration) < 10:
            return 0.0
        
        # Cap at +12% boost
        return min(0.12, abs(acceleration) / 100)
    
    def get_direction_hint(self) -> Optional[str]:
        """
        Get directional hint from funding rate.
        
        Returns:
            "UP", "DOWN", or None
        """
        if len(self._history) < 3:
            return None
        
        recent_rate = self._history[-1][1]
        
        # Extreme positive funding = longs crowded = DOWN likely (squeeze)
        # Extreme negative funding = shorts crowded = UP likely (squeeze)
        if recent_rate > 0.001:  # >0.1% funding
            return "DOWN"  # Long squeeze likely
        elif recent_rate < -0.001:  # <-0.1% funding
            return "UP"  # Short squeeze likely
        
        return None

