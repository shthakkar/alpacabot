# ================================================================
# BACKTEST REPORT — turn backtest_trades.csv into an HTML report
# Step 6.5 stage 2.
#
# Usage:
#   python3 backtest_report.py
# Opens backtest_report.html in the default browser.
# ================================================================
import argparse
import csv
import os
import webbrowser
from collections import Counter, defaultdict
from pathlib     import Path

CSV_PATH  = "backtest_trades.csv"
HTML_PATH = "backtest_report.html"
TITLE     = "SPY Mean-Reversion Bot — Backtest"


def read_trades(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "OK":
                continue
            r["pnl_dollars"]   = float(r["pnl_dollars"])   if r["pnl_dollars"]   else 0.0
            r["pnl_per_share"] = float(r["pnl_per_share"]) if r["pnl_per_share"] else 0.0
            r["contracts"]     = int(r["contracts"])       if r["contracts"]     else 0
            r["spy_at_signal"] = float(r["spy_at_signal"]) if r["spy_at_signal"] else 0.0
            rows.append(r)
    return rows


def case_label(t):
    """Map (contracts, exit_reason) to A/B/C/D or other label."""
    c, e = t["contracts"], t["exit_reason"]
    if c == 3 and e == "TARGET":   return "A — 3 buys + target"
    if c == 3 and e == "STOP":     return "B — 3 buys + stop"
    if c == 1 and e == "STOP":     return "C — 1 buy + stop"
    if c == 2 and e == "STOP":     return "D — 2 buys + stop"
    return f"{c}c + {e}"           # everything else (TIME exits, 1/2-target, etc.)


def aggregate(trades):
    """Group + summarize."""
    by_case   = defaultdict(list)
    by_dir    = defaultdict(list)
    by_day    = defaultdict(list)
    for t in trades:
        by_case[case_label(t)].append(t)
        by_dir[t["direction"]].append(t)
        by_day[t["day"]].append(t)

    summary = {
        "total_trades": len(trades),
        "wins":         sum(1 for t in trades if t["pnl_dollars"] > 0),
        "losses":       sum(1 for t in trades if t["pnl_dollars"] < 0),
        "flat":         sum(1 for t in trades if t["pnl_dollars"] == 0),
        "total_pnl":    sum(t["pnl_dollars"] for t in trades),
        "avg_pnl":      (sum(t["pnl_dollars"] for t in trades) / len(trades)) if trades else 0,
        "max_win":      max((t["pnl_dollars"] for t in trades), default=0),
        "max_loss":     min((t["pnl_dollars"] for t in trades), default=0),
    }
    summary["win_rate"] = summary["wins"] / summary["total_trades"] if trades else 0

    return summary, by_case, by_dir, by_day


def render_html(summary, by_case, by_dir, by_day, trades):
    def fmt_money(x):
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    case_rows = ""
    for label in sorted(by_case.keys()):
        rows = by_case[label]
        pnl  = sum(r["pnl_dollars"] for r in rows)
        avg  = pnl / len(rows)
        case_rows += (
            f"<tr><td>{label}</td>"
            f"<td>{len(rows)}</td>"
            f"<td>{len(rows)/summary['total_trades']*100:.1f}%</td>"
            f"<td>{fmt_money(avg)}</td>"
            f"<td>{fmt_money(pnl)}</td></tr>"
        )

    dir_rows = ""
    for d in ("CALL", "PUT"):
        rows = by_dir.get(d, [])
        if not rows:
            continue
        pnl = sum(r["pnl_dollars"] for r in rows)
        wins = sum(1 for r in rows if r["pnl_dollars"] > 0)
        dir_rows += (
            f"<tr><td>{d}</td><td>{len(rows)}</td>"
            f"<td>{wins/len(rows)*100:.1f}%</td>"
            f"<td>{fmt_money(pnl)}</td></tr>"
        )

    day_rows = ""
    cum = 0
    for day in sorted(by_day.keys()):
        rows = by_day[day]
        pnl  = sum(r["pnl_dollars"] for r in rows)
        cum += pnl
        day_rows += (
            f"<tr><td>{day}</td><td>{len(rows)}</td>"
            f"<td>{fmt_money(pnl)}</td><td>{fmt_money(cum)}</td></tr>"
        )

    trade_rows = ""
    for t in trades:
        trade_rows += (
            "<tr>"
            f"<td>{t['signal_time'][:16].replace('T',' ')}</td>"
            f"<td>{t['direction']}</td>"
            f"<td>${t['spy_at_signal']:.2f}</td>"
            f"<td>{t['symbol']}</td>"
            f"<td>${float(t['entry_price']):.2f}</td>"
            f"<td>{t['contracts']}</td>"
            f"<td>{t['exit_reason']}</td>"
            f"<td>${float(t['exit_price']):.2f}</td>"
            f"<td>{fmt_money(t['pnl_dollars'])}</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{TITLE}</title>
<style>
  body {{ font-family:-apple-system,Helvetica,Arial,sans-serif;
          max-width:1100px; margin:30px auto; color:#222; padding:0 20px; }}
  h1 {{ border-bottom:2px solid #333; padding-bottom:8px; }}
  h2 {{ margin-top:36px; }}
  .summary-grid {{ display:grid; grid-template-columns: repeat(4,1fr);
                  gap:12px; margin:20px 0; }}
  .stat {{ background:#f5f7fa; padding:14px; border-radius:8px;
          border-left:4px solid #4a90e2; }}
  .stat .label {{ font-size:12px; color:#666; text-transform:uppercase; letter-spacing:.5px;}}
  .stat .val {{ font-size:22px; font-weight:600; margin-top:4px; }}
  table {{ border-collapse:collapse; width:100%; margin-top:12px;
           font-size:14px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #e2e2e2; text-align:left;}}
  th {{ background:#fafafa; font-weight:600; }}
  tr:hover {{ background:#fbfbfd; }}
  .pos {{ color:#1a8f3c; font-weight:600; }}
  .neg {{ color:#cc2222; font-weight:600; }}
  .small {{ font-size:12px; color:#777; }}
</style></head><body>

<h1>{TITLE}</h1>
<p class="small">Generated from <code>{CSV_PATH}</code>. {summary['total_trades']} trades simulated.</p>

<div class="summary-grid">
  <div class="stat"><div class="label">Trades</div>
                    <div class="val">{summary['total_trades']}</div></div>
  <div class="stat"><div class="label">Win rate</div>
                    <div class="val">{summary['win_rate']*100:.1f}%</div></div>
  <div class="stat"><div class="label">Total P&amp;L</div>
                    <div class="val">{fmt_money(summary['total_pnl'])}</div></div>
  <div class="stat"><div class="label">Avg / trade</div>
                    <div class="val">{fmt_money(summary['avg_pnl'])}</div></div>
  <div class="stat"><div class="label">Wins / Losses / Flat</div>
                    <div class="val">{summary['wins']} / {summary['losses']} / {summary['flat']}</div></div>
  <div class="stat"><div class="label">Best trade</div>
                    <div class="val">{fmt_money(summary['max_win'])}</div></div>
  <div class="stat"><div class="label">Worst trade</div>
                    <div class="val">{fmt_money(summary['max_loss'])}</div></div>
  <div class="stat"><div class="label">Break-even WR</div>
                    <div class="val">{(abs(summary['max_loss'])/(summary['max_win']+abs(summary['max_loss']))*100 if (summary['max_win']+abs(summary['max_loss']))>0 else 0):.0f}%</div></div>
</div>

<h2>Case distribution (A / B / C / D &amp; others)</h2>
<table>
  <tr><th>Case</th><th>Count</th><th>%</th><th>Avg P&amp;L</th><th>Total P&amp;L</th></tr>
  {case_rows}
</table>

<h2>By direction</h2>
<table>
  <tr><th>Direction</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th></tr>
  {dir_rows}
</table>

<h2>By day (cumulative)</h2>
<table>
  <tr><th>Date</th><th>Trades</th><th>Daily P&amp;L</th><th>Cumulative</th></tr>
  {day_rows}
</table>

<h2>All trades</h2>
<table>
  <tr><th>Signal time</th><th>Dir</th><th>SPY</th><th>Symbol</th>
      <th>Entry</th><th>#</th><th>Exit</th><th>Exit px</th><th>P&amp;L</th></tr>
  {trade_rows}
</table>

</body></html>"""
    return html


def main():
    global CSV_PATH, HTML_PATH, TITLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",   default=CSV_PATH)
    ap.add_argument("--html",  default=HTML_PATH)
    ap.add_argument("--title", default=TITLE)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    CSV_PATH, HTML_PATH, TITLE = args.csv, args.html, args.title

    if not Path(CSV_PATH).exists():
        print(f"❌ {CSV_PATH} not found. Run backtest.py first.")
        return

    trades = read_trades(CSV_PATH)
    if not trades:
        print("⚠ No trades in CSV. Nothing to report.")
        return

    summary, by_case, by_dir, by_day = aggregate(trades)
    html = render_html(summary, by_case, by_dir, by_day, trades)

    with open(HTML_PATH, "w") as f:
        f.write(html)

    print(f"\nSummary:")
    print(f"  trades : {summary['total_trades']}")
    print(f"  win %  : {summary['win_rate']*100:.1f}%")
    print(f"  P&L    : ${summary['total_pnl']:+,.2f}")
    print(f"  avg    : ${summary['avg_pnl']:+,.2f}")
    print(f"\n✅ wrote {HTML_PATH}")

    # Open in default browser
    if not args.no_open:
        abs_path = os.path.abspath(HTML_PATH)
        webbrowser.open(f"file://{abs_path}")


if __name__ == "__main__":
    main()
