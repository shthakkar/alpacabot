# ================================================================
# STEP 6 TEST — paper order round-trip
#
# Run:
#   python3 test_step6.py
#
# Phase A (runs anytime, no orders sent):
#   • PAPER mode check
#   • list current positions
#   • list current open orders
#   • report market open/closed
#
# Phase B (only runs if market is open):
#   • Resolve a CALL contract for current SPY
#   • Pre-clean any leftover position/orders for that symbol
#   • BUY  1 @ marketable limit (limit = current ask)
#   • Wait for fill, verify position appears
#   • SELL 1 @ marketable limit (limit = current bid)
#   • Wait for fill, verify position is closed
#   • Report fill prices + round-trip P&L
#
# Expected round-trip cost: ~spread × 100 ≈ $5-$15 loss.
# That's the price of validating the plumbing.
# ================================================================
from data    import fetch_spy_bars
from options import resolve_option
from orders  import (
    is_market_open,
    list_positions,
    list_open_orders,
    get_position,
    close_any_position,
    cancel_all_orders,
    submit_buy,
    submit_sell,
    wait_for_fill,
)
from config import PAPER


def main():
    print("=" * 60)
    print("  STEP 6 — paper order round-trip")
    print("=" * 60)

    # ── Phase A: safe checks ─────────────────────────────────────
    print(f"\n[A1] PAPER mode = {PAPER}")
    if not PAPER:
        print("   ❌ REFUSING TO RUN against a LIVE account.")
        return

    print("\n[A2] Current open positions:")
    positions = list_positions()
    if not positions:
        print("   (none)")
    else:
        for p in positions:
            print(f"   • {p.symbol}  qty={p.qty}  side={p.side}  "
                  f"market_value=${float(p.market_value):.2f}")

    print("\n[A3] Current open orders:")
    open_orders = list_open_orders()
    if not open_orders:
        print("   (none)")
    else:
        for o in open_orders:
            print(f"   • {o.id[:8]}  {o.symbol}  {o.side}  qty={o.qty}  "
                  f"type={o.order_type}  status={o.status}")

    market = is_market_open()
    print(f"\n[A4] Market open = {market}")
    if not market:
        print("\n" + "=" * 60)
        print("  Market closed — skipping live round-trip (Phase B)")
        print("  Re-run during RTH (Mon-Fri 9:30am-4:00pm ET).")
        print("=" * 60)
        return

    # ── Phase B: live round-trip ─────────────────────────────────
    print("\n" + "-" * 60)
    print("  Phase B — live round-trip")
    print("-" * 60)

    # 1. Get SPY price + resolve a CALL contract
    spy = float(fetch_spy_bars(limit=1)["close"].iloc[-1])
    print(f"\n[B1] SPY @ ${spy:.2f}  →  resolving CALL contract…")
    opt = resolve_option("CALL", spy)
    symbol = opt["symbol"]
    print(f"     symbol  : {symbol}")
    print(f"     strike  : ${opt['strike']:.2f}  expiry: {opt['expiry']}")
    print(f"     bid/ask : ${opt['bid']:.2f} / ${opt['ask']:.2f}  spread: ${opt['spread']:.2f}")

    if opt['spread'] > 0.50:
        print("   ⚠ spread > $0.50 — aborting to avoid bad fills")
        return

    # 2. Pre-clean
    print(f"\n[B2] Pre-clean any leftover state for {symbol}…")
    cancelled = cancel_all_orders(symbol)
    closed    = close_any_position(symbol)
    print(f"     orders cancelled: {cancelled}    positions closed: {closed}")

    # 3. BUY at ask
    print(f"\n[B3] Submitting BUY 1 {symbol} @ limit ${opt['ask']:.2f}…")
    buy_order = submit_buy(symbol, qty=1, limit_price=opt['ask'])
    print(f"     order_id: {buy_order.id}")
    buy_filled = wait_for_fill(buy_order.id, timeout=30)
    print(f"     ✅ filled @ ${float(buy_filled.filled_avg_price):.2f}  status={buy_filled.status}")

    # 4. Verify position
    pos = get_position(symbol)
    assert pos is not None, "FAIL: no position after BUY filled"
    print(f"\n[B4] Position confirmed: qty={pos.qty}  avg_cost=${float(pos.avg_entry_price):.2f}")

    # 5. Get fresh quote and SELL at bid
    fresh = resolve_option("CALL", spy)
    print(f"\n[B5] Submitting SELL 1 {symbol} @ limit ${fresh['bid']:.2f}…")
    sell_order = submit_sell(symbol, qty=1, limit_price=fresh['bid'])
    print(f"     order_id: {sell_order.id}")
    sell_filled = wait_for_fill(sell_order.id, timeout=30)
    print(f"     ✅ filled @ ${float(sell_filled.filled_avg_price):.2f}  status={sell_filled.status}")

    # 6. Verify flat
    pos_after = get_position(symbol)
    assert pos_after is None, f"FAIL: still holding position after SELL: {pos_after}"
    print(f"\n[B6] Position closed — back to flat ✓")

    # 7. P&L
    buy_px   = float(buy_filled.filled_avg_price)
    sell_px  = float(sell_filled.filled_avg_price)
    pnl_each = (sell_px - buy_px) * 100        # options multiplier
    print(f"\n[B7] Round-trip:")
    print(f"     BUY  @ ${buy_px:.2f}")
    print(f"     SELL @ ${sell_px:.2f}")
    print(f"     P&L  = ${pnl_each:+.2f}  (expected ~−$5 to −$15 from spread)")

    print("\n" + "=" * 60)
    print("  🎉 STEP 6 PASSED — order plumbing works end-to-end")
    print("=" * 60)


if __name__ == "__main__":
    main()
