"""
Chainlink Oracle feed for BTC/USD price on Polygon.
Monitors on-chain oracle updates and calculates oracle age.
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

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
        poll_interval: float = 1.0,
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
        
        # Health
        self.connected = False
        self.last_poll_ms: int = 0
        self.error_count: int = 0
    
    async def _connect(self) -> bool:
        """Connect to Polygon RPC."""
        try:
            # Use HTTP provider for reliability
            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.rpc_url))
            
            # Add POA middleware for Polygon
            self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            
            # Verify connection
            if not await self._w3.is_connected():
                self.logger.error("Failed to connect to RPC")
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
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_oracle()
                await asyncio.sleep(self.poll_interval)
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
    
    def get_metrics(self) -> dict:
        """Get feed health metrics."""
        return {
            "name": "chainlink",
            "connected": self.connected,
            "last_poll_ms": self.last_poll_ms,
            "error_count": self.error_count,
            "current_price": self._current_data.current_value if self._current_data else None,
            "oracle_age_seconds": self._current_data.oracle_age_seconds if self._current_data else None,
            "avg_heartbeat_interval": self._heartbeat_tracker.avg_interval,
            "is_fast_heartbeat_mode": self._heartbeat_tracker.is_fast_heartbeat_mode(),
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

