# ================================================================
# CONFIG — credentials + strategy constants for alpacabot-5m5mcall
# ================================================================
import os
import pytz

# ---------- CREDENTIALS ----------
API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET."
    )

ET = pytz.timezone("America/New_York")

# Exit mechanics (same as alpacabot except TIME_EXIT_MIN)
AVG_UP_1_PCT     = 0.015
AVG_UP_2_PCT     = 0.030
STOP_PCT         = 0.088
TARGET_PCT       = 0.060
LIMIT_TARGET_PCT = 0.080
TIME_EXIT_MIN    = 15        # ← 15 min (alpacabot uses 30)

# Budget / risk
TOTAL_BUDGET           = 1000
AVG_UP_BUDGET_PER_SLOT = 500
DAILY_LOSS_LIMIT       = 200

# Option chain
OPTION_CHAIN_DAYS = 7

# Candle fetch retry
CANDLE_RETRIES     = 5
CANDLE_RETRY_SLEEP = 5   # seconds between retries

# Trade management poll interval
CHECK_SECS = 15

# Entry guards
MAX_SPREAD         = 0.50
ENTRY_FILL_TIMEOUT = 30

# Data feed
DATA_FEED = "iex"
