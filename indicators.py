# ================================================================
# INDICATORS — Bollinger Bands + RSI
# Step 3: pure-math helpers, no I/O. Easy to unit-test.
# ================================================================
import pandas as pd

from config import BB_PERIOD, BB_STD, RSI_PERIOD


def bollinger_bands(close: pd.Series,
                    period: int = BB_PERIOD,
                    std_dev: float = BB_STD):
    """
    Returns (upper, middle, lower) Bollinger Bands as pd.Series
    aligned to `close`. First `period-1` rows will be NaN.
    """
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)   # population std, matches TradingView
    upper = mid + std_dev * sigma
    lower = mid - std_dev * sigma
    return upper, mid, lower


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """
    Wilder's RSI (the version most charting tools use). First
    `period` rows will be NaN. Range: 0..100.
    """
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    # Wilder's smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs       = avg_gain / avg_loss
    rsi_vals = 100 - (100 / (1 + rs))
    return rsi_vals


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convenience: take an OHLCV bars frame, return a copy with
    bb_upper / bb_mid / bb_lower / rsi columns appended.
    """
    out = df.copy()
    upper, mid, lower = bollinger_bands(out["close"])
    out["bb_upper"] = upper
    out["bb_mid"]   = mid
    out["bb_lower"] = lower
    out["rsi"]      = rsi(out["close"])
    return out
