# ================================================================
# BOT — alpacabot-5m5mcall main runner
#
# Lifecycle:
#   1. Startup checks (positions, orders — PID handled by run script)
#   2. Fetch 9:30 ET 5-min candle (with retry)
#   3. Green → enter CALL trade
#      Red   → log "no signal", exit 0
#   4. TradeManager.tick() every 30s until done
#   5. exit 0
# ================================================================
import logging
import os
import signal
import sys
import time
from datetime import datetime

import pandas as pd

from config import (
    PAPER, CHECK_SECS, ET,
    MAX_SPREAD, ENTRY_FILL_TIMEOUT,
)
from data import get_opening_candle
from options import resolve_option
from orders import (
    list_positions, list_open_orders,
    submit_market_buy, wait_for_fill, get_buying_power,
)
from trade_manager import TradeManager

# ---------------- logging ----------------
log = logging.getLogger("bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)

os.makedirs("logs", exist_ok=True)
_logdate = datetime.now().strftime("%Y%m%d")
_ef = logging.FileHandler(f"logs/events_{_logdate}.log")
_ef.setFormatter(_fmt)
log.addHandler(_ef)


# ----------------------------------------------------------------
# Pure helpers (independently testable)
# ----------------------------------------------------------------

def is_green(candle: pd.Series) -> bool:
    """Return True if the candle closed above its open."""
    return float(candle["close"]) > float(candle["open"])


def compute_sizing(entry_cost: float, buying_power: float) -> tuple[int, int]:
    """
    Extreme back-loading position sizing: entry=1, avg-ups maximize.

    With 88.9% win rate on avg-ups, minimizes entry risk while maximizing upside.
    Safety: max 50% of buying power per trade.
    """
    max_per_trade = buying_power / 2
    total_qty = int(max_per_trade / (entry_cost * 1.03))

    if total_qty < 1:
        return 0, 0

    # Extreme back-loading (1-X-X): entry always 1, rest split between avg-ups
    entry_qty = 1
    allowed_avg_ups = 2 if total_qty >= 3 else 0

    return entry_qty, allowed_avg_ups


# ----------------------------------------------------------------
# Bot
# ----------------------------------------------------------------

class Bot:
    def __init__(self):
        self._shutting_down = False

    def startup_checks(self):
        log.info("=" * 60)
        log.info(" SPY 5m-5m CALL Bot")
        log.info("=" * 60)
        log.info(f"  Mode      : {'PAPER' if PAPER else 'LIVE'}")
        log.info(f"  Signal    : 9:30 ET 5-min candle green → CALL")
        log.info(f"  Time exit : 15 min")
        log.info(f"  Avg-ups   : +1.5% / +3.0%")
        log.info(f"  Stop      : avg × 0.912")
        log.info(f"  Target    : initial × 1.06 (poll)")
        log.info("=" * 60)

        positions = list_positions()
        spy_options = [p for p in positions
                       if p.symbol.startswith("SPY") and len(p.symbol) > 6]
        if spy_options:
            log.error("  ❌ Refusing to start — existing SPY option position(s):")
            for p in spy_options:
                log.error(f"     {p.symbol}  qty={p.qty}")
            log.error("     Close them manually, then re-start.")
            sys.exit(1)

        opens = list_open_orders()
        spy_opens = [o for o in opens
                     if o.symbol.startswith("SPY") and len(o.symbol) > 6]
        if spy_opens:
            log.error("  ❌ Refusing to start — open SPY option orders:")
            for o in spy_opens:
                log.error(f"     {o.symbol}  {o.side}  id={o.id}")
            log.error("     Cancel them manually, then re-start.")
            sys.exit(1)

        log.info("  ✅ Startup checks passed")

    def run(self):
        self.startup_checks()
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        candle = get_opening_candle()
        if candle is None:
            log.error("  ❌ 9:30 candle not available after retries — exiting")
            sys.exit(1)

        move = float(candle["close"]) - float(candle["open"])
        log.info(
            f"  📊 9:30 candle: open={float(candle['open']):.2f}  "
            f"close={float(candle['close']):.2f}  "
            f"move={move:+.3f}  "
            f"{'GREEN ✅' if is_green(candle) else 'RED ❌'}"
        )

        if not is_green(candle):
            log.info("  ⛔ No signal (candle red) — exiting")
            sys.exit(0)

        trade = self._enter_trade(float(candle["close"]))
        if trade is None:
            log.info("  ⛔ Entry failed or skipped — exiting")
            sys.exit(0)

        while not self._shutting_down:
            now = datetime.now(ET)
            try:
                done = trade.tick(now)
            except Exception as e:
                log.exception(f"  ❌ trade.tick raised: {e}")
                done = False
            if done:
                log.info(f"  ✅ Trade closed: {trade.summary()}")
                break
            time.sleep(CHECK_SECS)

        log.info("Bot exiting cleanly.")
        sys.exit(0)

    def _handle_signal(self, signum, frame):
        log.info(f"  ⚠ received signal {signum} — shutting down after this tick")
        self._shutting_down = True

    def _enter_trade(self, spy_price: float):
        log.info(f"  🚨 SIGNAL: CALL @ SPY={spy_price:.2f} — resolving contract")
        try:
            opt = resolve_option("CALL", spy_price)
        except Exception as e:
            log.error(f"  ❌ resolve_option failed: {e}")
            return None

        log.info(
            f"     contract: {opt['symbol']}  strike=${opt['strike']:.2f}"
            f"  expiry={opt['expiry']}"
        )
        log.info(
            f"     quote: bid=${opt['bid']:.2f}  ask=${opt['ask']:.2f}"
            f"  spread=${opt['spread']:.2f}"
        )

        if opt["spread"] > MAX_SPREAD:
            log.warning(f"  ⚠ spread ${opt['spread']:.2f} > ${MAX_SPREAD:.2f} — skipping")
            return None

        entry_cost = opt["ask"] * 100
        try:
            buying_power = get_buying_power()
        except Exception as e:
            log.warning(f"  ⚠ couldn't fetch buying power: {e} — skipping")
            return None

        entry_qty, allowed_avg_ups = compute_sizing(entry_cost, buying_power)
        log.info(
            f"     sizing: buying_power=${buying_power:.0f}"
            f"  entry_qty={entry_qty}  avg_ups_allowed={allowed_avg_ups}"
        )

        try:
            entry = submit_market_buy(opt["symbol"], qty=entry_qty)
            log.info(f"  📥 BUY submitted  id={entry.id}")
            filled = wait_for_fill(entry.id, timeout=ENTRY_FILL_TIMEOUT)
        except TimeoutError:
            log.error(f"  ❌ entry didn't fill in {ENTRY_FILL_TIMEOUT}s")
            return None
        except Exception as e:
            log.error(f"  ❌ entry submit/fill failed: {e}")
            return None

        if filled.status.value != "filled":
            log.error(f"  ❌ entry status={filled.status} — aborting")
            return None

        fill_price = float(filled.filled_avg_price)
        log.info(f"  ✅ ENTRY FILLED @ ${fill_price:.2f}")

        now = datetime.now(ET)
        trade = TradeManager(
            opt["symbol"], "CALL", fill_price, now,
            entry_qty=entry_qty,
            allowed_avg_ups=allowed_avg_ups,
        )
        try:
            trade.submit_management_orders()
        except Exception as e:
            log.exception(f"  ❌ failed to submit management orders: {e}")

        return trade


if __name__ == "__main__":
    Bot().run()
