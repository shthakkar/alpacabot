# ================================================================
# DATA — market data fetching from Alpaca
# Step 2: fetch_spy_bars() returns the most recent N 5-minute bars.
# ================================================================
from datetime import datetime, timedelta, timezone

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests   import StockBarsRequest
from alpaca.data.timeframe  import TimeFrame, TimeFrameUnit

from config import API_KEY, API_SECRET, ET, DATA_FEED

# Single shared client (cheap to create, but reuse anyway)
_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_spy_bars(limit: int = 50, feed: str = DATA_FEED) -> pd.DataFrame:
    """
    Fetch the most recent SPY 5-minute bars from Alpaca.

    Returns a DataFrame indexed by timestamp (America/New_York),
    sorted ascending, with columns: open, high, low, close, volume,
    trade_count, vwap. Length is up to `limit` (the most recent bars).
    """
    # Look back 3 calendar days. Generous enough to cover weekends
    # (Mon morning → last Fri bars) and after-hours gaps. We trim to
    # `limit` bars at the end.
    start = datetime.now(timezone.utc) - timedelta(days=3)

    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        feed=feed,
    )
    bars = _client.get_stock_bars(req)
    df   = bars.df

    # Alpaca returns a MultiIndex (symbol, timestamp) — flatten it
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs("SPY", level="symbol")

    df = df.sort_index()
    # Convert UTC -> ET so logs are human-readable
    df.index = df.index.tz_convert(ET)
    # Return the MOST RECENT `limit` bars
    return df.tail(limit)
