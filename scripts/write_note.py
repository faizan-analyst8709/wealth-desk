"""Write the short daily stats note from the repo's latest docs/data/summary.json (no trading here).
Usage: python scripts/write_note.py /workspace/daily-reports  -> <dir>/YYYY-MM-DD/trading.md (Asia/Karachi date)"""
import json, os, sys, datetime as dt
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
s = json.load(open(os.path.join(HERE, "docs", "data", "summary.json")))
L, B = s["live"], s["backtest"]
today = dt.datetime.now(ZoneInfo("Asia/Karachi")).date().isoformat()
out = os.path.join(sys.argv[1] if len(sys.argv) > 1 else "/workspace/daily-reports", today); os.makedirs(out, exist_ok=True)
f = lambda x: f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"
tt = [t for t in s["recent_trades"] if t["phase"] == "live" and t["date"] == today]
lines = [f"# Wealth Desk paper trading - {today}", "",
 f"- **Portfolio value:** ${L['equity']:,.2f} (start ${L['start_capital']:,.0f} on {L['start_date']})",
 f"- **Day P/L:** {f(L['day_pl'])} ({L['day_pl_pct']:+.2f}%) | **Total P/L:** {f(L['total_pl'])} ({L['total_pl_pct']:+.2f}%)",
 f"- **vs SPY buy-and-hold:** SPY ${L['spy_equity']:,.2f} ({L['spy_total_pct']:+.2f}%); strategy {L['vs_spy_pct']:+.2f} pts vs SPY",
 f"- **Positions ({len(s['holdings'])}):** " + (", ".join(f"{h['symbol']} {h['shares']}sh" for h in s["holdings"]) or "none (all cash)") + f"; cash ${L['cash']:,.2f}",
 "- **Trades today:** " + ("; ".join(f"{t['side']} {t['shares']} {t['symbol']} @ ${float(t['price']):,.2f} ({t['reason']})" for t in tt) or "none"),
 f"- **Data:** {s['data_source']}, market date {s['data_market_date']}, quote time {s['data_price_time_pkt']}; JSON generated {s['generated_pkt']}. {s['price_basis']}"
 + (f" STALE: {', '.join(s['stale_symbols'])}." if s["stale_symbols"] else ""),
 f"- **Backtest (hypothetical, {B['start']} to {B['end']}):** {B['total_return_pct']:+.2f}% vs SPY {B['spy_return_pct']:+.2f}%",
 "", "_Simulated paper trading with assumed 0.1% costs; not real money, not financial advice; backtested/past results do not guarantee future returns._", ""]
p = os.path.join(out, "trading.md"); open(p, "w").write("\n".join(lines)); print(p)
