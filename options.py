# ================================================================
# OPTIONS — contract resolution + quotes
# Step 5: given a direction (CALL/PUT) and current SPY price, find
# the exact tradeable option contract and its bid/ask.
#
# Rules:
#   • Expiry  = 2nd-soonest active expiry strictly after today
#               (sourced from Alpaca chain; holidays/weekends are
#                handled automatically because Alpaca only lists
#                contracts on real trading days).
#   • Strike  = nearest OTM listed:
#                 CALL → ceil(spy_price)
#                 PUT  → floor(spy_price)
# ================================================================
import math
from datetime import datetime, timedelta
from typing   import Optional

from alpaca.trading.client          import TradingClient
from alpaca.trading.requests        import GetOptionContractsRequest
from alpaca.trading.enums           import AssetStatus, ContractType
from alpaca.data.historical.option  import OptionHistoricalDataClient
from alpaca.data.requests           import OptionLatestQuoteRequest

from config import API_KEY, API_SECRET, PAPER, ET, OPTION_CHAIN_DAYS

# Shared clients
_trading      = TradingClient(API_KEY, API_SECRET, paper=PAPER)
_option_data  = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ----------------------------------------------------------------
# Account-level check (fail loudly if options aren't enabled)
# ----------------------------------------------------------------
def options_enabled() -> tuple[bool, int]:
    """
    Returns (enabled, level). Level >= 2 is required to BUY calls/puts.
    Level 0 = none, 1 = covered calls, 2 = long puts/calls, 3 = spreads,
    4 = naked.
    """
    account = _trading.get_account()
    level   = int(getattr(account, "options_trading_level", 0) or 0)
    return level >= 2, level


# ----------------------------------------------------------------
# Strike helpers
# ----------------------------------------------------------------
def nearest_otm_strike(direction: str, spy_price: float) -> int:
    """
    CALL → first whole-dollar strike ABOVE spy_price (OTM)
    PUT  → first whole-dollar strike BELOW spy_price (OTM)
    """
    if direction.upper() == "CALL":
        return int(math.ceil(spy_price))
    elif direction.upper() == "PUT":
        return int(math.floor(spy_price))
    else:
        raise ValueError(f"direction must be 'CALL' or 'PUT', got {direction!r}")


# ----------------------------------------------------------------
# Chain fetch + filtering
# ----------------------------------------------------------------
def fetch_chain(today: Optional[datetime] = None):
    """
    Fetch active SPY option contracts expiring between today and
    today + OPTION_CHAIN_DAYS. Returns the raw list.
    """
    if today is None:
        today = datetime.now(ET).date()

    req = GetOptionContractsRequest(
        underlying_symbols     = ["SPY"],
        status                 = AssetStatus.ACTIVE,
        expiration_date_gte    = today,
        expiration_date_lte    = today + timedelta(days=OPTION_CHAIN_DAYS),
        limit                  = 10_000,
    )
    return _trading.get_option_contracts(req).option_contracts


def pick_expiry(contracts, today=None) -> "date":
    """
    Pick the 2nd-soonest expiry strictly after `today`. Falls back to
    the 1st if there's only one (shouldn't happen with a 7-day window
    on SPY, but handle gracefully).
    """
    if today is None:
        today = datetime.now(ET).date()

    expirations = sorted({c.expiration_date for c in contracts if c.expiration_date > today})
    if not expirations:
        raise RuntimeError("No future expirations in chain — chain window too narrow?")
    return expirations[1] if len(expirations) >= 2 else expirations[0]


def pick_contract(contracts, direction: str, expiry, spy_price: float):
    """
    From the chain, pick the contract for the given direction/expiry
    closest to the desired OTM strike. If the exact OTM strike isn't
    listed, fall back to the nearest available OTM strike in the
    correct direction.
    """
    target_strike = nearest_otm_strike(direction, spy_price)
    side          = ContractType.CALL if direction.upper() == "CALL" else ContractType.PUT

    same_day = [
        c for c in contracts
        if c.expiration_date == expiry and c.type == side
    ]
    if not same_day:
        raise RuntimeError(f"No {direction} contracts found for {expiry}")

    # Find the strike >= target (CALL) or <= target (PUT) — i.e., still OTM —
    # closest to spy_price.
    if direction.upper() == "CALL":
        otm = [c for c in same_day if float(c.strike_price) >= target_strike]
        otm.sort(key=lambda c: float(c.strike_price))            # ascending
    else:
        otm = [c for c in same_day if float(c.strike_price) <= target_strike]
        otm.sort(key=lambda c: float(c.strike_price), reverse=True)  # descending

    if not otm:
        # Fallback: nearest-by-distance regardless of side
        same_day.sort(key=lambda c: abs(float(c.strike_price) - spy_price))
        return same_day[0]

    return otm[0]


# ----------------------------------------------------------------
# Quotes
# ----------------------------------------------------------------
def get_quote(symbol: str) -> dict:
    """
    Latest NBBO quote for an option contract.
    Returns dict with bid, ask, mid, spread, timestamp.
    """
    req    = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = _option_data.get_option_latest_quote(req)
    q      = quotes[symbol]
    bid    = float(q.bid_price)
    ask    = float(q.ask_price)
    return {
        "symbol":    symbol,
        "bid":       bid,
        "ask":       ask,
        "mid":       (bid + ask) / 2 if bid and ask else float("nan"),
        "spread":    ask - bid       if bid and ask else float("nan"),
        "timestamp": q.timestamp,
    }


# ----------------------------------------------------------------
# Convenience wrapper used by the main loop
# ----------------------------------------------------------------
def resolve_option(direction: str, spy_price: float) -> dict:
    """
    One-shot: from a (direction, spy_price), return everything the
    main loop needs to place an order.

    Returns dict:
        symbol, expiry, strike, bid, ask, mid, spread, timestamp
    """
    today    = datetime.now(ET).date()
    chain    = fetch_chain(today)
    expiry   = pick_expiry(chain, today)
    contract = pick_contract(chain, direction, expiry, spy_price)
    quote    = get_quote(contract.symbol)
    return {
        **quote,
        "expiry": expiry,
        "strike": float(contract.strike_price),
    }
