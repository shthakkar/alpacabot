# ================================================================
# DATA — market data fetching from Alpaca (1-minute bars)
# ================================================================
from datetime import datetime, timedelta, timezone

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests   import StockBarsRequest
from alpaca.data.timeframe  import TimeFrame, TimeFrameUnit

from config import API_KEY, API_SECRET, ET, DATA_FEED

_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_spy_bars(limit: int = 50, feed: str = DATA_FEED) -> pd.DataFrame:
    """
    Fetch the most recent SPY 1-minute bars from Alpaca.
    """
    start = datetime.now(timezone.utc) - timedelta(days=2)

    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        feed=feed,
    )
    bars = _client.get_stock_bars(req)
    df   = bars.df

    if isinstance(df.index, pd.MultiIndex):
        df = df.xs("SPY", level="symbol")

    df = df.sort_index()
    df.index = df.index.tz_convert(ET)
    return df.tail(limit)
