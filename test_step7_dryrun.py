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

    def submit_market_sell(self, symbol, qty):
        return self._mk(symbol=symbol, qty=qty, side="sell", type="market")

    def cancel_order(self, oid):
        o = self.orders.get(oid)
        if o is not None and not is_terminal_status(o.status):
            o.status = OrderStatus.CANCELED

    def get_order(self, oid):
        return self.orders[oid]

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

def _install_fakes():
    orders.submit_stop_limit_buy    = BROKER.submit_stop_limit_buy
    orders.submit_stop_market_sell  = BROKER.submit_stop_market_sell
    orders.submit_limit_sell        = BROKER.submit_limit_sell
    orders.submit_market_sell       = BROKER.submit_market_sell
    orders.cancel_order             = BROKER.cancel_order
    orders.get_order                = BROKER.get_order
    # re-bind names used inside trade_manager.py (imported at top)
    trade_manager.submit_stop_limit_buy    = BROKER.submit_stop_limit_buy
    trade_manager.submit_stop_market_sell  = BROKER.submit_stop_market_sell
    trade_manager.submit_limit_sell        = BROKER.submit_limit_sell
    trade_manager.submit_market_sell       = BROKER.submit_market_sell
    trade_manager.cancel_order             = BROKER.cancel_order
    trade_manager.get_order                = BROKER.get_order


# ----------------------------------------------------------------
# Scenario runner
# ----------------------------------------------------------------
def run_scenario(name, script):
    """
    Each scenario provides a `script` that mutates the fake broker
    between ticks. The script is a list of (action, args) tuples.
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
# Scenarios
# ----------------------------------------------------------------
def main():
    print("=" * 60)
    print("  STEP 7 DRY-RUN — TradeManager state machine")
    print("=" * 60)

    # 1. Case A: entry + avg-up1 + avg-up2 + target
    tm = run_scenario("Case A: full ride to target", [
        ("fill",   "avg_up_1", 3.0452),
        ("tick",),
        ("fill",   "avg_up_2", 3.0902),
        ("tick",),
        ("fill",   "target",   3.18),
        ("tick",),
    ])
    assert tm.exit_reason == "TARGET", f"expected TARGET, got {tm.exit_reason}"
    assert tm.contracts == 3,           f"expected 3 contracts, got {tm.contracts}"

    # 2. Case C: entry + stop fires
    tm = run_scenario("Case C: stop hits before any avg-up", [
        ("fill", "stop", 2.76),
        ("tick",),
    ])
    assert tm.exit_reason == "STOP"
    assert tm.contracts == 1

    # 3. Case D: entry + avg-up1 + stop fires
    tm = run_scenario("Case D: avg-up #1, then stop", [
        ("fill", "avg_up_1", 3.0452),
        ("tick",),
        ("fill", "stop",     2.7808),
        ("tick",),
    ])
    assert tm.exit_reason == "STOP"
    assert tm.contracts == 2

    # 4. Time exit
    tm = run_scenario("Time exit: nothing fires", [
        ("advance", 31),
        ("tick",),
    ])
    assert tm.exit_reason == "TIME"

    print("\n" + "=" * 60)
    print("  🎉 ALL DRY-RUN SCENARIOS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
