# ================================================================
# BOT — main live runner. Step 7.
#
# State machine:
#   IDLE  ──[signal fires]──►  IN_TRADE  ──[exit hits]──► IDLE
#
# 30-second poll loop. Refuses to run unless PAPER=True.
#
# Run:
#   python3 bot.py
# ================================================================
import logging
import signal
import sys
import time
from datetime import datetime, time as dt_time, timedelta

import pandas as pd

from config       import (PAPER, CHECK_SECS, ET,
                          MARKET_OPEN, MARKET_CLOSE, LAST_ENTRY,
                          LIMIT_TARGET_PCT, STOP_PCT)
from data         import fetch_spy_bars
from indicators   import add_indicators
from signals      import check_signal
from options      import resolve_option
from orders       import (
    is_market_open, list_positions, list_open_orders,
    submit_market_buy_bracket, wait_for_fill, get_order,
)
from trade_manager import TradeManager, EOD_EXIT_TIME

# ---------------- logging ----------------
log = logging.getLogger("bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt); log.addHandler(_sh)
_fh = logging.FileHandler("bot_log.txt"); _fh.setFormatter(_fmt); log.addHandler(_fh)

# ---------------- knobs ----------------
ACTIVE_START_TIME = dt_time(MARKET_OPEN.hour, MARKET_OPEN.minute)   # 10:00 ET
LAST_ENTRY_TIME   = dt_time(LAST_ENTRY.hour, LAST_ENTRY.minute)     # 15:15 ET — no new entries
ACTIVE_END_TIME   = EOD_EXIT_TIME                                    # 15:45 ET — force-close
MAX_SPREAD        = 0.50      # skip entry if bid-ask spread wider than this
ENTRY_FILL_TIMEOUT = 30       # seconds to wait for entry market order to fill
STALE_DATA_SEC    = 300       # skip cycle if latest bar > 5 min old


class Bot:
    def __init__(self):
        self.trade: TradeManager | None = None
        self._shutting_down = False

    # ---------- startup ----------
    def startup_checks(self):
        log.info("=" * 60)
        log.info(" SPY Mean-Reversion Options Bot")
        log.info("=" * 60)
        log.info(f"  Mode          : {'PAPER' if PAPER else 'LIVE'}")
        log.info(f"  Poll          : every {CHECK_SECS}s")
        log.info(f"  Active hours  : {ACTIVE_START_TIME} – {ACTIVE_END_TIME} ET")
        log.info(f"  Last entry    : {LAST_ENTRY_TIME} ET (no new trades after)")
        log.info(f"  Signal        : Mean Rev BB+RSI 65/35")
        log.info(f"  Target        : initial × 1.06 (locked)")
        log.info(f"  Stop          : avg × 0.92 (market stop)")
        log.info(f"  Avg-ups       : +1.5% / +3.0% (stop-limit BUY)")
        log.info(f"  Time exit     : 30 min")
        log.info("=" * 60)

        if not PAPER:
            log.error("  ❌ Refusing to run: PAPER=False")
            log.error("     Set PAPER=True in config.py for paper trading.")
            sys.exit(1)

        # Refuse to start if any SPY option position is already open
        positions = list_positions()
        spy_options = [p for p in positions
                        if p.symbol.startswith("SPY") and len(p.symbol) > 6]
        if spy_options:
            log.error("  ❌ Refusing to start — existing SPY option position(s):")
            for p in spy_options:
                log.error(f"     {p.symbol}  qty={p.qty}")
            log.error("     Close them manually, then re-start.")
            sys.exit(1)

        # Refuse to start if any open SPY option orders exist
        opens = list_open_orders()
        spy_opens = [o for o in opens
                      if o.symbol.startswith("SPY") and len(o.symbol) > 6]
        if spy_opens:
            log.error("  ❌ Refusing to start — open SPY option orders:")
            for o in spy_opens:
                log.error(f"     {o.symbol}  {o.side}  {o.order_type}  id={o.id}")
            log.error("     Cancel them manually, then re-start.")
            sys.exit(1)

        log.info("  ✅ Startup checks passed")
        log.info("")

    # ---------- main loop ----------
    def run(self):
        self.startup_checks()
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while not self._shutting_down:
            cycle_start = time.time()
            try:
                self.cycle()
            except Exception as e:
                log.exception(f"  ❌ cycle error: {e}")

            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, CHECK_SECS - elapsed)
            time.sleep(sleep_for)

        log.info("Bot exiting cleanly.")

    def _handle_signal(self, signum, frame):
        log.info(f"\n  ⚠ received signal {signum} — shutting down after this cycle")
        self._shutting_down = True

    # ---------- per-cycle ----------
    def cycle(self):
        now = datetime.now(ET)

        # Outside active hours: if a trade is open, the trade_manager's
        # EOD logic handles closing. Otherwise, just sleep.
        if not self._in_active_window(now):
            if self.trade is None:
                log.info(f"  ⏸ outside hours ({now.strftime('%H:%M:%S')})")
                return
            # Trade still open at boundary — let the manager force-close
            done = self.trade.tick(now)
            if done:
                log.info(f"  ✅ trade closed: {self.trade.summary()}")
                self.trade = None
            return

        if self.trade is None:
            self._idle_cycle(now)
        else:
            self._in_trade_cycle(now)

    def _in_active_window(self, now: datetime) -> bool:
        t = now.timetz().replace(tzinfo=None)
        return ACTIVE_START_TIME <= t < ACTIVE_END_TIME

    # ---------- IDLE mode ----------
    def _idle_cycle(self, now: datetime):
        try:
            df = fetch_spy_bars(limit=50)
        except Exception as e:
            log.warning(f"  ⚠ data fetch failed: {e}")
            return

        if df.empty:
            log.warning("  ⚠ no bars returned")
            return

        # Freshness
        latest_ts = df.index[-1].to_pydatetime()
        staleness = (now - latest_ts).total_seconds()
        if staleness > STALE_DATA_SEC:
            log.warning(f"  ⚠ data stale by {staleness:.0f}s — skipping cycle")
            return

        df = add_indicators(df)
        call_sig, put_sig, price = check_signal(df)

        last      = df.iloc[-1]
        now_local = now.timetz().replace(tzinfo=None)
        wind_down = now_local >= LAST_ENTRY_TIME

        log.info(f"  IDLE  SPY={price:.2f}  "
                 f"BBl={last['bb_lower']:.2f}  BBu={last['bb_upper']:.2f}  "
                 f"RSI={last['rsi']:.1f}  "
                 f"signal={'CALL' if call_sig else ('PUT' if put_sig else 'none')}"
                 f"{'  [wind-down: no new entries]' if wind_down else ''}")

        if not (call_sig or put_sig):
            return

        if wind_down:
            log.info(f"  ⏳ signal fired but past LAST_ENTRY {LAST_ENTRY_TIME} ET — skipping entry")
            return

        direction = "CALL" if call_sig else "PUT"
        self._enter_trade(direction, price, now)

    def _enter_trade(self, direction: str, spy_price: float, now: datetime):
        log.info(f"  🚨 SIGNAL: {direction} @ SPY={spy_price:.2f}  — resolving contract")
        try:
            opt = resolve_option(direction, spy_price)
        except Exception as e:
            log.error(f"  ❌ resolve_option failed: {e}")
            return

        log.info(f"     contract: {opt['symbol']}  strike=${opt['strike']:.2f} "
                 f"expiry={opt['expiry']}")
        log.info(f"     quote: bid=${opt['bid']:.2f}  ask=${opt['ask']:.2f}  "
                 f"spread=${opt['spread']:.2f}")

        if opt["spread"] > MAX_SPREAD:
            log.warning(f"  ⚠ spread ${opt['spread']:.2f} > ${MAX_SPREAD:.2f} — skipping entry")
            return

        # BRACKET market BUY for entry. Take-profit/stop child legs are
        # attached atomically — this sidesteps Alpaca's wash-trade and
        # "uncovered option" rejections we'd otherwise hit when submitting
        # SELL stop/target after the BUY avg-up legs were already pending.
        # Bracket children are sized off the option's current ask (close
        # enough to the actual fill that the small slippage is acceptable).
        approx_entry = opt["ask"]
        tp_price     = approx_entry * (1 + LIMIT_TARGET_PCT)
        sl_price     = approx_entry * (1 - STOP_PCT)
        try:
            entry = submit_market_buy_bracket(
                opt["symbol"], qty=1,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
            )
            log.info(f"  📥 BRACKET BUY submitted  id={entry.id} "
                     f"tp=${tp_price:.2f} sl=${sl_price:.2f}")
            filled = wait_for_fill(entry.id, timeout=ENTRY_FILL_TIMEOUT)
        except TimeoutError:
            log.error(f"  ❌ entry order didn't fill in {ENTRY_FILL_TIMEOUT}s")
            return
        except Exception as e:
            log.error(f"  ❌ entry submit/fill failed: {e}")
            return

        if filled.status.value != "filled":
            log.error(f"  ❌ entry status={filled.status} (not filled). Aborting.")
            return

        fill_price = float(filled.filled_avg_price)
        log.info(f"  ✅ ENTRY FILLED @ ${fill_price:.2f}")

        # Pull child IDs from the parent's legs (now that parent is filled)
        target_id = stop_id = None
        try:
            parent = get_order(entry.id)
            legs = list(parent.legs or [])
            # The TakeProfit child is a SELL LIMIT; the StopLoss child is a SELL STOP.
            for leg in legs:
                if leg.order_type.value == "limit":
                    target_id = leg.id
                elif leg.order_type.value in ("stop", "stop_limit"):
                    stop_id = leg.id
            log.info(f"     bracket children: target={target_id} stop={stop_id}")
        except Exception as e:
            log.warning(f"  ⚠ couldn't read bracket children: {e}")

        # Hand off to trade manager — pass child IDs so it skips re-submitting
        self.trade = TradeManager(
            opt["symbol"], direction, fill_price, now,
            target_id=target_id, stop_id=stop_id,
        )
        try:
            self.trade.submit_management_orders()
        except Exception as e:
            log.exception(f"  ❌ failed to submit management orders: {e}")
            log.error("     position is OPEN but avg-ups missing — manual cleanup needed!")
            # Leave self.trade in place so next cycle still tries to manage

    # ---------- IN_TRADE mode ----------
    def _in_trade_cycle(self, now: datetime):
        try:
            done = self.trade.tick(now)
        except Exception as e:
            log.exception(f"  ❌ trade.tick raised: {e}")
            return

        if done:
            log.info(f"  ✅ trade closed: {self.trade.summary()}")
            self.trade = None


if __name__ == "__main__":
    Bot().run()
