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
#   • Avg-up #1: poll-only — bid ≥ entry × 1.015 → market BUY
#   • Avg-up #2: poll-only — bid ≥ entry × 1.030 → market BUY
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
    submit_market_buy, submit_market_sell,
    submit_stop_market_sell,
    submit_limit_sell,
    cancel_order, get_order, is_terminal, replace_order,
    wait_for_fill,
)
from options import get_quote

AVG_UP_FILL_TIMEOUT = 15  # seconds to wait for avg-up market BUY to fill

# Force-exit this many minutes before market close, so we never carry
# overnight. 15:45 ET keeps a 15-min buffer.
EOD_EXIT_TIME = dt_time(15, 45)

log = logging.getLogger("bot")


class TradeManager:

    # ---------- construction ----------
    def __init__(self, symbol: str, direction: str,
                 entry_fill_price: float, entered_at: datetime,
                 target_id: str = None, stop_id: str = None,
                 entry_qty: int = 1,
                 allowed_avg_ups: int = 2):
        self.symbol             = symbol
        self.direction          = direction          # "CALL" or "PUT" (informational)
        self.entered_at         = entered_at
        self.fills              = [entry_fill_price] * entry_qty
        self.contracts          = entry_qty
        self.avg_cost           = entry_fill_price
        self.initial_entry      = entry_fill_price   # target reference (locked)
        self.target_price       = entry_fill_price * (1 + TARGET_PCT)        # +6% poll trigger
        self.limit_target_price = entry_fill_price * (1 + LIMIT_TARGET_PCT)  # +8% Alpaca safety-net
        self.stop_price         = entry_fill_price * (1 - STOP_PCT)
        self.avg_up_1_price     = entry_fill_price * (1 + AVG_UP_1_PCT)     # +1.5% poll trigger
        self.avg_up_2_price     = entry_fill_price * (1 + AVG_UP_2_PCT)     # +3.0% poll trigger

        # Order IDs of standing orders (stop SELL + optional safety-net target).
        # Avg-ups are poll-only — no standing BUY orders at Alpaca.
        self.order_ids = {
            "stop":   stop_id,
            "target": target_id,
        }

        # Once an avg-up fills, mark done so we don't re-submit
        self.avg_up_1_done = False
        self.avg_up_2_done = False

        self.allowed_avg_ups = allowed_avg_ups

        # Final state for summary
        self.exit_reason  = None
        self.exit_avg_px  = None

    # ---------- setup ----------
    def submit_management_orders(self):
        """
        Submit stop SELL only. Avg-ups are poll-based (no standing BUY orders)
        to avoid Alpaca's wash-trade rejection when a SELL already exists.
        Target exits are also poll-only.
        """
        if self.order_ids["stop"] is None:
            self.order_ids["stop"] = submit_stop_market_sell(
                self.symbol, self.contracts, self.stop_price).id

        log.info(
            f"  📋 Standing orders set (avg_ups_allowed={self.allowed_avg_ups}):"
            f"\n     stop    : market-stop @ {self.stop_price:.2f}    id={self.order_ids['stop']}"
            f"\n     avg_up_1: poll bid ≥ {self.avg_up_1_price:.2f} (+1.5%)"
            f"\n     avg_up_2: poll bid ≥ {self.avg_up_2_price:.2f} (+3.0%)"
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
            raw = states["stop"].filled_avg_price
            if raw is None:
                log.warning("  ⚠ stop order filled but filled_avg_price is None — skipping")
            else:
                log.info(f"  🛑 STOP FILLED @ {float(raw):.2f}")
                self.exit_reason = "STOP"
                self.exit_avg_px = float(raw)
                self._cancel_all_except("stop")
                return True
        if states["target"] and states["target"].status == OrderStatus.FILLED:
            raw = states["target"].filled_avg_price
            if raw is None:
                log.warning("  ⚠ target order filled but filled_avg_price is None — skipping")
            else:
                log.info(
                    f"  🎯 SAFETY-NET TARGET FILLED @ {float(raw):.2f}"
                )
                self.exit_reason = "TARGET"
                self.exit_avg_px = float(raw)
                self._cancel_all_except("target")
                return True

        # 5. Avg-up poll (bid-based, one per tick to avoid double-buy)
        if bid is not None:
            if (not self.avg_up_1_done
                    and self.allowed_avg_ups >= 1
                    and bid >= self.avg_up_1_price):
                log.info(
                    f"  📈 AVG_UP_1 triggered bid=${bid:.2f}"
                    f" ≥ ${self.avg_up_1_price:.2f} (+1.5%) → market-buy"
                )
                self._execute_avg_up(1)
            elif (not self.avg_up_2_done
                    and self.allowed_avg_ups >= 2
                    and bid >= self.avg_up_2_price):
                log.info(
                    f"  📈 AVG_UP_2 triggered bid=${bid:.2f}"
                    f" ≥ ${self.avg_up_2_price:.2f} (+3.0%) → market-buy"
                )
                self._execute_avg_up(2)

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

    def _execute_avg_up(self, which: int):
        """
        Cancel the standing stop (to clear Alpaca's wash-trade check),
        submit a market BUY, wait for fill, then _handle_avg_up_fill()
        re-submits the stop at the updated price/qty.
        """
        # Mark done BEFORE submit so we don't retrigger in the same poll.
        # We reset the flag in the except block if submit fails so the next
        # tick can retry.
        if which == 1:
            self.avg_up_1_done = True
        else:
            self.avg_up_2_done = True
        self._cancel_one("stop")  # must clear SELL before BUY or Alpaca rejects
        try:
            order = submit_market_buy(self.symbol, 1)
            filled = wait_for_fill(order.id, timeout=AVG_UP_FILL_TIMEOUT)
            if filled.filled_avg_price is None:
                raise ValueError(f"avg-up filled but filled_avg_price is None")
            fill_px = float(filled.filled_avg_price)
            log.info(f"  📈 AVG_UP_{which} FILLED @ ${fill_px:.2f}")
            self._handle_avg_up_fill(which, fill_px)  # re-submits stop inside
        except Exception as e:
            log.error(f"  ❌ avg_up_{which} market-buy failed: {e}")
            # Reset flag so next tick can retry
            if which == 1:
                self.avg_up_1_done = False
            else:
                self.avg_up_2_done = False
            # Re-submit stop since we already cancelled it
            try:
                self.order_ids["stop"] = submit_stop_market_sell(
                    self.symbol, self.contracts, self.stop_price).id
                log.info(f"  🔄 stop re-submitted @ {self.stop_price:.2f}  id={self.order_ids['stop']}")
            except Exception as e2:
                log.error(f"  ❌ stop re-submit failed: {e2} — position unprotected!")

    def _handle_avg_up_fill(self, which: int, fill_price: float):
        """Avg-up fired → update avg, atomically resize stop & target via PATCH."""
        self.fills.append(fill_price)
        self.contracts += 1
        self.avg_cost   = sum(self.fills) / len(self.fills)
        self.stop_price = self.avg_cost * (1 - STOP_PCT)
        # Target stays unchanged — it's locked to initial_entry

        # avg_up_N_done was already set True in _execute_avg_up before the BUY fired

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
        stop_oid = self.order_ids.get("stop")  # save before _cancel_one clears it
        for k in list(self.order_ids):
            self._cancel_one(k)
        try:
            order = submit_market_sell(self.symbol, self.contracts)
            log.info(f"  🔴 force-sell {self.contracts}x {self.symbol}  id={order.id}")
            filled = wait_for_fill(order.id, timeout=30)
            self.exit_reason = reason
            self.exit_avg_px = float(filled.filled_avg_price)
        except Exception as e:
            # Standing stop may have already filled and closed the position.
            # Fetch it to recover the actual exit price instead of marking as FAILED.
            if stop_oid:
                try:
                    stop_order = get_order(stop_oid)
                    if stop_order.filled_avg_price:
                        fill = float(stop_order.filled_avg_price)
                        log.info(f"  🛑 standing stop already filled @ ${fill:.2f} — position closed cleanly")
                        self.exit_reason = reason
                        self.exit_avg_px = fill
                        return
                except Exception as e2:
                    log.warning(f"  ⚠ stop order recovery failed: {e2}")
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
