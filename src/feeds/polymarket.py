"""
Polymarket orderbook WebSocket feed.
Monitors YES/NO token orderbooks for 15-minute up/down markets.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from src.feeds.base import FeedHealth
from src.models.schemas import PolymarketData, OrderbookLevel

logger = structlog.get_logger()


@dataclass
class LiquiditySnapshot:
    """Snapshot of liquidity at a point in time."""
    timestamp_ms: int
    yes_liquidity: float
    no_liquidity: float


class LiquidityTracker:
    """Tracks historical liquidity for collapse detection."""
    
    def __init__(self, max_age_seconds: int = 120):
        self.max_age_seconds = max_age_seconds
        self.snapshots: deque[LiquiditySnapshot] = deque()
    
    def add_snapshot(self, yes_liquidity: float, no_liquidity: float) -> None:
        """Add a new liquidity snapshot."""
        now_ms = int(time.time() * 1000)
        self.snapshots.append(LiquiditySnapshot(
            timestamp_ms=now_ms,
            yes_liquidity=yes_liquidity,
            no_liquidity=no_liquidity,
        ))
        self._cleanup()
    
    def _cleanup(self) -> None:
        """Remove old snapshots."""
        cutoff_ms = int(time.time() * 1000) - (self.max_age_seconds * 1000)
        while self.snapshots and self.snapshots[0].timestamp_ms < cutoff_ms:
            self.snapshots.popleft()
    
    def get_liquidity_at(self, seconds_ago: int) -> tuple[float, float]:
        """Get YES and NO liquidity from N seconds ago."""
        target_ms = int(time.time() * 1000) - (seconds_ago * 1000)
        
        # Find closest snapshot
        closest = None
        min_diff = float('inf')
        
        for snapshot in self.snapshots:
            diff = abs(snapshot.timestamp_ms - target_ms)
            if diff < min_diff:
                min_diff = diff
                closest = snapshot
        
        if closest and min_diff < 10000:  # Within 10 seconds
            return closest.yes_liquidity, closest.no_liquidity
        
        return 0.0, 0.0


@dataclass
class OrderbookSide:
    """One side of the orderbook (bids or asks)."""
    levels: list[OrderbookLevel] = field(default_factory=list)
    
    @property
    def best_price(self) -> float:
        """Get best price (highest bid or lowest ask)."""
        return self.levels[0].price if self.levels else 0.0
    
    @property
    def best_size(self) -> float:
        """Get size at best price."""
        return self.levels[0].size if self.levels else 0.0
    
    @property
    def total_depth(self) -> float:
        """Get total size across all levels."""
        return sum(level.size for level in self.levels)
    
    def depth_at_levels(self, n: int) -> list[float]:
        """Get sizes at first N levels."""
        return [level.size for level in self.levels[:n]]


class PolymarketFeed:
    """
    Polymarket CLOB WebSocket feed for orderbook data.
    
    Monitors YES and NO token orderbooks for prediction markets.
    Reference: https://docs.polymarket.com/#websocket-api
    """
    
    def __init__(
        self,
        market_id: str,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        snapshot_interval: float = 0.5,  # 500ms snapshots
    ):
        self.market_id = market_id
        self.ws_url = ws_url
        self.snapshot_interval = snapshot_interval
        
        self.logger = logger.bind(feed="polymarket", market_id=market_id)
        
        # Connection state
        self._ws = None
        self._running = False
        self.health = FeedHealth()
        
        # Orderbook state
        self._yes_bids = OrderbookSide()
        self._yes_asks = OrderbookSide()
        self._no_bids = OrderbookSide()
        self._no_asks = OrderbookSide()
        
        # Liquidity tracking
        self._liquidity_tracker = LiquidityTracker()
        
        # Callbacks
        self._callbacks: list[Callable[[PolymarketData], None]] = []
        
        # Last snapshot timestamp
        self._last_snapshot_ms: int = 0
    
    def add_callback(self, callback: Callable[[PolymarketData], None]) -> None:
        """Register a callback for orderbook updates."""
        self._callbacks.append(callback)
    
    def _notify_callbacks(self, data: PolymarketData) -> None:
        """Notify all registered callbacks."""
        for callback in self._callbacks:
            try:
                callback(data)
            except Exception as e:
                self.logger.error("Callback error", error=str(e))
    
    async def _connect(self) -> bool:
        """Establish WebSocket connection."""
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
            self.health.connected = True
            self.logger.info("Connected to Polymarket WebSocket")
            return True
        except Exception as e:
            self.health.connected = False
            self.health.error_count += 1
            self.logger.error("Connection failed", error=str(e))
            return False
    
    async def _subscribe(self) -> None:
        """Subscribe to market orderbook."""
        # Polymarket subscription format
        subscribe_msg = {
            "type": "subscribe",
            "channel": "market",
            "market": self.market_id,
        }
        
        await self._ws.send(json.dumps(subscribe_msg))
        self.logger.info("Sent subscription request")
    
    def _parse_orderbook_update(self, data: dict) -> None:
        """Parse orderbook update message."""
        try:
            # Handle different message formats based on Polymarket API
            if "bids" in data:
                self._update_side(self._yes_bids, data["bids"], is_bid=True)
            if "asks" in data:
                self._update_side(self._yes_asks, data["asks"], is_bid=False)
            
            # Some markets have separate YES/NO structures
            if "yes" in data:
                yes_data = data["yes"]
                if "bids" in yes_data:
                    self._update_side(self._yes_bids, yes_data["bids"], is_bid=True)
                if "asks" in yes_data:
                    self._update_side(self._yes_asks, yes_data["asks"], is_bid=False)
            
            if "no" in data:
                no_data = data["no"]
                if "bids" in no_data:
                    self._update_side(self._no_bids, no_data["bids"], is_bid=True)
                if "asks" in no_data:
                    self._update_side(self._no_asks, no_data["asks"], is_bid=False)
                    
        except Exception as e:
            self.logger.error("Error parsing orderbook", error=str(e))
    
    def _update_side(self, side: OrderbookSide, updates: list, is_bid: bool) -> None:
        """Update one side of the orderbook."""
        # Convert updates to OrderbookLevel objects
        # Format: [[price, size], ...] or [{"price": x, "size": y}, ...]
        new_levels = []
        
        for update in updates:
            if isinstance(update, list) and len(update) >= 2:
                price = float(update[0])
                size = float(update[1])
            elif isinstance(update, dict):
                price = float(update.get("price", 0))
                size = float(update.get("size", 0))
            else:
                continue
            
            if size > 0:
                new_levels.append(OrderbookLevel(price=price, size=size))
        
        # Sort: bids descending, asks ascending
        if is_bid:
            new_levels.sort(key=lambda x: x.price, reverse=True)
        else:
            new_levels.sort(key=lambda x: x.price)
        
        side.levels = new_levels[:10]  # Keep top 10 levels
    
    def _should_snapshot(self) -> bool:
        """Check if enough time has passed for a new snapshot."""
        now_ms = int(time.time() * 1000)
        interval_ms = int(self.snapshot_interval * 1000)
        return now_ms - self._last_snapshot_ms >= interval_ms
    
    def _create_snapshot(self) -> PolymarketData:
        """Create a snapshot of current orderbook state."""
        now_ms = int(time.time() * 1000)
        
        # Get historical liquidity for collapse detection
        yes_liq_30s, no_liq_30s = self._liquidity_tracker.get_liquidity_at(30)
        yes_liq_60s, no_liq_60s = self._liquidity_tracker.get_liquidity_at(60)
        
        # Current liquidity
        current_yes_liq = self._yes_bids.best_size
        current_no_liq = self._no_bids.best_size
        
        # Add to liquidity tracker
        self._liquidity_tracker.add_snapshot(current_yes_liq, current_no_liq)
        
        # Calculate spread
        yes_bid = self._yes_bids.best_price
        yes_ask = self._yes_asks.best_price
        spread = yes_ask - yes_bid if yes_ask > 0 and yes_bid > 0 else 0.0
        
        # Calculate implied probability (mid price)
        implied_prob = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0.5
        
        # Check for liquidity collapse
        liquidity_collapsing = False
        if yes_liq_30s > 0:
            liquidity_collapsing = current_yes_liq < 0.6 * yes_liq_30s
        
        # Calculate orderbook imbalance
        total_yes_depth = self._yes_bids.total_depth + self._yes_asks.total_depth
        total_no_depth = self._no_bids.total_depth + self._no_asks.total_depth
        imbalance_ratio = total_yes_depth / total_no_depth if total_no_depth > 0 else 1.0
        
        self._last_snapshot_ms = now_ms
        
        return PolymarketData(
            market_id=self.market_id,
            timestamp_ms=now_ms,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            yes_liquidity_best=current_yes_liq,
            yes_depth_3=[OrderbookLevel(l.price, l.size) for l in self._yes_bids.levels[:3]],
            no_bid=self._no_bids.best_price,
            no_ask=self._no_asks.best_price,
            no_liquidity_best=current_no_liq,
            no_depth_3=[OrderbookLevel(l.price, l.size) for l in self._no_bids.levels[:3]],
            spread=spread,
            implied_probability=implied_prob,
            liquidity_30s_ago=yes_liq_30s,
            liquidity_60s_ago=yes_liq_60s,
            liquidity_collapsing=liquidity_collapsing,
            orderbook_imbalance_ratio=imbalance_ratio,
        )
    
    async def _handle_message(self, message: str) -> None:
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            
            msg_type = data.get("type", "")
            
            # Handle subscription confirmation
            if msg_type == "subscribed":
                self.logger.info("Subscription confirmed")
                return
            
            # Handle errors
            if msg_type == "error":
                self.logger.error("Polymarket error", error=data.get("message"))
                return
            
            # Handle orderbook updates
            if msg_type in ["snapshot", "update", "book"]:
                self._parse_orderbook_update(data)
                
                # Create snapshot at regular intervals
                if self._should_snapshot():
                    snapshot = self._create_snapshot()
                    self._notify_callbacks(snapshot)
            
            # Handle trade events (for additional context)
            if msg_type == "trade":
                # Could track recent trades for additional signals
                pass
                
        except json.JSONDecodeError as e:
            self.logger.error("JSON decode error", error=str(e))
        except Exception as e:
            self.logger.error("Error handling message", error=str(e))
    
    async def _receive_loop(self) -> None:
        """Main message receive loop."""
        while self._running:
            try:
                if not self._ws or not self._ws.open:
                    if not await self._connect():
                        await asyncio.sleep(1)
                        continue
                    await self._subscribe()
                
                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=30.0
                )
                
                self.health.last_message_ms = int(time.time() * 1000)
                await self._handle_message(message)
                
            except asyncio.TimeoutError:
                self.logger.warning("Receive timeout")
            except ConnectionClosed as e:
                self.logger.warning("Connection closed", code=e.code)
                self.health.connected = False
            except Exception as e:
                self.logger.error("Receive error", error=str(e))
                self.health.error_count += 1
                await asyncio.sleep(1)
    
    async def start(self) -> None:
        """Start the Polymarket feed."""
        self._running = True
        self.logger.info("Starting Polymarket feed")
        
        if await self._connect():
            await self._subscribe()
        
        await self._receive_loop()
    
    async def stop(self) -> None:
        """Stop the feed."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self.health.connected = False
        self.logger.info("Stopped Polymarket feed")
    
    def get_data(self) -> Optional[PolymarketData]:
        """Get current orderbook snapshot."""
        if not self._yes_bids.levels:
            return None
        return self._create_snapshot()
    
    def get_metrics(self) -> dict:
        """Get feed health metrics."""
        return {
            "name": "polymarket",
            "market_id": self.market_id,
            "connected": self.health.connected,
            "is_stale": self.health.is_stale,
            "age_ms": self.health.age_ms,
            "error_count": self.health.error_count,
            "yes_bid": self._yes_bids.best_price,
            "yes_ask": self._yes_asks.best_price,
            "spread": self._yes_asks.best_price - self._yes_bids.best_price if self._yes_asks.levels and self._yes_bids.levels else 0,
        }

