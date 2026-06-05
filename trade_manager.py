# ================================================================
# TRADE MANAGER — encapsulates IN_TRADE state
# Step 7. Used by bot.py while a position is open.
#
# Hybrid exit design:
#   • Bot polls option bid every 30s; if bid ≥ initial × 1.06 → market-sell
#     (captures the actual bid, which may be >+6% on momentum spikes).
#   • Safety-net LIMIT at initial × 1.08 lives at Alpaca — fires if the
#     bot is offline or the option leaps past +8% between polls.
#   • Stop = avg_cost × 0.92 (market stop at Alpaca, re-set after avg-ups)
#   • Avg-up #1 stop-limit BUY @ avg × 1.015 (limit + $0.02)
#   • Avg-up #2 stop-limit BUY @ avg × 1.030 (limit + $0.02)
#   • Time exit at TIME_EXIT_MIN minutes since entry
# ================================================================
import logging
from datetime import datetime, time as dt_time, timedelta

from alpaca.trading.enums import OrderStatus

from config import (
    ET, AVG_UP_1_PCT, AVG_UP_2_PCT, STOP_PCT, TARGET_PCT, LIMIT_TARGET_PCT,
    TIME_EXIT_MIN, MARKET_CLOSE,
)
from orders import (
    submit_market_sell,
    submit_stop_limit_buy,
    submit_stop_market_sell,
    submit_limit_sell,
    cancel_order, get_order, is_terminal, replace_order,
)
from options import get_quote

AVG_UP_LIMIT_OFFSET = 0.02

# Force-exit this many minutes before market close, so we never carry
# overnight. 15:45 ET keeps a 15-min buffer.
EOD_EXIT_TIME = dt_time(15, 45)

log = logging.getLogger("bot")


class TradeManager:

    # ---------- construction ----------
    def __init__(self, symbol: str, direction: str,
                 entry_fill_price: float, entered_at: datetime,
                 target_id: str = None, stop_id: str = None):
        """
        target_id / stop_id: when entry was submitted as a BRACKET, these are
        the IDs of the bracket's child orders (already live at Alpaca).
        When None (legacy / dry-run), submit_management_orders will create
        them as independent orders.
        """
        self.symbol             = symbol
        self.direction          = direction          # "CALL" or "PUT" (informational)
        self.entered_at         = entered_at
        self.fills              = [entry_fill_price]
        self.contracts          = 1
        self.avg_cost           = entry_fill_price
        self.initial_entry      = entry_fill_price   # target reference (locked)
        self.target_price       = entry_fill_price * (1 + TARGET_PCT)        # +6% poll trigger
        self.limit_target_price = entry_fill_price * (1 + LIMIT_TARGET_PCT)  # +8% Alpaca safety-net
        self.stop_price         = entry_fill_price * (1 - STOP_PCT)

        # Order IDs of the 4 standing orders. Stop/target may be pre-set
        # from the bracket parent's children; avg-ups are submitted later.
        self.order_ids = {
            "avg_up_1": None,
            "avg_up_2": None,
            "stop":     stop_id,
            "target":   target_id,
        }

        # Once an avg-up fills, mark done so we don't re-submit
        self.avg_up_1_done = False
        self.avg_up_2_done = False

        # Final state for summary
        self.exit_reason  = None
        self.exit_avg_px  = None

    # ---------- setup ----------
    def submit_management_orders(self):
        """
        Submit stop SELL first (before any BUY orders exist so Alpaca's
        wash-trade check doesn't fire), then the avg-up BUYs.
        No standing target SELL — Alpaca rejects a second SELL on the same
        contract as "uncovered option". The +6% target is caught by the
        bid poll in tick(); +8% fallback is also poll-based.
        """
        au1_trg = self.avg_cost * (1 + AVG_UP_1_PCT)
        au1_lim = au1_trg + AVG_UP_LIMIT_OFFSET
        au2_trg = self.avg_cost * (1 + AVG_UP_2_PCT)
        au2_lim = au2_trg + AVG_UP_LIMIT_OFFSET

        # Stop SELL must go in before the avg-up BUYs
        if self.order_ids["stop"] is None:
            self.order_ids["stop"] = submit_stop_market_sell(
                self.symbol, 1, self.stop_price).id

        self.order_ids["avg_up_1"] = submit_stop_limit_buy(
            self.symbol, 1, au1_trg, au1_lim).id
        self.order_ids["avg_up_2"] = submit_stop_limit_buy(
            self.symbol, 1, au2_trg, au2_lim).id

        log.info(
            f"  📋 Standing orders set:"
            f"\n     stop    : market-stop @ {self.stop_price:.2f}    id={self.order_ids['stop']}"
            f"\n     avg_up_1: stop {au1_trg:.2f} lim {au1_lim:.2f}  id={self.order_ids['avg_up_1']}"
            f"\n     avg_up_2: stop {au2_trg:.2f} lim {au2_lim:.2f}  id={self.order_ids['avg_up_2']}"
            f"\n     poll trg: bid ≥ {self.target_price:.2f} (+6%) / {self.limit_target_price:.2f} (+8%)"
        )

    # ---------- main loop entry (called every 30s by bot.py) ----------
    def tick(self, now: datetime) -> bool:
        """
        Returns True when the trade is finished (back to IDLE).
        """
        # 1. Time exit
        elapsed_min = (now - self.entered_at).total_seconds() / 60
        if elapsed_min >= TIME_EXIT_MIN:
            log.info(f"  ⏰ TIME_EXIT after {elapsed_min:.1f} min")
            self._force_close("TIME")
            return True

        # 2. EOD force-close (15 min before market close)
        if now.timetz().replace(tzinfo=None) >= EOD_EXIT_TIME:
            log.info(f"  🕞 EOD force-close (now {now.strftime('%H:%M:%S')})")
            self._force_close("EOD")
            return True

        # 3. Poll bid for both stop and target exits
        try:
            bid = get_quote(self.symbol).get("bid")
        except Exception as e:
            log.warning(f"  ⚠ bid poll quote failed: {e}")
            bid = None
        if bid is not None and bid <= self.stop_price:
            log.info(
                f"  🛑 STOP HIT (poll) bid=${bid:.2f} ≤ ${self.stop_price:.2f}"
                f" → market-sell"
            )
            self._force_close("STOP")
            return True
        if bid is not None and bid >= self.target_price:
            log.info(
                f"  🎯 TARGET HIT (poll) bid=${bid:.2f} ≥ ${self.target_price:.2f}"
                f" → market-sell"
            )
            self._force_close("TARGET")
            return True

        # 4. Poll all standing orders (stop + safety-net limit at +8%)
        states = self._poll_states()

        if states["stop"] and states["stop"].status == OrderStatus.FILLED:
            log.info(f"  🛑 STOP FILLED @ {float(states['stop'].filled_avg_price):.2f}")
            self.exit_reason = "STOP"
            self.exit_avg_px = float(states["stop"].filled_avg_price)
            self._cancel_all_except("stop")
            return True
        if states["target"] and states["target"].status == OrderStatus.FILLED:
            log.info(
                f"  🎯 SAFETY-NET TARGET FILLED @ "
                f"{float(states['target'].filled_avg_price):.2f}"
            )
            self.exit_reason = "TARGET"
            self.exit_avg_px = float(states["target"].filled_avg_price)
            self._cancel_all_except("target")
            return True

        # 5. Avg-up fills
        if (not self.avg_up_1_done
                and states["avg_up_1"]
                and states["avg_up_1"].status == OrderStatus.FILLED):
            fill_px = float(states["avg_up_1"].filled_avg_price)
            log.info(f"  📈 AVG_UP_1 FILLED @ {fill_px:.2f}")
            self._handle_avg_up_fill(1, fill_px)

        if (not self.avg_up_2_done
                and states["avg_up_2"]
                and states["avg_up_2"].status == OrderStatus.FILLED):
            fill_px = float(states["avg_up_2"].filled_avg_price)
            log.info(f"  📈 AVG_UP_2 FILLED @ {fill_px:.2f}")
            self._handle_avg_up_fill(2, fill_px)

        return False

    # ---------- internals ----------
    def _poll_states(self):
        out = {}
        for k, oid in self.order_ids.items():
            if oid is None:
                out[k] = None
                continue
            try:
                out[k] = get_order(oid)
            except Exception as e:
                log.warning(f"  ⚠ couldn't fetch order {k}={oid}: {e}")
                out[k] = None
        return out

    def _handle_avg_up_fill(self, which: int, fill_price: float):
        """Avg-up fired → update avg, atomically resize stop & target via PATCH."""
        self.fills.append(fill_price)
        self.contracts += 1
        self.avg_cost   = sum(self.fills) / len(self.fills)
        self.stop_price = self.avg_cost * (1 - STOP_PCT)
        # Target stays unchanged — it's locked to initial_entry

        if which == 1: self.avg_up_1_done = True
        else:          self.avg_up_2_done = True

        # Atomic PATCH on the stop order — resize qty and move stop price.
        # No standing target order (poll-only), so only stop is resized.
        self.order_ids["stop"] = self._resize("stop",
            qty=self.contracts, stop_price=self.stop_price)

        log.info(
            f"  → contracts={self.contracts}  avg_cost={self.avg_cost:.2f}"
            f"  new_stop={self.stop_price:.2f}  poll_tgt={self.target_price:.2f} (unchanged)"
            f"\n     new stop  id={self.order_ids['stop']}"
        )

    def _resize(self, key: str, **changes):
        """
        Replace one standing order with new qty / stop / limit.
        Falls back to a fresh submit if the original id is missing
        (e.g. initial submit failed) so the position is never naked.
        """
        oid = self.order_ids.get(key)
        if oid is not None:
            try:
                return replace_order(oid, **changes).id
            except Exception as e:
                log.warning(f"  ⚠ replace {key}={oid} failed: {e}; submitting fresh")
        # Fallback: submit a brand-new order.
        if key == "stop":
            return submit_stop_market_sell(
                self.symbol, self.contracts, self.stop_price).id
        if key == "target":
            return submit_limit_sell(
                self.symbol, self.contracts, self.limit_target_price).id
        raise ValueError(f"unknown order key {key!r}")

    def _cancel_one(self, key: str):
        oid = self.order_ids.get(key)
        if not oid:
            return
        try:
            cancel_order(oid)
        except Exception as e:
            log.warning(f"  ⚠ couldn't cancel {key}={oid}: {e}")
        self.order_ids[key] = None

    def _cancel_all_except(self, keep: str):
        for k in list(self.order_ids):
            if k != keep and self.order_ids[k] is not None:
                self._cancel_one(k)

    def _force_close(self, reason: str):
        """Cancel everything pending, then market-sell anything we hold."""
        for k in list(self.order_ids):
            self._cancel_one(k)
        try:
            order  = submit_market_sell(self.symbol, self.contracts)
            log.info(f"  🔴 force-sell {self.contracts}x {self.symbol}  id={order.id}")
            self.exit_reason = reason
        except Exception as e:
            log.error(f"  ❌ force-sell failed: {e}")
            self.exit_reason = f"{reason}_FAILED"

    # ---------- end-of-trade summary ----------
    def summary(self) -> str:
        if self.exit_avg_px is None:
            return (f"{self.symbol} {self.direction} {self.contracts}c "
                    f"avg={self.avg_cost:.2f} reason={self.exit_reason}")
        pnl = (self.exit_avg_px - self.avg_cost) * self.contracts * 100
        return (f"{self.symbol} {self.direction} {self.contracts}c "
                f"avg={self.avg_cost:.2f} exit={self.exit_avg_px:.2f} "
                f"reason={self.exit_reason}  P&L=${pnl:+.2f}")
