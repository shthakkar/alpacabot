# ================================================================
# DATA — fetch today's 9:30 ET opening 5-min candle with retry
# ================================================================
import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests   import StockBarsRequest
from alpaca.data.timeframe  import TimeFrame, TimeFrameUnit

from config import API_KEY, API_SECRET, ET, DATA_FEED, CANDLE_RETRIES, CANDLE_RETRY_SLEEP

log = logging.getLogger("bot")

_client = StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_recent_bars() -> pd.DataFrame:
    """Fetch the last 24h of SPY 5-min bars."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        feed=DATA_FEED,
    )
    bars = _client.get_stock_bars(req)
    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs("SPY", level="symbol")
    df = df.sort_index()
    df.index = df.index.tz_convert(ET)
    return df


def get_opening_candle(retries: int = CANDLE_RETRIES,
                       retry_sleep: int = CANDLE_RETRY_SLEEP,
                       _today=None):
    """
    Return today's 9:30–9:35 ET 5-min candle as a pd.Series.
    Retries up to `retries` times with `retry_sleep` seconds between attempts.
    Returns None if the bar is not available after all attempts.
    _today: override date (for testing); defaults to datetime.now(ET).date()
    """
    today = _today or datetime.now(ET).date()
    for attempt in range(retries):
        df = fetch_recent_bars()
        mask = (
            (df.index.date == today) &
            (df.index.hour == 9) &
            (df.index.minute == 30)
        )
        matches = df[mask]
        if not matches.empty:
            return matches.iloc[0]
        if attempt < retries - 1:
            log.info(
                f"  9:30 candle not yet available "
                f"(attempt {attempt + 1}/{retries}) — retrying in {retry_sleep}s"
            )
            time.sleep(retry_sleep)
    log.error(f"  9:30 candle not available after {retries} attempts")
    return None
