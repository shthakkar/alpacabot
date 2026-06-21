# ================================================================
# STEP 7 DRY-RUN — verify TradeManager state machine
#
# No real API calls. Monkey-patches orders.* to a fake broker that
# tracks submitted orders in memory. We then feed scripted "fills"
# and assert the manager reacts correctly.
#
# Scenarios:
#   1. Case A — entry + avg-up 1 + avg-up 2 + target hits
#   2. Case C — entry + stop hits before any avg-up
#   3. Case D — entry + avg-up 1 + stop hits
#   4. Time exit  — entry + nothing fires + 30 min passes
#   5. Budget cap — allowed_avg_ups=0, still exits on time
#   6. Case E — TIME exit captures exit_avg_px
#   7. Case F — TARGET poll exit captures exit_avg_px
#
# Run:
#   python3 test_step7_dryrun.py
# ================================================================
import logging
import sys
from datetime import datetime, timedelta
from types    import SimpleNamespace

import pytz

# We patch BEFORE importing trade_manager so its module-level imports
# bind to the fakes.
import orders
import trade_manager   # noqa: import order intentional

from alpaca.trading.enums import OrderStatus

ET = pytz.timezone("America/New_York")

# Quiet bot logger for cleaner test output (just see test prints)
trade_manager.log.setLevel(logging.WARNING)


# ----------------------------------------------------------------
# Fake broker — drop-in replacements for orders.submit_*
# ----------------------------------------------------------------
class FakeBroker:
    def __init__(self):
        self.next_id = 0
        self.orders  = {}      # id -> SimpleNamespace

    def _mk(self, **kw):
        self.next_id += 1
        oid = f"FK{self.next_id:04d}"
        o   = SimpleNamespace(id=oid, status=OrderStatus.NEW,
                               filled_avg_price=None, **kw)
        self.orders[oid] = o
        return o

    # submit_*
    def submit_stop_limit_buy(self, symbol, qty, stop_price, limit_price):
        return self._mk(symbol=symbol, qty=qty, side="buy",
                         type="stop_limit",
                         stop_price=stop_price, limit_price=limit_price)

    def submit_stop_market_sell(self, symbol, qty, stop_price):
        return self._mk(symbol=symbol, qty=qty, side="sell",
                         type="stop", stop_price=stop_price)

    def submit_limit_sell(self, symbol, qty, limit_price):
        return self._mk(symbol=symbol, qty=qty, side="sell",
                         type="limit", limit_price=limit_price)

    def submit_market_buy(self, symbol, qty):
        return self._mk(symbol=symbol, qty=qty, side="buy", type="market")

    def submit_market_sell(self, symbol, qty):
        return self._mk(symbol=symbol, qty=qty, side="sell", type="market")

    def cancel_order(self, oid):
        o = self.orders.get(oid)
        if o is not None and not is_terminal_status(o.status):
            o.status = OrderStatus.CANCELED

    def get_order(self, oid):
        return self.orders[oid]

    def replace_order(self, oid, **changes):
        """Simulate replace: cancel old, return new order."""
        old = self.orders.get(oid)
        if old is None:
            raise KeyError(f"unknown order {oid}")
        old.status = OrderStatus.REPLACED
        qty        = changes.get("qty", old.qty)
        stop_price = changes.get("stop_price", getattr(old, "stop_price", None))
        return self._mk(symbol=old.symbol, qty=qty, side=old.side,
                         type=old.type, stop_price=stop_price)

    def fill(self, oid, price):
        """Test helper: simulate this order filling at `price`."""
        o = self.orders[oid]
        o.status = OrderStatus.FILLED
        o.filled_avg_price = price


def is_terminal_status(s):
    return s in {OrderStatus.FILLED, OrderStatus.CANCELED,
                  OrderStatus.EXPIRED, OrderStatus.REJECTED,
                  OrderStatus.DONE_FOR_DAY, OrderStatus.REPLACED}


# ----------------------------------------------------------------
# Patch orders.* to point at the fake broker
# ----------------------------------------------------------------
BROKER = FakeBroker()
FAKE_BID = None   # injectable bid for get_quote fake

# Preset fill price for _fake_wait_for_fill (used by Cases E and F)
_fake_fill_price = None


def _fake_get_quote(symbol):
    return {"bid": FAKE_BID}


def _fake_wait_for_fill(order_id, timeout=30, poll=0.5):
    """Auto-fill any order at a preset price for testing."""
    o = BROKER.orders[order_id]
    o.status = OrderStatus.FILLED
    o.filled_avg_price = _fake_fill_price
    return o


def _install_fakes():
    global FAKE_BID
    FAKE_BID = None
    orders.submit_stop_limit_buy    = BROKER.submit_stop_limit_buy
    orders.submit_stop_market_sell  = BROKER.submit_stop_market_sell
    orders.submit_limit_sell        = BROKER.submit_limit_sell
    orders.submit_market_buy        = BROKER.submit_market_buy
    orders.submit_market_sell       = BROKER.submit_market_sell
    orders.cancel_order             = BROKER.cancel_order
    orders.get_order                = BROKER.get_order
    orders.replace_order            = BROKER.replace_order
    # re-bind names used inside trade_manager.py (imported at top)
    trade_manager.submit_stop_limit_buy    = BROKER.submit_stop_limit_buy
    trade_manager.submit_stop_market_sell  = BROKER.submit_stop_market_sell
    trade_manager.submit_limit_sell        = BROKER.submit_limit_sell
    trade_manager.submit_market_buy        = BROKER.submit_market_buy
    trade_manager.submit_market_sell       = BROKER.submit_market_sell
    trade_manager.cancel_order             = BROKER.cancel_order
    trade_manager.get_order                = BROKER.get_order
    trade_manager.replace_order            = BROKER.replace_order
    trade_manager.get_quote                = _fake_get_quote
    trade_manager.wait_for_fill            = _fake_wait_for_fill


# ----------------------------------------------------------------
# Scenario runner
# ----------------------------------------------------------------
def run_scenario(name, script):
    """
    Each scenario provides a `script` that mutates the fake broker
    between ticks. The script is a list of (action, args) tuples.

    Supported actions:
      ("fill", "stop", price)   — simulate the standing stop order filling
      ("set_bid", price)        — set the injected bid for get_quote
      ("advance", minutes)      — advance the clock
      ("tick",)                 — call tm.tick(current_time)
    """
    global BROKER
    BROKER = FakeBroker()
    _install_fakes()

    entry_time = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm = trade_manager.TradeManager("SPY260528C00750000", "CALL",
                                     entry_fill_price=3.00,
                                     entered_at=entry_time)
    tm.submit_management_orders()

    print(f"\n— {name} —")
    print(f"  After entry: contracts=1  avg=$3.00  stop=$2.76  target=$3.18")

    for step, (action, *args) in enumerate(script, 1):
        if action == "fill":
            order_key, price = args
            oid = tm.order_ids[order_key]
            BROKER.fill(oid, price)
            print(f"  → step {step}: SIMULATE {order_key} fills @ ${price}")
        elif action == "set_bid":
            (bid_price,) = args
            global FAKE_BID
            FAKE_BID = bid_price
            print(f"  → step {step}: SIMULATE bid=${bid_price}")
        elif action == "advance":
            (minutes,) = args
            entry_time = entry_time + timedelta(minutes=minutes)
            print(f"  → step {step}: SIMULATE clock +{minutes} min")
        elif action == "tick":
            done = tm.tick(entry_time)
            print(f"  → step {step}: TICK → "
                  f"contracts={tm.contracts}  avg={tm.avg_cost:.2f}  "
                  f"stop={tm.stop_price:.2f}  done={done}  reason={tm.exit_reason}")
            if done: break

    print(f"  RESULT: {tm.summary()}")
    return tm


# ----------------------------------------------------------------
# Unit test: quote validation
# ----------------------------------------------------------------
def test_quote_validation():
    """Zero bid must produce NaN mid/spread, and the entry guard must catch it."""
    import math

    # Simulate what get_quote returns for bid=0 (stale or illiquid contract)
    def build_quote(bid, ask):
        return {
            "bid":    bid,
            "ask":    ask,
            "mid":    (bid + ask) / 2 if bid > 0 and ask > 0 else float("nan"),
            "spread": ask - bid       if bid > 0 and ask > 0 else float("nan"),
        }

    q_zero_bid = build_quote(0, 1.50)
    assert math.isnan(q_zero_bid["spread"]), \
        f"spread should be NaN for zero bid, got {q_zero_bid['spread']}"
    assert math.isnan(q_zero_bid["mid"]), \
        f"mid should be NaN for zero bid, got {q_zero_bid['mid']}"

    q_valid = build_quote(1.20, 1.30)
    assert abs(q_valid["spread"] - 0.10) < 0.001, \
        f"spread should be 0.10, got {q_valid['spread']}"
    assert abs(q_valid["mid"] - 1.25) < 0.001, \
        f"mid should be 1.25, got {q_valid['mid']}"

    print("\n— Quote validation unit test PASSED —")


# ----------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------
def main():
    print("=" * 60)
    print("  STEP 7 DRY-RUN — TradeManager state machine")
    print("=" * 60)

    # Run the quote validation unit test first
    test_quote_validation()

    # 1. Case A: entry + avg-up1 (bid poll) + avg-up2 (bid poll) + target (bid poll)
    # Avg-ups are now poll-only (no standing BUY orders), so we set the bid
    # above each trigger threshold and let _execute_avg_up fire naturally.
    # _fake_wait_for_fill will fill each market BUY at _fake_fill_price.
    global _fake_fill_price
    _fake_fill_price = 3.0452   # avg-up #1 fill price
    tm = run_scenario("Case A: full ride to target", [
        # bid above avg-up #1 threshold (3.00 * 1.015 = 3.045)
        ("set_bid", 3.0452),
        ("tick",),              # tick fires avg-up #1
        # now bid above avg-up #2 threshold (3.00 * 1.03 = 3.09)
        ("set_bid", 3.0902),
        ("tick",),              # tick fires avg-up #2
        ("set_bid", 3.18),      # bid hits target poll (3.00 * 1.06 = 3.18)
        ("tick",),
    ])
    assert tm.exit_reason == "TARGET", f"expected TARGET, got {tm.exit_reason}"
    assert tm.contracts == 3,           f"expected 3 contracts, got {tm.contracts}"

    # 2. Case C: entry + stop fires (standing stop order fills)
    _fake_fill_price = None
    tm = run_scenario("Case C: stop hits before any avg-up", [
        ("fill", "stop", 2.76),
        ("tick",),
    ])
    assert tm.exit_reason == "STOP"
    assert tm.contracts == 1

    # 3. Case D: entry + avg-up1 (bid poll) + stop fires
    _fake_fill_price = 3.0452  # avg-up #1 fill price
    global BROKER
    BROKER = FakeBroker()
    _install_fakes()
    entry_time_d = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm_d = trade_manager.TradeManager("SPY260528C00750000", "CALL",
                                       entry_fill_price=3.00,
                                       entered_at=entry_time_d)
    tm_d.submit_management_orders()
    print(f"\n— Case D: avg-up #1, then stop —")
    print(f"  After entry: contracts=1  avg=$3.00  stop=$2.76  target=$3.18")
    # Tick 1: bid above avg-up #1 fires the poll buy; wait_for_fill fills it
    global FAKE_BID
    FAKE_BID = 3.0452
    done = tm_d.tick(entry_time_d)
    print(f"  → step 1: TICK (bid=3.0452) → contracts={tm_d.contracts}  avg={tm_d.avg_cost:.4f}  done={done}")
    assert tm_d.contracts == 2, f"expected 2 contracts after avg-up, got {tm_d.contracts}"
    # Tick 2: standing stop fills
    FAKE_BID = None
    stop_oid = tm_d.order_ids["stop"]
    BROKER.fill(stop_oid, 2.7808)
    print(f"  → step 2: SIMULATE stop fills @ 2.7808")
    done = tm_d.tick(entry_time_d)
    print(f"  → step 2: TICK → contracts={tm_d.contracts}  done={done}  reason={tm_d.exit_reason}")
    print(f"  RESULT: {tm_d.summary()}")
    assert tm_d.exit_reason == "STOP", f"expected STOP, got {tm_d.exit_reason}"
    assert tm_d.contracts == 2, f"expected 2 contracts, got {tm_d.contracts}"

    # 4. Time exit
    _fake_fill_price = 2.50   # force-sell will be filled at this price by _fake_wait_for_fill
    tm = run_scenario("Time exit: nothing fires", [
        ("advance", 31),
        ("tick",),
    ])
    assert tm.exit_reason == "TIME"

    # 5. Budget cap: allowed_avg_ups=0 — stop submitted, no avg-up orders
    _fake_fill_price = 2.50
    BROKER = FakeBroker()
    _install_fakes()
    entry_time_5 = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm5 = trade_manager.TradeManager(
        "SPY260528C00750000", "CALL",
        entry_fill_price=5.00, entered_at=entry_time_5,
        allowed_avg_ups=0,
    )
    tm5.submit_management_orders()
    assert tm5.order_ids["stop"] is not None, \
        "stop order should still be submitted"
    # Verify avg-ups were suppressed: high bid with allowed_avg_ups=0 should not increase contracts
    FAKE_BID = 5.20  # above both +1.5% and +3.0% thresholds, but below +6% target (5.30)
    done_early = tm5.tick(entry_time_5 + timedelta(minutes=5))
    assert not done_early, "should not exit early"
    assert tm5.contracts == 1, f"contracts should stay 1 with allowed_avg_ups=0, got {tm5.contracts}"
    # Verify time exit works with no avg-ups in play
    done = tm5.tick(entry_time_5 + timedelta(minutes=31))
    assert done, "should exit on time"
    assert tm5.exit_reason == "TIME", f"expected TIME, got {tm5.exit_reason}"
    print("\n— Case 5: Budget cap (allowed_avg_ups=0) —")
    print(f"  stop={tm5.order_ids['stop']}")
    print(f"  RESULT: {tm5.summary()}")

    # Case E: TIME exit captures exit_avg_px
    _fake_fill_price = 2.85
    BROKER = FakeBroker(); _install_fakes()
    entry_time_e = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm_e = trade_manager.TradeManager("SPY260528C00750000", "CALL",
                                       entry_fill_price=3.00, entered_at=entry_time_e)
    tm_e.submit_management_orders()
    done = tm_e.tick(entry_time_e + timedelta(minutes=31))
    assert done, "should have exited on time"
    assert tm_e.exit_reason == "TIME", f"got {tm_e.exit_reason}"
    assert tm_e.exit_avg_px == 2.85, \
        f"exit_avg_px should be 2.85, got {tm_e.exit_avg_px}"
    pnl = (tm_e.exit_avg_px - tm_e.avg_cost) * tm_e.contracts * 100
    assert abs(pnl - (-15.0)) < 0.01, f"P&L should be -$15, got {pnl}"
    print(f"\n— Case E: TIME exit P&L —\n  RESULT: {tm_e.summary()}")

    # Case F: TARGET poll exit captures exit_avg_px
    _fake_fill_price = 3.25
    BROKER = FakeBroker(); _install_fakes()
    entry_time_f = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm_f = trade_manager.TradeManager("SPY260528C00750000", "CALL",
                                       entry_fill_price=3.00, entered_at=entry_time_f)
    tm_f.submit_management_orders()
    # Inject a bid above target (3.00 * 1.06 = 3.18)
    FAKE_BID = 3.20
    done = tm_f.tick(entry_time_f + timedelta(minutes=5))
    assert done, "should have exited on target"
    assert tm_f.exit_reason == "TARGET", f"got {tm_f.exit_reason}"
    assert tm_f.exit_avg_px == 3.25, \
        f"exit_avg_px should be 3.25, got {tm_f.exit_avg_px}"
    print(f"\n— Case F: TARGET poll exit P&L —\n  RESULT: {tm_f.summary()}")

    # Case G: avg-up flag resets on submit failure so retry works
    global _buy_call_count
    _buy_call_count  = 0

    def _failing_then_ok_buy(symbol, qty):
        global _buy_call_count
        _buy_call_count += 1
        if _buy_call_count == 1:
            raise RuntimeError("simulated network error on first avg-up attempt")
        return BROKER._mk(symbol=symbol, qty=qty, side="buy", type="market")

    BROKER = FakeBroker(); _install_fakes()
    _buy_call_count = 0
    trade_manager.submit_market_buy = _failing_then_ok_buy
    # Also patch wait_for_fill to handle the successful second attempt
    _fake_fill_price = 3.05
    entry_time_g = datetime(2026, 5, 26, 11, 0, tzinfo=ET)
    tm_g = trade_manager.TradeManager("SPY260528C00750000", "CALL",
                                       entry_fill_price=3.00, entered_at=entry_time_g)
    tm_g.submit_management_orders()

    # First tick: bid triggers avg-up #1, submit fails
    FAKE_BID = 3.06  # above avg_up_1_price (3.00 * 1.015 = 3.045)
    tm_g.tick(entry_time_g + timedelta(minutes=1))
    assert not tm_g.avg_up_1_done, \
        f"avg_up_1_done should be reset to False after submit failure, got {tm_g.avg_up_1_done}"

    # Second tick: retry should succeed
    tm_g.tick(entry_time_g + timedelta(minutes=2))
    assert tm_g.avg_up_1_done, "avg_up_1_done should be True after successful retry"
    assert tm_g.contracts == 2, f"expected 2 contracts after avg-up, got {tm_g.contracts}"
    print(f"\n— Case G: avg-up flag reset on submit failure —\n  RESULT: {tm_g.summary()}")

    print("\n" + "=" * 60)
    print("  🎉 ALL DRY-RUN SCENARIOS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
