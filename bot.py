# ================================================================
# BOT — main live runner. Step 7.
#
# State machine:
#   IDLE  ──[signal fires]──►  IN_TRADE  ──[exit hits]──► IDLE
#
# 30-second poll loop.
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
                          LIMIT_TARGET_PCT, STOP_PCT, DAILY_LOSS_LIMIT,
                          LOSS_COOLDOWN_MIN)
from data         import fetch_spy_bars
from indicators   import add_indicators
from signals      import check_signal
from options      import resolve_option
from orders       import (
    is_market_open, list_positions, list_open_orders,
    submit_market_buy, wait_for_fill, get_order, get_buying_power,
)
from trade_manager import TradeManager, EOD_EXIT_TIME

# ---------------- logging ----------------
log = logging.getLogger("bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")

# Full log — stdout (redirected to logs/bot_YYYYMMDD.log by run_bot.sh)
_sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt); log.addHandler(_sh)

# Events log — signals, entries, exits, errors only (no heartbeat lines)
class _EventsFilter(logging.Filter):
    _SKIP = ("IDLE ", "⏸ outside hours")
    def filter(self, record):
        return not any(s in record.getMessage() for s in self._SKIP)

_logdate = datetime.now().strftime("%Y%m%d")
_ef = logging.FileHandler(f"logs/events_{_logdate}.log")
_ef.setFormatter(_fmt)
_ef.addFilter(_EventsFilter())
log.addHandler(_ef)

# ---------------- knobs ----------------
ACTIVE_START_TIME = dt_time(MARKET_OPEN.hour, MARKET_OPEN.minute)   # 10:00 ET
LAST_ENTRY_TIME   = dt_time(LAST_ENTRY.hour, LAST_ENTRY.minute)     # 15:15 ET — no new entries
ACTIVE_END_TIME   = EOD_EXIT_TIME                                    # 15:45 ET — force-close
MAX_SPREAD        = 0.50      # skip entry if bid-ask spread wider than this
ENTRY_FILL_TIMEOUT = 30       # seconds to wait for entry market order to fill
STALE_DATA_SEC    = 420       # skip cycle if latest bar > 7 min old (covers 5-min bar + IEX latency)
FROZEN_BAR_LIMIT  = 12        # same bar seen 12×30s = 6 min → likely frozen feed


class Bot:
    def __init__(self):
        self.trade: TradeManager | None = None
        self._shutting_down = False
        self._last_bar_ts = None
        self._frozen_count = 0
        self.daily_pnl = 0.0
        self._loss_cooldown_until: datetime | None = None

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
                self._close_trade()
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

        # Frozen feed detection — same bar repeated too many times
        if latest_ts == self._last_bar_ts:
            self._frozen_count += 1
            if self._frozen_count >= FROZEN_BAR_LIMIT:
                log.warning(f"  ⚠ bar frozen at {latest_ts} for {self._frozen_count} cycles — feed may be stuck")
                return
        else:
            self._frozen_count = 0
            self._last_bar_ts = latest_ts

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

        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            log.warning(f"  🚫 daily loss limit hit (${self.daily_pnl:+.2f}) — no new entries today")
            return

        if self._loss_cooldown_until and now < self._loss_cooldown_until:
            remaining = int((self._loss_cooldown_until - now).total_seconds() / 60) + 1
            log.info(f"  ⏳ loss cooldown active — {remaining}min remaining, skipping entry")
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

        entry_cost = opt["ask"] * 100  # cost of 1 contract

        # Dynamic position sizing based on current buying power.
        # max_per_trade = half the account. slots = how many contracts fit
        # assuming worst-case avg-up 2 price (entry × 1.03).
        # entry_qty = slots − 2 (reserve 2 for avg-ups), capped at 5.
        try:
            buying_power = get_buying_power()
        except Exception as e:
            log.warning(f"  ⚠ couldn't fetch buying power: {e} — skipping entry")
            return

        max_per_trade = buying_power / 2
        slots = int(max_per_trade / (entry_cost * 1.03))

        if slots < 3:
            entry_qty      = 1
            allowed_avg_ups = 0
        elif slots == 3:
            entry_qty      = 1
            allowed_avg_ups = 2
        else:
            entry_qty      = min(slots - 2, 5)  # cap at 5
            allowed_avg_ups = 2

        log.info(
            f"     sizing: buying_power=${buying_power:.0f}  max_per_trade=${max_per_trade:.0f}"
            f"  slots={slots}  entry_qty={entry_qty}  avg_ups_allowed={allowed_avg_ups}"
        )

        # Plain market BUY. Stop SELL is submitted by TradeManager before
        # the avg-up BUYs so Alpaca's wash-trade check doesn't fire.
        # Target exits are poll-only (no standing SELL order at Alpaca).
        try:
            entry = submit_market_buy(opt["symbol"], qty=entry_qty)
            log.info(f"  📥 BUY submitted  id={entry.id}")
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

        self.trade = TradeManager(
            opt["symbol"], direction, fill_price, now,
            entry_qty=entry_qty,
            allowed_avg_ups=allowed_avg_ups,
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
            self._close_trade()

    def _close_trade(self):
        """Log trade close, update daily P&L, set cooldown on loss, clear self.trade."""
        t = self.trade
        self.trade = None
        log.info(f"  ✅ trade closed: {t.summary()}")
        if t.exit_avg_px is not None:
            pnl = (t.exit_avg_px - t.avg_cost) * t.contracts * 100
            self.daily_pnl += pnl
            log.info(f"  📊 daily P&L: ${self.daily_pnl:+.2f}")
            if pnl < 0:
                self._loss_cooldown_until = datetime.now(ET) + timedelta(minutes=LOSS_COOLDOWN_MIN)
                log.info(f"  ⏳ loss cooldown — no new entries until {self._loss_cooldown_until.strftime('%H:%M:%S')} ET")


if __name__ == "__main__":
    Bot().run()
