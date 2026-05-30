# ================================================================
# STEP 4 TEST — signal detection dry-run
#
# Run:
#   python3 test_step4.py
#
# What it does:
#   • Fetches the last ~250 5-min bars (~4 trading days)
#   • Scans them bar-by-bar and lists every CALL/PUT that would
#     have fired historically
#   • Reports the current bar's signal state (if any)
#
# No orders. No option symbols. Pure detection sanity check.
# ================================================================
import pandas as pd

from data       import fetch_spy_bars
from indicators import add_indicators
from signals    import check_signal, scan_history


def main():
    print("=" * 60)
    print("  STEP 4 — signal detection dry-run")
    print("=" * 60)

    df = fetch_spy_bars(limit=250)
    df = add_indicators(df)
    print(f"\nFetched {len(df)} bars  ({df.index[0]} → {df.index[-1]})")

    # ── Historical scan ──────────────────────────────────────────
    hits = scan_history(df)
    print(f"\nHistorical signals in this window: {len(hits)}")

    if not hits.empty:
        # Show counts by direction
        by_dir = hits["direction"].value_counts().to_dict()
        print(f"   By direction: {by_dir}")

        print("\nAll historical signals:")
        print(hits.to_string(
            index=False,
            float_format=lambda x: f"{x:7.2f}",
        ))
    else:
        print("   (none — strategy is conservative on this window)")

    # ── Current bar status ───────────────────────────────────────
    call_sig, put_sig, price = check_signal(df)
    last = df.iloc[-1]

    print("\n" + "-" * 60)
    print("  CURRENT BAR STATUS")
    print("-" * 60)
    print(f"   Time      : {df.index[-1]}")
    print(f"   Close     : {price:.2f}")
    print(f"   BB upper  : {last['bb_upper']:.2f}")
    print(f"   BB lower  : {last['bb_lower']:.2f}")
    print(f"   RSI(14)   : {last['rsi']:.2f}")

    if call_sig:
        print(f"\n   🟢 CALL SIGNAL — would enter long calls @ {price:.2f}")
    elif put_sig:
        print(f"\n   🔴 PUT SIGNAL — would enter long puts @ {price:.2f}")
    else:
        # Explain WHY no signal — useful for debugging
        curr = df.iloc[-1]
        print("\n   ⬜ No signal. Why:")
        if curr["close"] >= curr["bb_lower"]:
            print(f"      • close {curr['close']:.2f} is NOT below lower BB {curr['bb_lower']:.2f}")
        elif curr["rsi"] >= 35:
            print(f"      • close below lower BB, but RSI {curr['rsi']:.1f} ≥ 35")
        if curr["close"] <= curr["bb_upper"]:
            print(f"      • close {curr['close']:.2f} is NOT above upper BB {curr['bb_upper']:.2f}")
        elif curr["rsi"] <= 65:
            print(f"      • close above upper BB, but RSI {curr['rsi']:.1f} ≤ 65")

    print("\n" + "=" * 60)
    print("  🎉 STEP 4 PASSED  (dry-run only, no orders sent)")
    print("=" * 60)


if __name__ == "__main__":
    main()
