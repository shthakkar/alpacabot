# ================================================================
# STEP 2 TEST — verify fetch_spy_bars()
#
# Run:
#   python3 test_step2.py
#
# Pass criteria:
#   • DataFrame non-empty
#   • Has OHLCV columns
#   • Sorted ascending by timestamp
#   • Index is tz-aware (America/New_York)
#   • Intraday bar spacing is exactly 5 minutes
#   • Latest bar is fresh (within 30 minutes of now, if market is open)
# ================================================================
from datetime import datetime, timezone, timedelta

import pandas as pd

from data import fetch_spy_bars


def main():
    print("=" * 60)
    print("  STEP 2 — fetch SPY 5-min bars")
    print("=" * 60)

    print("\nFetching last 50 bars...")
    df = fetch_spy_bars(limit=50)

    # ---- Sanity checks ----
    assert not df.empty, "FAIL: empty dataframe"
    required = {"open", "high", "low", "close", "volume"}
    missing  = required - set(df.columns)
    assert not missing, f"FAIL: missing columns {missing}. Got: {list(df.columns)}"
    assert df.index.is_monotonic_increasing, "FAIL: bars not sorted ascending"
    assert df.index.tz is not None, "FAIL: index is not timezone-aware"

    # Bar spacing — intraday bars should be exactly 5 minutes apart.
    # Overnight / weekend gaps are filtered out (> 10 min).
    deltas    = df.index.to_series().diff().dropna()
    intraday  = deltas[deltas < pd.Timedelta(minutes=10)]
    bad       = intraday[intraday != pd.Timedelta(minutes=5)]
    assert bad.empty, f"FAIL: non-5-min spacing found: {bad.unique()}"

    # Freshness — latest bar should be within 30 min of "now"
    now_utc      = datetime.now(timezone.utc)
    latest_utc   = df.index[-1].tz_convert(timezone.utc)
    staleness    = now_utc - latest_utc
    assert staleness < timedelta(minutes=30), \
        f"FAIL: latest bar is stale by {staleness} (latest={df.index[-1]}, now={now_utc})"

    # ---- Report ----
    print(f"\n✅ Got {len(df)} bars")
    print(f"   First bar : {df.index[0]}")
    print(f"   Last  bar : {df.index[-1]}")
    print(f"   Staleness : {staleness} behind now")
    print(f"   Columns   : {list(df.columns)}")
    print(f"   TZ        : {df.index.tz}")

    print("\nLast 5 bars:")
    print(df[["open", "high", "low", "close", "volume"]].tail(5).to_string())

    print("\n" + "=" * 60)
    print("  🎉 STEP 2 PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
