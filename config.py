# ================================================================
# SHARED CONFIG — credentials + strategy constants
# Imported by every module so we don't duplicate keys/params.
# ================================================================
import datetime
import os
import pytz

# ---------- CREDENTIALS ----------
# Read from environment. Copy .env.example to .env and fill in your keys,
# or export ALPACA_API_KEY / ALPACA_API_SECRET in your shell / crontab.
API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET "
        "(see .env.example) before running."
    )
# ---------------------------------

# Timezone
ET = pytz.timezone("America/New_York")

# Strategy params (used in later steps)
BB_PERIOD    = 20
BB_STD       = 2.0
RSI_PERIOD   = 14
RSI_OB       = 65   # overbought → CALL signal
RSI_OS       = 35   # oversold   → PUT  signal

AVG_UP_1_PCT     = 0.015
AVG_UP_2_PCT     = 0.030
STOP_PCT         = 0.088
TARGET_PCT       = 0.060   # bid-poll threshold — bot market-sells once bid ≥ entry × this
LIMIT_TARGET_PCT = 0.080   # safety-net LIMIT submitted at Alpaca — fires if bot is offline
TOTAL_BUDGET           = 1000  # skip entry if ask × 100 exceeds this
AVG_UP_BUDGET_PER_SLOT = 500   # allow avg-ups only if ask × 100 ≤ this
DAILY_LOSS_LIMIT       = 200   # stop taking new trades if realized P&L ≤ −$200
LOSS_COOLDOWN_MIN      = 10    # minutes to wait before next entry after a losing trade

# Time-based exit — close trade after this many minutes regardless of P&L
# (protects against theta decay on short-dated options)
TIME_EXIT_MIN = 30

# Option chain window — how many days ahead to scan for expiries
OPTION_CHAIN_DAYS = 7

# Loop / hours
CHECK_SECS   = 30
MARKET_OPEN  = datetime.time(10, 0)    # earliest signal entries (10:00 ET / 7:00 PT)
LAST_ENTRY   = datetime.time(15, 15)   # no new entries after this — 30 min runway before EOD
MARKET_CLOSE = datetime.time(16, 0)    # NYSE close (used for reference only)

# Data feed: "iex" is free; "sip" requires a paid subscription
DATA_FEED    = "iex"
