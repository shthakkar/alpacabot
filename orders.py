# ================================================================
# ORDERS — order placement + position/order management
# Step 6: end-to-end BUY → SELL plumbing.
# Used by Step 7's main loop for entry/avg-up/exit.
#
# Conventions:
#   • All BUYs are marketable limit at the ASK (fills like a market
#     order but with price protection).
#   • All SELLs are marketable limit at the BID (same idea).
#   • Time-in-force = DAY (options can't be GTC for our use case).
# ================================================================
import time
from typing  import Optional, List

from alpaca.trading.client    import TradingClient
from alpaca.trading.requests  import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    GetOrdersRequest,
    ReplaceOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums     import (
    OrderSide, TimeInForce, OrderStatus, QueryOrderStatus, PositionIntent,
    OrderClass,
)
from alpaca.common.exceptions import APIError

from config import API_KEY, API_SECRET, PAPER

_trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)

# Terminal states — once an order is in one of these, it won't change
_TERMINAL = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.REPLACED,
}


# ----------------------------------------------------------------
# Market state
# ----------------------------------------------------------------
def is_market_open() -> bool:
    """True if NYSE is currently open (Alpaca clock)."""
    return _trading.get_clock().is_open


# ----------------------------------------------------------------
# Positions
# ----------------------------------------------------------------
def get_buying_power() -> float:
    """Current options buying power from the account."""
    return float(_trading.get_account().cash)


def list_positions() -> list:
    """All currently open positions."""
    return _trading.get_all_positions()


def get_position(symbol: str):
    """Position object for `symbol`, or None if flat. Raises on API failure."""
    try:
        return _trading.get_open_position(symbol)
    except APIError as e:
        if "position does not exist" in str(e).lower() or getattr(e, "status_code", None) == 404:
            return None
        raise


def close_any_position(symbol: str) -> bool:
    """
    Safety helper: if there's an open position in `symbol`, close it.
    Returns True if it had to close something.
    """
    pos = get_position(symbol)
    if pos is None:
        return False
    _trading.close_position(symbol)
    return True


# ----------------------------------------------------------------
# Open orders
# ----------------------------------------------------------------
def list_open_orders(symbol: Optional[str] = None) -> list:
    """All OPEN (non-terminal) orders, optionally filtered by symbol."""
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = _trading.get_orders(req)
    if symbol:
        orders = [o for o in orders if o.symbol == symbol]
    return orders


def cancel_order(order_id: str) -> None:
    """Cancel one order by id. No-op if already terminal."""
    _trading.cancel_order_by_id(order_id)


def cancel_all_orders(symbol: Optional[str] = None) -> int:
    """
    Cancel all open orders (optionally filtered to one symbol).
    Returns the count we attempted to cancel.
    """
    open_orders = list_open_orders(symbol)
    for o in open_orders:
        try:
            cancel_order(o.id)
        except Exception as e:
            print(f"   ⚠ couldn't cancel {o.id}: {e}")
    return len(open_orders)


# ----------------------------------------------------------------
# Order submission
# ----------------------------------------------------------------
def submit_buy(symbol: str, qty: int, limit_price: float):
    """Marketable limit BUY (limit @ ask gives near-instant fill)."""
    req = LimitOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.BUY,
        time_in_force   = TimeInForce.DAY,
        limit_price     = round(limit_price, 2),
        position_intent = PositionIntent.BUY_TO_OPEN,
    )
    return _trading.submit_order(req)


def submit_sell(symbol: str, qty: int, limit_price: float):
    """Marketable limit SELL (limit @ bid gives near-instant fill)."""
    req = LimitOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.SELL,
        time_in_force   = TimeInForce.DAY,
        limit_price     = round(limit_price, 2),
        position_intent = PositionIntent.SELL_TO_CLOSE,
    )
    return _trading.submit_order(req)


# ----------------------------------------------------------------
# Order submission — additional types for Step 7 (live bot)
# ----------------------------------------------------------------
def submit_market_buy(symbol: str, qty: int):
    """Plain MARKET BUY (used for the entry order in Step 7)."""
    req = MarketOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.BUY,
        time_in_force   = TimeInForce.DAY,
        position_intent = PositionIntent.BUY_TO_OPEN,
    )
    return _trading.submit_order(req)


def submit_market_buy_bracket(symbol: str, qty: int,
                              take_profit_price: float,
                              stop_loss_price: float):
    """
    BRACKET MARKET BUY — submits the entry plus the +X% take-profit LIMIT
    and -Y% stop-loss STOP as atomic child orders. Atomic submission
    sidesteps Alpaca's "potential wash trade" and "uncovered option"
    rejections that fire on independent post-fill submissions.

    After the parent fills, the children become live. Their IDs are
    discoverable via the order's `legs` attribute or by polling.
    """
    req = MarketOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.BUY,
        time_in_force   = TimeInForce.DAY,
        position_intent = PositionIntent.BUY_TO_OPEN,
        order_class     = OrderClass.BRACKET,
        take_profit     = TakeProfitRequest(
            limit_price = round(take_profit_price, 2),
        ),
        stop_loss       = StopLossRequest(
            stop_price  = round(stop_loss_price, 2),
        ),
    )
    return _trading.submit_order(req)


def submit_market_sell(symbol: str, qty: int):
    """Plain MARKET SELL (used for time-exit / EOD force-close)."""
    req = MarketOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.SELL,
        time_in_force   = TimeInForce.DAY,
        position_intent = PositionIntent.SELL_TO_CLOSE,
    )
    return _trading.submit_order(req)


def submit_stop_limit_buy(symbol: str, qty: int,
                           stop_price: float, limit_price: float):
    """
    STOP-LIMIT BUY — used for avg-up #1 and #2. Triggers a LIMIT BUY
    at `limit_price` when price reaches `stop_price`. Limit is set
    a couple cents above the stop so the fill is near-immediate.
    """
    req = StopLimitOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.BUY,
        time_in_force   = TimeInForce.DAY,
        stop_price      = round(stop_price,  2),
        limit_price     = round(limit_price, 2),
        position_intent = PositionIntent.BUY_TO_OPEN,
    )
    return _trading.submit_order(req)


def submit_stop_market_sell(symbol: str, qty: int, stop_price: float):
    """
    STOP MARKET SELL — used for the stop loss. Fires a MARKET SELL
    when price drops to `stop_price`. No limit (fast exit, accept
    minor slippage).
    """
    req = StopOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.SELL,
        time_in_force   = TimeInForce.DAY,
        stop_price      = round(stop_price, 2),
        position_intent = PositionIntent.SELL_TO_CLOSE,
    )
    return _trading.submit_order(req)


def submit_limit_sell(symbol: str, qty: int, limit_price: float):
    """
    LIMIT SELL — used for the profit target. Fills when bid reaches
    `limit_price`.
    """
    req = LimitOrderRequest(
        symbol          = symbol,
        qty             = qty,
        side            = OrderSide.SELL,
        time_in_force   = TimeInForce.DAY,
        limit_price     = round(limit_price, 2),
        position_intent = PositionIntent.SELL_TO_CLOSE,
    )
    return _trading.submit_order(req)


def replace_order(order_id: str, qty: Optional[int] = None,
                  stop_price: Optional[float] = None,
                  limit_price: Optional[float] = None):
    """
    Atomically update an existing working order (PATCH /v2/orders/{id}).
    Returns the NEW order object — has a new id; original goes to 'replaced'.
    Inherits side, type, time_in_force, and position_intent from the original.
    """
    fields = {}
    if qty         is not None: fields["qty"]         = qty
    if stop_price  is not None: fields["stop_price"]  = round(stop_price,  2)
    if limit_price is not None: fields["limit_price"] = round(limit_price, 2)
    return _trading.replace_order_by_id(order_id, ReplaceOrderRequest(**fields))


def get_order(order_id: str):
    """Fetch latest state of a single order."""
    return _trading.get_order_by_id(order_id)


def is_terminal(order_status) -> bool:
    """True if the order is in a terminal state (won't change anymore)."""
    return order_status in _TERMINAL


# ----------------------------------------------------------------
# Fill polling
# ----------------------------------------------------------------
def wait_for_fill(order_id: str, timeout: float = 30.0, poll: float = 0.5):
    """
    Poll the order until it reaches a terminal status or `timeout`
    seconds elapse. Returns the final order object. Raises on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        o = _trading.get_order_by_id(order_id)
        if o.status in _TERMINAL:
            return o
        time.sleep(poll)
    raise TimeoutError(f"Order {order_id} did not reach terminal status within {timeout}s")
