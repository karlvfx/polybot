"""
Trading execution modules.

- maker_orders: Maker-only order execution for 0% fees + rebates
- real_trader: Real trade execution with position management
"""

from src.trading.maker_orders import (
    MakerOrderExecutor,
    MakerOrderResult,
    OrderStatus,
    execute_maker_order,
    PY_CLOB_AVAILABLE,
)

from src.trading.real_trader import (
    RealTrader,
    RealPosition,
)

__all__ = [
    # Maker orders
    "MakerOrderExecutor",
    "MakerOrderResult",
    "OrderStatus",
    "execute_maker_order",
    "PY_CLOB_AVAILABLE",
    # Real trader
    "RealTrader",
    "RealPosition",
]

