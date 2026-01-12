"""
Chainlink Oracle feed for BTC/USD price on Polygon.
Monitors on-chain oracle updates and calculates oracle age.
"""

import asyncio
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import certifi
import structlog
from web3 import AsyncWeb3
from web3.providers import WebSocketProvider
from web3.middleware import ExtraDataToPOAMiddleware

from src.models.schemas import OracleData

logger = structlog.get_logger()


# Chainlink Aggregator V3 Interface ABI (minimal for reading)
AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "description",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint80", "name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class WindowPriceTracker:
    """
    Tracks Chainlink prices at 15-minute window boundaries.
    
    This is CRITICAL for 15-min up/down markets:
    - Market resolves UP if: price_at_end >= price_at_start
    - Market resolves DOWN if: price_at_end < price_at_start
    
    We need to track the price at window START to determine true direction.
    """
    # Window interval in seconds (15 minutes)
    interval_seconds: int = 900
    
    # Cached window start prices: window_end_ts -> price
    _window_start_prices: dict = field(default_factory=dict)
    
    # Current price (latest from Chainlink)
    current_price: float = 0.0
    
    def update_price(self, price: float, timestamp_seconds: int) -> None:
        """Update current price and record window start price if at boundary."""
        self.current_price = price
        
        # Calculate which window we're in
        window_end = ((timestamp_seconds // self.interval_seconds) + 1) * self.interval_seconds
        window_start = window_end - self.interval_seconds
        
        # If we don't have a start price for this window yet, record it
        if window_end not in self._window_start_prices:
            self._window_start_prices[window_end] = price
            # Cleanup old entries (keep last 10 windows)
            self._cleanup_old_windows(window_end)
    
    def _cleanup_old_windows(self, current_window_end: int) -> None:
        """Remove old window entries to prevent memory growth."""
        cutoff = current_window_end - (10 * self.interval_seconds)
        old_keys = [k for k in self._window_start_prices if k < cutoff]
        for k in old_keys:
            del self._window_start_prices[k]
    
    def get_window_start_price(self, window_end_ts: int = 0) -> float:
        """Get the start price for a specific window."""
        if window_end_ts == 0:
            # Use current window
            now = int(time.time())
            window_end_ts = ((now // self.interval_seconds) + 1) * self.interval_seconds
        return self._window_start_prices.get(window_end_ts, 0.0)
    
    def get_window_move_pct(self, window_end_ts: int = 0) -> float:
        """
        Calculate current price move relative to window start.
        
        This is the KEY metric for determining UP/DOWN direction!
        
        Returns:
            Percentage move from window start (positive = up, negative = down)
        """
        start_price = self.get_window_start_price(window_end_ts)
        if start_price <= 0 or self.current_price <= 0:
            return 0.0
        return (self.current_price - start_price) / start_price
    
    def get_current_window_info(self) -> dict:
        """Get info about the current window."""
        now = int(time.time())
        window_end = ((now // self.interval_seconds) + 1) * self.interval_seconds
        window_start = window_end - self.interval_seconds
        time_remaining = window_end - now
        
        return {
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "time_remaining_seconds": time_remaining,
            "window_start_price": self.get_window_start_price(window_end),
            "current_price": self.current_price,
            "window_move_pct": self.get_window_move_pct(window_end),
        }


@dataclass
class HeartbeatTracker:
    """Tracks oracle heartbeat intervals for prediction."""
    intervals: deque = field(default_factory=lambda: deque(maxlen=20))
    last_update_timestamps: deque = field(default_factory=lambda: deque(maxlen=20))
    
    def add_update(self, timestamp_ms: int) -> None:
        """Record a new oracle update."""
        if self.last_update_timestamps:
            interval = (timestamp_ms - self.last_update_timestamps[-1]) / 1000
            if interval > 0:
                self.intervals.append(interval)
        self.last_update_timestamps.append(timestamp_ms)
    
    @property
    def avg_interval(self) -> float:
        """Get average heartbeat interval in seconds."""
        if not self.intervals:
            return 60.0  # Default assumption
        return sum(self.intervals) / len(self.intervals)
    
    @property
    def recent_intervals(self) -> list[float]:
        """Get recent heartbeat intervals."""
        return list(self.intervals)[-5:]
    
    def estimate_next_update(self, current_time_ms: int) -> int:
        """Estimate next oracle update timestamp."""
        if not self.last_update_timestamps:
            return current_time_ms + int(self.avg_interval * 1000)
        
        last_update = self.last_update_timestamps[-1]
        return last_update + int(self.avg_interval * 1000)
    
    def is_fast_heartbeat_mode(self, threshold: float = 35.0) -> bool:
        """Check if oracle is in fast heartbeat mode."""
        recent = self.recent_intervals
        if len(recent) < 3:
            return False
        avg_recent = sum(recent) / len(recent)
        return avg_recent < threshold


class ChainlinkFeed:
    """
    Chainlink Oracle feed for monitoring BTC/USD price on Polygon.
    
    Uses polling with short intervals to detect updates quickly.
    WebSocket subscription to new blocks helps time polls efficiently.
    """
    
    def __init__(
        self,
        feed_address: str,
        rpc_url: str,
        ws_url: Optional[str] = None,
        poll_interval: float = 5.0,  # 5 second poll (oracle is secondary signal)
    ):
        self.feed_address = feed_address
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.poll_interval = poll_interval
        
        self.logger = logger.bind(feed="chainlink")
        
        # State
        self._running = False
        self._w3: Optional[AsyncWeb3] = None
        self._contract = None
        self._decimals: int = 8
        
        # Current oracle data
        self._current_data: Optional[OracleData] = None
        self._last_round_id: int = 0
        
        # Heartbeat tracking
        self._heartbeat_tracker = HeartbeatTracker()
        
        # Window price tracking (CRITICAL for 15-min up/down markets)
        self._window_tracker = WindowPriceTracker()
        
        # Health
        self.connected = False
        self.last_poll_ms: int = 0
        self.error_count: int = 0
    
    async def _connect(self) -> bool:
        """Connect to Polygon RPC."""
        try:
            # Create SSL context with certifi certificates
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            
            # Create aiohttp connector with SSL context
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            session = aiohttp.ClientSession(connector=connector)
            
            # Use HTTP provider with custom session for SSL
            self._w3 = AsyncWeb3(
                AsyncWeb3.AsyncHTTPProvider(
                    self.rpc_url,
                    request_kwargs={"ssl": ssl_context}
                )
            )
            
            # Add POA middleware for Polygon
            self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            
            # Verify connection by getting block number (is_connected() is unreliable)
            try:
                block_num = await self._w3.eth.block_number
                self.logger.info("RPC connected", block_number=block_num)
            except Exception as e:
                self.logger.error("Failed to connect to RPC", error=str(e))
                return False
            
            # Get contract instance
            self._contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.feed_address),
                abi=AGGREGATOR_V3_ABI,
            )
            
            # Get decimals
            self._decimals = await self._contract.functions.decimals().call()
            description = await self._contract.functions.description().call()
            
            self.logger.info(
                "Connected to Chainlink feed",
                address=self.feed_address,
                decimals=self._decimals,
                description=description,
            )
            
            self.connected = True
            return True
            
        except Exception as e:
            self.logger.error("Connection failed", error=str(e))
            self.error_count += 1
            return False
    
    async def _poll_oracle(self) -> Optional[OracleData]:
        """Poll the oracle for latest data."""
        if not self._contract:
            return None
        
        try:
            # Get latest round data
            result = await self._contract.functions.latestRoundData().call()
            round_id, answer, started_at, updated_at, answered_in_round = result
            
            current_time_ms = int(time.time() * 1000)
            updated_at_ms = updated_at * 1000
            oracle_age = (current_time_ms - updated_at_ms) / 1000
            
            # Convert price using decimals
            price = answer / (10 ** self._decimals)
            
            # Update window price tracker (CRITICAL for 15-min market direction)
            self._window_tracker.update_price(price, int(time.time()))
            
            # Check for new round (oracle update)
            if round_id > self._last_round_id and self._last_round_id > 0:
                self._heartbeat_tracker.add_update(updated_at_ms)
                self.logger.info(
                    "Oracle updated",
                    round_id=round_id,
                    price=price,
                    oracle_age=oracle_age,
                )
            
            self._last_round_id = round_id
            self.last_poll_ms = current_time_ms
            
            # Build oracle data
            oracle_data = OracleData(
                current_value=price,
                last_update_timestamp_ms=updated_at_ms,
                oracle_age_seconds=oracle_age,
                round_id=round_id,
                recent_heartbeat_intervals=self._heartbeat_tracker.recent_intervals,
                avg_heartbeat_interval=self._heartbeat_tracker.avg_interval,
                next_heartbeat_estimate_ms=self._heartbeat_tracker.estimate_next_update(current_time_ms),
                is_fast_heartbeat_mode=self._heartbeat_tracker.is_fast_heartbeat_mode(),
            )
            
            self._current_data = oracle_data
            return oracle_data
            
        except Exception as e:
            self.logger.error("Poll failed", error=str(e))
            self.error_count += 1
            return None
    
    async def _poll_loop(self) -> None:
        """
        Smart polling loop with adaptive intervals.
        
        - Normal: Poll every 5 seconds (reduced from 2s since oracle is secondary)
        - Near expected update: Poll every 2 seconds
        - If cache is fresh (<2s): Skip poll
        """
        while self._running:
            try:
                current_time_ms = int(time.time() * 1000)
                
                # Skip if cache is very fresh (< 2 seconds old)
                if self._current_data and self.last_poll_ms:
                    cache_age_ms = current_time_ms - self.last_poll_ms
                    if cache_age_ms < 2000:
                        await asyncio.sleep(1)
                        continue
                
                # Check if we're near expected update
                next_update_ms = self._heartbeat_tracker.estimate_next_update(current_time_ms)
                time_to_update = (next_update_ms - current_time_ms) / 1000
                
                # Poll now
                await self._poll_oracle()
                
                # Adaptive sleep based on expected next update
                if 0 < time_to_update < 10:
                    # Near expected update - poll more frequently
                    await asyncio.sleep(2)
                else:
                    # Normal interval - poll less frequently (save RPC calls)
                    await asyncio.sleep(self.poll_interval)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Poll loop error", error=str(e))
                await asyncio.sleep(self.poll_interval * 2)
    
    async def start(self) -> None:
        """Start the oracle feed."""
        self._running = True
        self.logger.info("Starting Chainlink feed")
        
        if not await self._connect():
            self.logger.error("Failed to start - connection failed")
            return
        
        # Initial poll
        await self._poll_oracle()
        
        # Start polling loop
        await self._poll_loop()
    
    async def stop(self) -> None:
        """Stop the oracle feed."""
        self._running = False
        self.connected = False
        self.logger.info("Stopped Chainlink feed")
    
    def get_data(self) -> Optional[OracleData]:
        """Get current oracle data."""
        if self._current_data:
            # Update oracle age
            current_time_ms = int(time.time() * 1000)
            self._current_data.oracle_age_seconds = (
                current_time_ms - self._current_data.last_update_timestamp_ms
            ) / 1000
        return self._current_data
    
    def get_window_info(self) -> dict:
        """
        Get current 15-minute window price info.
        
        CRITICAL for 15-min up/down markets:
        - window_start_price: Chainlink price at window start
        - window_move_pct: Current move from window start (+ = up, - = down)
        """
        return self._window_tracker.get_current_window_info()
    
    def get_window_move_pct(self, window_end_ts: int = 0) -> float:
        """Get the percentage move from window start to current price."""
        return self._window_tracker.get_window_move_pct(window_end_ts)
    
    def get_window_start_price(self, window_end_ts: int = 0) -> float:
        """Get the Chainlink price at the start of the specified window."""
        return self._window_tracker.get_window_start_price(window_end_ts)
    
    def get_metrics(self) -> dict:
        """Get feed health metrics."""
        window_info = self._window_tracker.get_current_window_info()
        return {
            "name": "chainlink",
            "connected": self.connected,
            "last_poll_ms": self.last_poll_ms,
            "error_count": self.error_count,
            "current_price": self._current_data.current_value if self._current_data else None,
            "oracle_age_seconds": self._current_data.oracle_age_seconds if self._current_data else None,
            "avg_heartbeat_interval": self._heartbeat_tracker.avg_interval,
            "is_fast_heartbeat_mode": self._heartbeat_tracker.is_fast_heartbeat_mode(),
            "window_start_price": window_info.get("window_start_price", 0),
            "window_move_pct": window_info.get("window_move_pct", 0),
            "window_time_remaining": window_info.get("time_remaining_seconds", 0),
        }


class ChainlinkFeedWithEvents(ChainlinkFeed):
    """
    Extended Chainlink feed that also listens for on-chain events.
    More responsive to oracle updates but requires WebSocket RPC.
    """
    
    async def _subscribe_to_events(self) -> None:
        """Subscribe to AnswerUpdated events."""
        if not self.ws_url:
            return
        
        try:
            from web3 import AsyncWeb3
            
            ws_w3 = await AsyncWeb3.persistent_websocket(
                WebSocketProvider(self.ws_url)
            )
            
            # Subscribe to new blocks and check for updates
            async for block in ws_w3.eth.subscribe("newHeads"):
                if not self._running:
                    break
                
                # Poll oracle on each new block
                await self._poll_oracle()
                
        except Exception as e:
            self.logger.error("Event subscription error", error=str(e))
    
    async def start(self) -> None:
        """Start with both polling and event subscription."""
        self._running = True
        self.logger.info("Starting Chainlink feed with events")
        
        if not await self._connect():
            self.logger.error("Failed to start - connection failed")
            return
        
        # Initial poll
        await self._poll_oracle()
        
        # Run polling and event subscription concurrently
        if self.ws_url:
            await asyncio.gather(
                self._poll_loop(),
                self._subscribe_to_events(),
            )
        else:
            await self._poll_loop()

