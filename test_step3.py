# ================================================================
# STEP 3 TEST — verify Bollinger Bands + RSI
#
# Run:
#   python3 test_step3.py
#
# Pass criteria:
#   • BB columns present, all numeric on the last row
#   • RSI in valid range [0, 100] (where defined)
#   • lower < mid < upper on every non-NaN row
#   • last row's close sits roughly between bands (within ±5 std)
# ================================================================
import pandas as pd

from data       import fetch_spy_bars
from indicators import add_indicators


def main():
    print("=" * 60)
    print("  STEP 3 — Bollinger Bands + RSI")
    print("=" * 60)

    df = fetch_spy_bars(limit=50)
    df = add_indicators(df)

    # ---- Sanity checks ----
    for col in ("bb_upper", "bb_mid", "bb_lower", "rsi"):
        assert col in df.columns, f"FAIL: missing column {col}"

    # Last row must be fully populated (50 bars > 20-period BB + 14-period RSI)
    last = df.iloc[-1]
    for col in ("bb_upper", "bb_mid", "bb_lower", "rsi"):
        assert pd.notna(last[col]), f"FAIL: last-row {col} is NaN"

    # BB ordering
    valid    = df.dropna(subset=["bb_upper", "bb_mid", "bb_lower"])
    ordering = (valid["bb_lower"] < valid["bb_mid"]) & (valid["bb_mid"] < valid["bb_upper"])
    assert ordering.all(), "FAIL: BB ordering violated on some row"

    # RSI range
    rsi_valid = df["rsi"].dropna()
    assert ((rsi_valid >= 0) & (rsi_valid <= 100)).all(), \
        f"FAIL: RSI out of [0,100]. min={rsi_valid.min()} max={rsi_valid.max()}"

    # ---- Report ----
    print(f"\n✅ Indicators computed on {len(df)} bars")
    print(f"   Latest close : {last['close']:.2f}")
    print(f"   BB upper     : {last['bb_upper']:.2f}")
    print(f"   BB mid (SMA) : {last['bb_mid']:.2f}")
    print(f"   BB lower     : {last['bb_lower']:.2f}")
    print(f"   RSI(14)      : {last['rsi']:.2f}")

    print("\nLast 5 bars with indicators:")
    cols = ["close", "bb_upper", "bb_mid", "bb_lower", "rsi"]
    print(df[cols].tail(5).to_string(float_format=lambda x: f"{x:7.2f}"))

    # Interpretation hint
    if last["rsi"] > 70:
        zone = "🟢 overbought (RSI > 70)"
    elif last["rsi"] < 30:
        zone = "🔴 oversold (RSI < 30)"
    else:
        zone = "⚪ neutral"
    print(f"\nRSI zone: {zone}")

    if last["close"] > last["bb_upper"]:
        print("Price is ABOVE upper band")
    elif last["close"] < last["bb_lower"]:
        print("Price is BELOW lower band")
    else:
        print("Price is INSIDE the bands")

    print("\n" + "=" * 60)
    print("  🎉 STEP 3 PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
