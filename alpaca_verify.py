# ================================================================
# STEP 1 — VERIFY ALPACA API CREDENTIALS
# ================================================================
# Install:
#   pip install alpaca-py
#
# Run:
#   python alpaca_verify.py
# ================================================================

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# ---------- CREDENTIALS ----------
API_KEY    = "PKUIH6DMVHXVP5UKX7YCEZ6U3N"
API_SECRET = "ANg9o9prUvjKcgV34awwAVGT8MTBfpxNBktg1sH9Z64A"
PAPER      = True   # True = paper account, False = live
# ---------------------------------


def check_trading_api():
    print("\n[1] Checking TRADING API (account access)...")
    client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    account = client.get_account()

    print(f"   ✅ Connected!")
    print(f"   Account #     : {account.account_number}")
    print(f"   Status        : {account.status}")
    print(f"   Currency      : {account.currency}")
    print(f"   Cash          : ${float(account.cash):,.2f}")
    print(f"   Equity        : ${float(account.equity):,.2f}")
    print(f"   Buying Power  : ${float(account.buying_power):,.2f}")
    print(f"   Pattern DT    : {account.pattern_day_trader}")
    return True


def check_market_data_api():
    print("\n[2] Checking MARKET DATA API (SPY quote)...")
    client = StockHistoricalDataClient(API_KEY, API_SECRET)
    request = StockLatestTradeRequest(symbol_or_symbols="SPY")
    trades = client.get_stock_latest_trade(request)
    spy = trades["SPY"]

    print(f"   ✅ Connected!")
    print(f"   SPY last price: ${spy.price:.2f}")
    print(f"   Timestamp     : {spy.timestamp}")
    return True


def main():
    mode = "PAPER" if PAPER else "LIVE"
    print("=" * 50)
    print(f"  ALPACA API CREDENTIAL CHECK — {mode} MODE")
    print("=" * 50)

    try:
        check_trading_api()
        check_market_data_api()
        print("\n" + "=" * 50)
        print("  🎉 ALL CHECKS PASSED — credentials are working")
        print("=" * 50)
    except Exception as e:
        print(f"\n   ❌ FAILED: {type(e).__name__}: {e}")
        print("\n   Things to check:")
        print("   - API key & secret are correct")
        print("   - PAPER flag matches the key type (paper vs live)")
        print("   - alpaca-py is installed: pip install alpaca-py")


if __name__ == "__main__":
    main()
