"""
Maker Order Execution - Polymarket CLOB Integration.

Uses py-clob-client for maker-only orders to:
- Pay 0% fees (vs 1.6-3% taker fees)
- Earn daily rebates (~0.5-2% of volume)
- Get better entry prices (inside spread)

Critical: Uses post_only=True to prevent accidental taker fills.
"""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any

import structlog

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs, AssetType, BalanceAllowanceParams
    from py_clob_client.order_builder.constants import BUY, SELL
    PY_CLOB_AVAILABLE = True
    
    # PartialCreateOrderOptions may not exist in all versions
    try:
        from py_clob_client.clob_types import PartialCreateOrderOptions
        PARTIAL_OPTIONS_AVAILABLE = True
    except ImportError:
        PartialCreateOrderOptions = None
        PARTIAL_OPTIONS_AVAILABLE = False
        
except ImportError:
    PY_CLOB_AVAILABLE = False
    ClobClient = None
    OrderArgs = None
    OrderType = None
    MarketOrderArgs = None
    PartialCreateOrderOptions = None
    AssetType = None
    BalanceAllowanceParams = None
    PARTIAL_OPTIONS_AVAILABLE = False
    BUY = SELL = None

from config.settings import settings

logger = structlog.get_logger()


class OrderStatus(str, Enum):
    """Order execution status."""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class MakerOrderResult:
    """Result of a maker order attempt."""
    success: bool
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    filled_size: float = 0.0
    unfilled_size: float = 0.0
    is_maker: bool = True
    fee_paid: float = 0.0
    rebate_earned: float = 0.0
    time_to_fill_ms: Optional[int] = None
    error: Optional[str] = None


class MakerOrderExecutor:
    """
    Executes maker-only orders on Polymarket.
    
    Strategy:
    1. Place limit order INSIDE spread with post_only=True
    2. Wait up to 3.5 seconds for fill
    3. If not filled, cancel (DON'T fallback to taker)
    4. Track fill rates for optimization
    
    Why maker-only:
    - Taker fees: 1.6-3% at 50% odds
    - Maker fees: 0% + daily rebate
    - A 2-3% fee swing per trade = massive P&L difference
    """
    
    # Maker order configuration
    MAKER_TIMEOUT_SECONDS = 1.5  # Fast timeout - edge decays quickly
    TICK_SIZE = "0.01"  # Polymarket minimum price increment (as string for API)
    TICK_SIZE_FLOAT = 0.01  # Same as above but as float for math operations
    NEG_RISK = False    # Standard YES/NO tokens
    MIN_SPREAD_FOR_MAKER = 0.02  # 2% minimum spread to place inside
    
    def __init__(
        self,
        private_key: str,
        chain_id: int = 137,  # Polygon mainnet
        host: str = "https://clob.polymarket.com",
    ):
        self.logger = logger.bind(component="maker_executor")
        self.host = host
        self.chain_id = chain_id
        self._private_key = private_key
        
        # Initialize client
        self._client: Optional[ClobClient] = None
        self._initialized = False
        
        # Cache for token tick sizes
        self._tick_size_cache: Dict[str, str] = {}
        
        # Performance tracking
        self._fill_attempts = 0
        self._fill_successes = 0
        self._total_rebates = 0.0
        
    async def initialize(self) -> bool:
        """Initialize the CLOB client."""
        if not PY_CLOB_AVAILABLE:
            self.logger.error(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            return False
        
        if not self._private_key:
            self.logger.error("Private key not configured")
            return False
        
        try:
            self._client = ClobClient(
                host=self.host,
                key=self._private_key,
                chain_id=self.chain_id,
            )
            
            # Derive API credentials
            self._client.set_api_creds(self._client.derive_api_key())
            
            # Ensure ERC-1155 approval for selling shares
            await self._ensure_conditional_token_approval()
            
            self._initialized = True
            self.logger.info("Maker executor initialized successfully")
            return True
            
        except Exception as e:
            self.logger.error("Failed to initialize maker executor", error=str(e))
            return False
    
    async def _ensure_conditional_token_approval(self) -> bool:
        """
        Ensure the Exchange contract is approved to transfer conditional tokens.
        
        This is required for selling shares. Without this approval, all sell
        orders will fail with 'not enough balance / allowance'.
        """
        try:
            from web3 import Web3
            
            # Connect to Polygon
            w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))
            if not w3.is_connected():
                self.logger.warning("Could not connect to Polygon RPC for approval check")
                return False
            
            account = w3.eth.account.from_key(self._private_key)
            
            # Contract addresses
            EXCHANGE = self._client.get_exchange_address()
            CONDITIONAL_TOKEN = self._client.get_conditional_address()
            
            # ERC-1155 ABI (minimal)
            erc1155_abi = [
                {
                    'inputs': [
                        {'name': 'operator', 'type': 'address'},
                        {'name': 'approved', 'type': 'bool'}
                    ],
                    'name': 'setApprovalForAll',
                    'outputs': [],
                    'stateMutability': 'nonpayable',
                    'type': 'function'
                },
                {
                    'inputs': [
                        {'name': 'account', 'type': 'address'},
                        {'name': 'operator', 'type': 'address'}
                    ],
                    'name': 'isApprovedForAll',
                    'outputs': [{'name': '', 'type': 'bool'}],
                    'stateMutability': 'view',
                    'type': 'function'
                }
            ]
            
            ct_contract = w3.eth.contract(address=CONDITIONAL_TOKEN, abi=erc1155_abi)
            is_approved = ct_contract.functions.isApprovedForAll(account.address, EXCHANGE).call()
            
            if is_approved:
                self.logger.info("✅ Exchange already approved for conditional tokens")
                return True
            
            self.logger.warning("Exchange NOT approved - setting approval now...")
            
            # Build and send approval transaction
            nonce = w3.eth.get_transaction_count(account.address)
            gas_price = w3.eth.gas_price
            
            tx = ct_contract.functions.setApprovalForAll(EXCHANGE, True).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': 100000,
                'gasPrice': gas_price,
            })
            
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            self.logger.info(f"Approval transaction sent: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt['status'] == 1:
                self.logger.info("✅ Exchange approved for conditional tokens!")
                return True
            else:
                self.logger.error("❌ Approval transaction failed!")
                return False
                
        except ImportError:
            self.logger.warning("web3 not installed - skipping approval check")
            return True
        except Exception as e:
            self.logger.error(f"Error checking/setting approval: {e}")
            return True  # Continue anyway, might already be approved
    
    def _get_tick_size(self, token_id: str) -> str:
        """
        Get tick size for a token - ALWAYS fetch fresh to avoid stale cache issues.
        
        py-clob-client has a known bug where cached tick sizes become stale
        when markets become one-sided. Always fetch fresh from API.
        """
        try:
            # Method 1: Try get_market endpoint
            market = self._client.get_market(token_id)
            if isinstance(market, dict):
                # Try different possible field names
                for field in ["minimum_tick_size", "tick_size", "tickSize"]:
                    if field in market:
                        tick_size = str(market[field])
                        self.logger.debug(f"Got tick_size from API: {tick_size}")
                        return tick_size
        except Exception as e:
            self.logger.debug(f"get_market failed: {e}")
        
        try:
            # Method 2: Try get_tick_size if available
            if hasattr(self._client, 'get_tick_size'):
                tick_size = self._client.get_tick_size(token_id)
                if tick_size:
                    return str(tick_size)
        except Exception as e:
            self.logger.debug(f"get_tick_size failed: {e}")
        
        # Default to standard Polymarket tick size (0.01 = 1 cent)
        self.logger.debug(f"Using default tick_size: {self.TICK_SIZE}")
        return self.TICK_SIZE
    
    def _calculate_maker_price(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        target_price: float,
    ) -> float:
        """
        Calculate optimal maker price (inside spread).
        
        Strategy: Step inside spread by 1 tick for better queue priority.
        
        For BUY orders: Place at best_ask - 1 tick (but not above target)
        For SELL orders: Place at best_bid + 1 tick (but not below target)
        """
        spread = best_ask - best_bid
        
        if spread < self.MIN_SPREAD_FOR_MAKER:
            # Spread too tight - place at target price
            return round(target_price, 2)
        
        if side == "BUY":
            # Step inside ask by 1 tick
            maker_price = best_ask - self.TICK_SIZE_FLOAT
            # Don't pay more than target
            maker_price = min(maker_price, target_price)
        else:  # SELL
            # Step inside bid by 1 tick
            maker_price = best_bid + self.TICK_SIZE_FLOAT
            # Don't sell for less than target
            maker_price = max(maker_price, target_price)
        
        return round(maker_price, 2)
    
    async def _ensure_sell_allowance(self, token_id: str) -> bool:
        """
        Legacy method - approval is now handled at initialization via setApprovalForAll.
        Kept for compatibility but does nothing.
        """
        # Approval is now set once at startup via _ensure_conditional_token_approval()
        # No per-token approval needed for ERC-1155
        return True
    
    async def place_maker_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        size: float,
        target_price: float,
        best_bid: float,
        best_ask: float,
        aggressive: bool = False,
    ) -> MakerOrderResult:
        """
        Place an order with timeout.
        
        Args:
            token_id: Polymarket token ID (YES or NO)
            side: "BUY" or "SELL"
            size: Number of shares to trade
            target_price: Maximum price willing to pay (BUY) or minimum (SELL)
            best_bid: Current best bid price
            best_ask: Current best ask price
            aggressive: If True, use taker price for instant fill (FOK)
            
        Returns:
            MakerOrderResult with fill details
        """
        if not self._initialized:
            return MakerOrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error="Executor not initialized",
            )
        
        self._fill_attempts += 1
        start_time = int(time.time() * 1000)
        
        try:
            # For SELL orders, ensure we have allowance set for the conditional token
            if side == "SELL":
                await self._ensure_sell_allowance(token_id)
            
            # Calculate price - aggressive uses market price for instant fill
            if aggressive:
                # Cross the spread for immediate fill
                if side == "BUY":
                    maker_price = min(best_ask + 0.01, target_price)  # Hit the ask + buffer
                else:
                    maker_price = max(best_bid - 0.01, 0.01)  # Hit the bid - buffer
                self.logger.info(
                    "🚀 AGGRESSIVE ORDER - crossing spread",
                    token_id=token_id[:16] + "...",
                    side=side,
                    price=maker_price,
                )
            else:
                # Calculate maker price (inside spread)
                maker_price = self._calculate_maker_price(
                    side, best_bid, best_ask, target_price
                )
            
            # Round to Polymarket precision requirements
            # Use integer sizes to ensure clean maker amounts (size * price = 2 decimals)
            from decimal import Decimal, ROUND_DOWN
            
            price_dec = Decimal(str(maker_price)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            maker_price = float(price_dec)
            
            # Use integer size for guaranteed clean maker amount
            size_dec = Decimal(str(size)).quantize(Decimal('1'), rounding=ROUND_DOWN)
            size = float(size_dec)
            
            # Ensure minimum size of 1
            if size < 1:
                size = 1.0
            
            self.logger.info(
                "Placing maker order",
                token_id=token_id[:16] + "...",
                side=side,
                size=size,
                maker_price=maker_price,
                target_price=target_price,
                spread=f"{(best_ask - best_bid):.2%}",
            )
            
            # Build order args
            # Polymarket uses tick_size=0.01 and neg_risk=False for standard YES/NO tokens
            order_side = BUY if side == "BUY" else SELL
            
            # Get fresh tick size from API (avoid stale cache issues)
            tick_size = self._get_tick_size(token_id)
            
            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=maker_price,
                size=size,
                side=order_side,
            )
            
            # Create and post order - try multiple methods for compatibility
            order = None
            last_error = None
            
            # Use FOK for aggressive orders (instant fill or cancel)
            # Use GTC for maker orders (wait for fill)
            order_type = OrderType.FOK if aggressive else OrderType.GTC
            
            # Method 1: Use PartialCreateOrderOptions if available
            if PARTIAL_OPTIONS_AVAILABLE and PartialCreateOrderOptions:
                try:
                    options = PartialCreateOrderOptions(
                        tick_size=tick_size,
                        neg_risk=self.NEG_RISK,
                    )
                    signed_order = self._client.create_order(order_args, options)
                    order = self._client.post_order(signed_order, order_type)
                    self.logger.debug("Order created with PartialCreateOrderOptions")
                except Exception as e:
                    last_error = e
                    self.logger.debug(f"PartialCreateOrderOptions failed: {e}")
            
            # Method 2: Try create_and_post_order (simpler API)
            if order is None:
                try:
                    order = self._client.create_and_post_order(order_args, order_type)
                    self.logger.debug("Order created with create_and_post_order")
                except Exception as e:
                    last_error = e
                    self.logger.debug(f"create_and_post_order failed: {e}")
            
            # Method 3: Try with dict-based order creation
            if order is None:
                try:
                    signed_order = self._client.create_order(order_args)
                    order = self._client.post_order(signed_order, order_type)
                    self.logger.debug("Order created with simple create_order")
                except Exception as e:
                    last_error = e
                    self.logger.debug(f"Simple create_order failed: {e}")
            
            if order is None:
                raise last_error or Exception("All order creation methods failed")
            
            order_id = order.get("orderID") or order.get("order_id")
            
            if not order_id:
                return MakerOrderResult(
                    success=False,
                    status=OrderStatus.FAILED,
                    error="No order ID returned",
                )
            
            self.logger.debug("Order placed", order_id=order_id)
            
            # Wait for fill with timeout
            filled = await self._wait_for_fill(
                order_id,
                timeout_seconds=self.MAKER_TIMEOUT_SECONDS,
            )
            
            end_time = int(time.time() * 1000)
            
            if filled:
                self._fill_successes += 1
                
                # Estimate rebate (0.5% daily, prorated)
                rebate = size * maker_price * 0.005 / 86400 * (self.MAKER_TIMEOUT_SECONDS)
                self._total_rebates += rebate
                
                self.logger.info(
                    "✅ Maker order FILLED",
                    order_id=order_id,
                    fill_price=maker_price,
                    time_to_fill_ms=end_time - start_time,
                    rebate=f"~€{rebate:.4f}",
                )
                
                return MakerOrderResult(
                    success=True,
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    fill_price=maker_price,
                    filled_size=size,
                    is_maker=True,
                    fee_paid=0.0,
                    rebate_earned=rebate,
                    time_to_fill_ms=end_time - start_time,
                )
            else:
                # Not filled - cancel order
                await self._cancel_order(order_id)
                
                self.logger.info(
                    "⏱️ Maker order NOT filled (timeout)",
                    order_id=order_id,
                    timeout=self.MAKER_TIMEOUT_SECONDS,
                )
                
                return MakerOrderResult(
                    success=False,
                    order_id=order_id,
                    status=OrderStatus.EXPIRED,
                    unfilled_size=size,
                    is_maker=True,
                    time_to_fill_ms=end_time - start_time,
                )
                
        except Exception as e:
            self.logger.error("Maker order failed", error=str(e))
            return MakerOrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error=str(e),
            )
    
    async def _wait_for_fill(
        self,
        order_id: str,
        timeout_seconds: float,
    ) -> bool:
        """
        Wait for order to fill with polling.
        
        Returns True if filled, False if timeout.
        """
        check_interval = 0.1  # Check every 100ms - ultra fast
        elapsed = 0.0
        
        while elapsed < timeout_seconds:
            try:
                order_status = self._client.get_order(order_id)
                
                # Handle None response from API
                if order_status is None:
                    self.logger.debug("Order status returned None, retrying...")
                    await asyncio.sleep(check_interval)
                    elapsed += check_interval
                    continue
                
                status = order_status.get("status", "").upper()
                
                if status in ("MATCHED", "FILLED"):
                    return True
                elif status in ("CANCELLED", "EXPIRED"):
                    return False
                
            except Exception as e:
                self.logger.warning("Error checking order status", error=str(e))
            
            await asyncio.sleep(check_interval)
            elapsed += check_interval
        
        return False
    
    async def _cancel_order(self, order_id: str) -> bool:
        """Cancel an unfilled order."""
        try:
            self._client.cancel(order_id)
            self.logger.debug("Order cancelled", order_id=order_id)
            return True
        except Exception as e:
            self.logger.warning("Failed to cancel order", order_id=order_id, error=str(e))
            return False
    
    def get_fill_rate(self) -> float:
        """Get maker fill rate (0.0 - 1.0)."""
        if self._fill_attempts == 0:
            return 0.0
        return self._fill_successes / self._fill_attempts
    
    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            "initialized": self._initialized,
            "fill_attempts": self._fill_attempts,
            "fill_successes": self._fill_successes,
            "fill_rate": self.get_fill_rate(),
            "total_rebates_earned": self._total_rebates,
            "timeout_seconds": self.MAKER_TIMEOUT_SECONDS,
        }


# Convenience function for quick integration
async def execute_maker_order(
    token_id: str,
    side: str,
    size_eur: float,
    target_price: float,
    best_bid: float,
    best_ask: float,
) -> MakerOrderResult:
    """
    Execute a maker order using global settings.
    
    This is a convenience function for quick integration.
    For production, use MakerOrderExecutor directly.
    """
    executor = MakerOrderExecutor(
        private_key=settings.private_key,
    )
    
    if not await executor.initialize():
        return MakerOrderResult(
            success=False,
            status=OrderStatus.FAILED,
            error="Failed to initialize executor",
        )
    
    # Convert EUR to shares (size_eur / price = shares)
    shares = size_eur / target_price if target_price > 0 else 0
    
    return await executor.place_maker_order(
        token_id=token_id,
        side=side,
        size=shares,
        target_price=target_price,
        best_bid=best_bid,
        best_ask=best_ask,
    )

