# ================================================================
# STEP 5 TEST — option symbol resolution
#
# Run:
#   python3 test_step5.py
#
# Checks:
#   1. Paper account has options trading enabled (level ≥ 2)
#   2. Alpaca chain for SPY returns contracts in next 7 days
#   3. We can pick the 2nd-soonest expiry
#   4. We can resolve a CALL → OTM strike + tradeable symbol + quote
#   5. We can resolve a PUT  → OTM strike + tradeable symbol + quote
#
# No orders are placed.
# ================================================================
from datetime import datetime

import pytz

from config  import ET
from data    import fetch_spy_bars
from options import (
    options_enabled,
    fetch_chain,
    pick_expiry,
    resolve_option,
)

PT = pytz.timezone("America/Los_Angeles")


def main():
    print("=" * 60)
    print("  STEP 5 — option symbol resolution")
    print("=" * 60)

    # ── 1. account check ───────────────────────────────────────
    print("\n[1] Checking options trading is enabled…")
    enabled, level = options_enabled()
    print(f"   options_trading_level = {level}")
    if not enabled:
        print("   ❌ Account is not approved to BUY options (need level ≥ 2).")
        print("      Enable in Alpaca paper dashboard → Configuration → Options.")
        return
    print("   ✅ Account can trade long options")

    # ── 2. chain fetch ─────────────────────────────────────────
    print("\n[2] Fetching SPY option chain (today + 7 days)…")
    today    = datetime.now(ET).date()
    contracts = fetch_chain(today)
    expiries  = sorted({c.expiration_date for c in contracts})
    print(f"   ✅ {len(contracts)} contracts across {len(expiries)} expirations")
    print(f"   Expirations: {expiries}")

    # ── 3. expiry pick ─────────────────────────────────────────
    target_expiry = pick_expiry(contracts, today)
    print(f"\n[3] Target expiry (2nd-soonest after today): {target_expiry}")

    # ── 4 + 5. resolve CALL and PUT against current SPY ────────
    bars  = fetch_spy_bars(limit=1)
    spy   = float(bars["close"].iloc[-1])
    print(f"\nCurrent SPY price: ${spy:.2f}")

    for direction in ("CALL", "PUT"):
        print(f"\n[{direction}] resolving…")
        opt = resolve_option(direction, spy)
        print(f"   symbol  : {opt['symbol']}")
        print(f"   expiry  : {opt['expiry']}")
        print(f"   strike  : ${opt['strike']:.2f}")
        print(f"   bid/ask : ${opt['bid']:.2f} / ${opt['ask']:.2f}")
        print(f"   mid     : ${opt['mid']:.2f}   spread: ${opt['spread']:.2f}")
        print(f"   quote @ : {opt['timestamp']}")

        # Sanity: OTM check
        if direction == "CALL":
            assert opt["strike"] >= spy, f"FAIL: CALL strike {opt['strike']} not OTM vs spy {spy}"
        else:
            assert opt["strike"] <= spy, f"FAIL: PUT  strike {opt['strike']} not OTM vs spy {spy}"

    print("\n" + "=" * 60)
    print("  🎉 STEP 5 PASSED  (no orders sent)")
    print("=" * 60)


if __name__ == "__main__":
    main()
