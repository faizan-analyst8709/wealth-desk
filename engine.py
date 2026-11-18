"""Wealth Desk paper-trading engine. PAPER TRADING ONLY: no brokerage, no real orders, no real money.
Deterministic rules, no LLM. Run hourly by GitHub Actions: `python engine.py update`. See README.md.
"""
import csv, json, math, os, time, fcntl, datetime as dt
from zoneinfo import ZoneInfo
import data as D

HERE = os.path.dirname(os.path.abspath(__file__))
ST = os.path.join(HERE, "state")
os.makedirs(ST, exist_ok=True)
PKT = ZoneInfo("Asia/Karachi"); ET = ZoneInfo("America/New_York")

CORE = ["NVDA", "GOOGL", "AMZN", "AMD", "META", "TSLA", "MSFT", "AAPL"]
EXTRA = {"AVGO": "AI networking/custom-chip leader; top-10 S&P weight, adds semis breadth beyond NVDA/AMD",
         "JPM": "Largest US bank; financials diversify away from pure tech",
         "BRK-B": "Berkshire: defensive, cash-rich conglomerate; low beta ballast"}
TICKERS = CORE + list(EXTRA)
BENCH = "SPY"
CAPITAL = 100_000.0
COST = 0.001          # 0.1% assumed cost per trade (commission + slippage)
SMA_N = 50
LIVE_START = "2026-11-18"
BT_START, BT_END = "2025-11-18", "2026-11-17"   # fixed hypothetical backtest window

P_PORT = os.path.join(ST, "portfolio.json")
P_LEDGER = os.path.join(ST, "ledger.csv")
P_LIVE_EQ = os.path.join(ST, "live_equity.csv")
P_BT_LEDGER = os.path.join(ST, "backtest_ledger.csv")
P_BT_EQ = os.path.join(ST, "backtest_equity.csv")
P_BT = os.path.join(ST, "backtest.json")
P_SUM = os.path.join(HERE, "docs", "data", "summary.json")
LEDGER_COLS = ["timestamp_pkt", "date", "phase", "side", "symbol", "shares", "price", "fee", "value", "reason"]
EQ_COLS = ["date", "market_date", "equity", "cash", "spy_equity", "positions", "updated_pkt"]


class Lock:
    def __enter__(self):
        self.f = open(os.path.join(ST, ".lock"), "w"); fcntl.flock(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


def now_pkt():
    return dt.datetime.now(PKT)

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None

def us_market_open_now():
    n = dt.datetime.now(ET)
    return n.weekday() < 5 and dt.time(9, 30) <= n.time() < dt.time(16, 0)


# ---------------- strategy (shared by backtest and live) ----------------
def decide(positions, cash, prices, sig, smas, month_rebal):
    """positions: {sym: shares}. sig: {sym: bool above SMA}. Returns list of (side, sym, shares, reason)."""
    equity = cash + sum(positions.get(s, 0) * prices[s] for s in TICKERS)
    target = equity / len(TICKERS)
    w = f"{100/len(TICKERS):.2f}%"
    sells, buys = [], []
    for s in TICKERS:
        sh, p = positions.get(s, 0), prices[s]
        if sig[s] is None:
            continue
        if sh > 0 and not sig[s]:
            sells.append(("SELL", s, sh, f"close ${p:,.2f} fell below 50-day MA ${smas[s]:,.2f}; move slice to cash"))
        elif sh == 0 and sig[s]:
            q = int(target // (p * (1 + COST)))
            if q > 0:
                buys.append(("BUY", s, q, f"close ${p:,.2f} above 50-day MA ${smas[s]:,.2f}; buy to equal weight {w}"))
        elif sh > 0 and sig[s] and month_rebal:
            want = int(target // (p * (1 + COST)))
            diff = want - sh
            if abs(diff) * p > 0.05 * target and diff != 0:  # only if drift > 5% of target slice
                side = "BUY" if diff > 0 else "SELL"
                (buys if diff > 0 else sells).append((side, s, abs(diff), f"monthly rebalance back to target weight {w}"))
    return sells + buys


def execute(trades, positions, cost_basis, cash, prices, on_trade):
    """Sells first, then buys limited by cash. Mutates positions/cost_basis. Returns cash."""
    for side, s, q, reason in trades:
        p = prices[s]
        if side == "BUY":
            q = min(q, int(cash // (p * (1 + COST))))
            if q <= 0:
                continue
            fee = q * p * COST
            cash -= q * p + fee
            positions[s] = positions.get(s, 0) + q
            cost_basis[s] = cost_basis.get(s, 0.0) + q * p + fee
            on_trade(side, s, q, p, fee, reason, None)
        else:
            fee = q * p * COST
            held = positions.get(s, 0)
            avg = cost_basis.get(s, 0.0) / held if held else 0
            pnl = q * p - fee - avg * q
            cash += q * p - fee
            positions[s] = held - q
            cost_basis[s] = cost_basis.get(s, 0.0) - avg * q
            if positions[s] == 0:
                positions.pop(s); cost_basis.pop(s, None)
            on_trade(side, s, q, p, fee, reason, pnl)
    return cash


# ---------------- data helpers ----------------
def load_hist(fresh=True, rng="2y"):
    syms = TICKERS + [BENCH]
    return D.fetch_all(syms, rng) if fresh else {s: json.load(open(os.path.join(D.DATA, f"{s}.json"))) for s in syms}

def completed_bars(d):
    return d["bars"][:-1] if d.get("session_open") else d["bars"]

def signals_from(hist):
    sig, smas, closes = {}, {}, {}
    for s in TICKERS:
        c = [b[1] for b in completed_bars(hist[s])]
        m = sma(c, SMA_N)
        smas[s], closes[s] = m, c[-1]
        sig[s] = None if m is None else c[-1] > m
    return sig, smas, closes


# ---------------- backtest ----------------
def backtest(hist):
    series = {s: dict(hist[s]["bars"]) for s in TICKERS + [BENCH]}
    alld = sorted(series[BENCH])
    dates = [d for d in alld if BT_START <= d <= BT_END]
    positions, cb, cash = {}, {}, CAPITAL
    ledger, eq, pnls = [], [], []
    spy0 = series[BENCH][dates[0]]; spy_sh = CAPITAL * (1 - COST) / spy0
    last_month = None
    for d in dates:
        idx = alld.index(d)
        prices, sig, smas = {}, {}, {}
        for s in TICKERS:
            hist_c = [series[s][x] for x in alld[: idx + 1] if x in series[s]]
            prices[s] = hist_c[-1]; m = sma(hist_c, SMA_N); smas[s] = m
            sig[s] = None if m is None else hist_c[-1] > m
        month_rebal = last_month is not None and d[:7] != last_month
        last_month = d[:7]
        def rec(side, s, q, p, fee, reason, pnl):
            ledger.append({"timestamp_pkt": d, "date": d, "phase": "backtest", "side": side, "symbol": s, "shares": q,
                           "price": round(p, 4), "fee": round(fee, 2), "value": round(q * p, 2), "reason": reason})
            if pnl is not None: pnls.append(pnl)
        cash = execute(decide(positions, cash, prices, sig, smas, month_rebal), positions, cb, cash, prices, rec)
        e = cash + sum(positions[s] * prices[s] for s in positions)
        eq.append({"date": d, "equity": round(e, 2), "cash": round(cash, 2), "spy_equity": round(spy_sh * series[BENCH][d], 2),
                   "positions": len(positions)})
    write_csv(P_BT_LEDGER, ledger, LEDGER_COLS)
    write_csv(P_BT_EQ, eq, ["date", "equity", "cash", "spy_equity", "positions"])
    st = stats([r["equity"] for r in eq], [r["spy_equity"] for r in eq])
    res = {"label": "HYPOTHETICAL BACKTEST - not actual results", "start": dates[0], "end": dates[-1], "days": len(dates),
           "start_capital": CAPITAL, "end_equity": eq[-1]["equity"], "total_return_pct": round((eq[-1]["equity"] / CAPITAL - 1) * 100, 2),
           "spy_end_equity": eq[-1]["spy_equity"], "spy_return_pct": round((eq[-1]["spy_equity"] / CAPITAL - 1) * 100, 2),
           "max_drawdown_pct": st["max_dd"], "spy_max_drawdown_pct": round(maxdd([r["spy_equity"] for r in eq]) * 100, 2),
           "sharpe": st["sharpe"], "beta": st["beta"], "ann_vol_pct": st["vol"], "trades": len(ledger),
           "buys": sum(1 for r in ledger if r["side"] == "BUY"), "sells": len(pnls),
           "win_rate_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1) if pnls else None,
           "realized_pnl": round(sum(pnls), 2), "fees_paid": round(sum(r["fee"] for r in ledger), 2),
           "open_positions_end": len(positions)}
    json.dump(res, open(P_BT, "w"), indent=2)
    return res, eq, ledger


# ---------------- stats ----------------
def rets(v):
    return [v[i] / v[i - 1] - 1 for i in range(1, len(v))]

def maxdd(v):
    pk, m = v[0], 0
    for x in v:
        pk = max(pk, x); m = min(m, x / pk - 1)
    return m

def stats(v, b):
    r, rb = rets(v), rets(b)
    n = len(r)
    if n < 2:
        return {"sharpe": None, "beta": None, "vol": None, "max_dd": round(maxdd(v) * 100, 2), "ann_ret": None}
    mu = sum(r) / n; sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (n - 1))
    mb = sum(rb) / n; vb = sum((x - mb) ** 2 for x in rb) / (n - 1)
    cov = sum((r[i] - mu) * (rb[i] - mb) for i in range(n)) / (n - 1)
    return {"sharpe": round(mu / sd * math.sqrt(252), 2) if sd else None, "beta": round(cov / vb, 2) if vb else None,
            "vol": round(sd * math.sqrt(252) * 100, 2), "max_dd": round(maxdd(v) * 100, 2),
            "ann_ret": round(((v[-1] / v[0]) ** (252 / n) - 1) * 100, 2)}


# ---------------- io ----------------
def write_csv(p, rows, cols):
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)

def read_csv(p):
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []

def load_port():
    if os.path.exists(P_PORT):
        return json.load(open(P_PORT))
    return {"start_date": LIVE_START, "start_capital": CAPITAL, "cash": CAPITAL, "positions": {}, "cost_basis": {},
            "last_trade_date": None, "spy_shares": None, "spy_start_price": None, "last_month": None}


# ---------------- live ----------------
def mark(hist, port):
    prices = {s: float(hist[s]["price"]) for s in TICKERS + [BENCH]}
    eqv = port["cash"] + sum(q * prices[s] for s, q in port["positions"].items())
    spy_eq = port["spy_shares"] * prices[BENCH] if port["spy_shares"] else CAPITAL
    return prices, eqv, spy_eq

def upsert_live_eq(port, hist, eqv, spy_eq):
    rows = read_csv(P_LIVE_EQ)
    today = now_pkt().date().isoformat()
    mdate = hist[BENCH]["bars"][-1][0]
    row = {"date": today, "market_date": mdate, "equity": round(eqv, 2), "cash": round(port["cash"], 2),
           "spy_equity": round(spy_eq, 2), "positions": len(port["positions"]), "updated_pkt": now_pkt().strftime("%Y-%m-%d %H:%M")}
    old = next((r for r in rows if r["date"] == today), None)
    if old and all(str(old[k]) == str(row[k]) for k in ("market_date", "equity", "cash", "spy_equity", "positions")):
        return rows  # nothing changed: keep file byte-identical (no empty commits)
    rows = [r for r in rows if r["date"] != today] + [row]
    write_csv(P_LIVE_EQ, rows, EQ_COLS)
    return rows

def live_rebalance(hist, port):
    """Run the rules once per PKT date (idempotent). Fills at latest available price."""
    today = now_pkt().date().isoformat()
    if port["last_trade_date"] == today:
        return [], False
    sig, smas, _ = signals_from(hist)
    prices = {s: float(hist[s]["price"]) for s in TICKERS + [BENCH]}
    mkt_month = completed_bars(hist[BENCH])[-1][0][:7]
    month_rebal = port["last_month"] is not None and mkt_month != port["last_month"]
    if port["spy_shares"] is None:
        port["spy_shares"] = CAPITAL * (1 - COST) / prices[BENCH]; port["spy_start_price"] = prices[BENCH]
    made = []; ts = now_pkt().strftime("%Y-%m-%d %H:%M PKT")
    def rec(side, s, q, p, fee, reason, pnl):
        made.append({"timestamp_pkt": ts, "date": today, "phase": "live", "side": side, "symbol": s, "shares": q,
                     "price": round(p, 4), "fee": round(fee, 2), "value": round(q * p, 2), "reason": reason})
    pos = {s: int(q) for s, q in port["positions"].items()}
    port["cash"] = execute(decide(pos, port["cash"], prices, sig, smas, month_rebal), pos, port["cost_basis"], port["cash"], prices, rec)
    port["positions"] = pos
    port["last_trade_date"] = today; port["last_month"] = mkt_month
    if made:
        new = not os.path.exists(P_LEDGER)
        with open(P_LEDGER, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
            if new: w.writeheader()
            w.writerows(made)
    elif not os.path.exists(P_LEDGER):
        write_csv(P_LEDGER, [], LEDGER_COLS)
    return made, True


# ---------------- summary for dashboard ----------------
def monthly(dates, vals):
    out = {}; prev = None; last = {}
    for d, v in zip(dates, vals):
        last[d[:7]] = v
    months = sorted(last)
    for i, m in enumerate(months):
        if i == 0:
            continue
        out[m] = round((last[m] / last[months[i - 1]] - 1) * 100, 2)
    return out

def build_summary(hist, port, live_rows, bt, bt_eq, bt_ledger):
    prices, eqv, spy_eq = mark(hist, port)
    today = now_pkt().date().isoformat()
    prev_rows = [r for r in live_rows if r["date"] < today]
    prev_eq = float(prev_rows[-1]["equity"]) if prev_rows else CAPITAL
    prev_spy = float(prev_rows[-1]["spy_equity"]) if prev_rows else CAPITAL
    sig, smas, _ = signals_from(hist)
    watch = []
    for s in TICKERS + [BENCH]:
        b = hist[s]["bars"]; p = prices[s]
        prevc = b[-2][1] if len(b) > 1 else p
        c = [x[1] for x in completed_bars(hist[s])]
        m = sma(c, SMA_N)
        watch.append({"symbol": s, "price": round(p, 2), "prev_close": round(prevc, 2), "chg": round(p - prevc, 2),
                      "chg_pct": round((p / prevc - 1) * 100, 2), "sma50": round(m, 2) if m else None,
                      "above_sma": (c[-1] > m) if m else None})
    wmap = {w["symbol"]: w for w in watch}
    holdings = []
    for s, q in sorted(port["positions"].items()):
        v = q * prices[s]; cb = port["cost_basis"].get(s, 0)
        holdings.append({"symbol": s, "shares": q, "price": round(prices[s], 2), "chg": wmap[s]["chg"], "chg_pct": wmap[s]["chg_pct"],
                         "value": round(v, 2), "weight_pct": round(v / eqv * 100, 2), "cost_basis": round(cb, 2),
                         "avg_cost": round(cb / q, 2), "unrealized": round(v - cb, 2), "total_ret_pct": round((v / cb - 1) * 100, 2) if cb else 0})
    # chain-linked strategy series (backtest then live), for heatmap/risk
    sd = [r["date"] for r in bt_eq]; sv = [r["equity"] for r in bt_eq]; bv = [r["spy_equity"] for r in bt_eq]
    lr = [r for r in live_rows]
    if lr:
        base_s, base_b = sv[-1], bv[-1]
        for r in lr:
            md = r["market_date"]
            v = base_s * float(r["equity"]) / CAPITAL; b2 = base_b * float(r["spy_equity"]) / CAPITAL
            if md in sd:
                continue  # live day priced off a close already in the backtest window
            sd.append(md); sv.append(v); bv.append(b2)
    heat = {"months": [], "rows": []}
    tick_month = {}
    for s in TICKERS + [BENCH]:
        bars = [x for x in hist[s]["bars"] if x[0] >= "2025-10-31"]
        tick_month[s] = monthly([x[0] for x in bars], [x[1] for x in bars])
    strat_m = monthly(["2025-10-31"] + sd, [CAPITAL] + sv)
    months = sorted(tick_month[BENCH])
    heat["months"] = months
    heat["rows"] = [{"name": "Strategy*", "vals": [strat_m.get(m) for m in months]}] + \
                   [{"name": s, "vals": [tick_month[s].get(m) for m in months]} for s in TICKERS + [BENCH]]
    risk = []
    for s in TICKERS + [BENCH]:
        bars = [x for x in hist[s]["bars"] if x[0] >= BT_START]
        st = stats([x[1] for x in bars], [x[1] for x in bars])
        risk.append({"name": s, "ann_ret": st["ann_ret"], "vol": st["vol"]})
    ss = stats(sv, bv)
    risk.append({"name": "Strategy*", "ann_ret": ss["ann_ret"], "vol": ss["vol"]})
    alloc = [{"label": h["symbol"], "value": h["value"]} for h in holdings] + [{"label": "Cash", "value": round(port["cash"], 2)}]
    live_ledger = read_csv(P_LEDGER)
    recent = list(reversed(live_ledger))[:40] + list(reversed(bt_ledger))[:60]
    sess = hist[BENCH].get("session_open")
    mdate = hist[BENCH]["bars"][-1][0]
    pt = hist[BENCH].get("price_time") or 0
    price_time_pkt = dt.datetime.fromtimestamp(pt, PKT).strftime("%Y-%m-%d %H:%M PKT") if pt else None
    stale = [s for s in hist if hist[s].get("stale")]
    summ = {
        "generated_pkt": now_pkt().strftime("%Y-%m-%d %H:%M:%S PKT"), "paper_trading_only": True,
        "disclaimer": "Simulated paper trading, not financial advice. No real money or brokerage. Backtest results are hypothetical and not a guarantee of future returns.",
        "data_source": hist[BENCH].get("source"), "data_market_date": mdate, "data_price_time_pkt": price_time_pkt,
        "session_open": bool(sess), "stale_symbols": stale,
        "price_basis": ("Intraday: US session is open (opened 7:30 PM PKT); prices are live/early-session, signals use the previous completed close."
                        if sess else f"Prices are the last completed US close ({mdate}). US market opens 7:30 PM PKT (winter)."),
        "strategy": {"name": "Equal-weight trend filter", "tickers": TICKERS, "extras": EXTRA, "benchmark": BENCH, "sma": SMA_N,
                     "cost_pct": COST * 100,
                     "rules": [f"Target weight 1/{len(TICKERS)} ({100/len(TICKERS):.2f}%) per stock.",
                               "Hold a stock only while its last close is above its 50-day moving average; otherwise that slice sits in cash.",
                               "Checked hourly by GitHub Actions but trades at most once per PKT date, after the US close (03:00 PKT or later); entries/exits trade on signal flips.",
                               "First run of each new month trims/tops up held stocks back to target if drift > 5% of the slice.",
                               "Whole shares only; 0.1% assumed cost on every trade."]},
        "live": {"start_date": port["start_date"], "start_capital": CAPITAL, "equity": round(eqv, 2), "cash": round(port["cash"], 2),
                 "day_pl": round(eqv - prev_eq, 2), "day_pl_pct": round((eqv / prev_eq - 1) * 100, 2),
                 "total_pl": round(eqv - CAPITAL, 2), "total_pl_pct": round((eqv / CAPITAL - 1) * 100, 2),
                 "spy_equity": round(spy_eq, 2), "spy_day_pct": round((spy_eq / prev_spy - 1) * 100, 2),
                 "spy_total_pct": round((spy_eq / CAPITAL - 1) * 100, 2),
                 "vs_spy_pct": round((eqv / CAPITAL - spy_eq / CAPITAL) * 100, 2),
                 "last_trade_date": port["last_trade_date"], "positions": len(port["positions"])},
        "backtest": bt,
        "metrics": {"basis": "chain-linked backtest + live daily equity; Sharpe uses 0% risk-free rate", **ss},
        "curve": {"backtest": [{"d": r["date"], "s": r["equity"], "b": r["spy_equity"]} for r in bt_eq],
                  "live": [{"d": r["date"], "md": r["market_date"], "s": float(r["equity"]), "b": float(r["spy_equity"])} for r in live_rows]},
        "allocation": alloc, "holdings": holdings, "watchlist": watch, "heatmap": heat, "risk": risk,
        "trades_today": [r for r in live_ledger if r["date"] == today],
        "recent_trades": recent,
    }
    os.makedirs(os.path.dirname(P_SUM), exist_ok=True)
    if os.path.exists(P_SUM):
        try:
            old = json.load(open(P_SUM))
            if {k: v for k, v in old.items() if k != "generated_pkt"} == json.loads(json.dumps({k: v for k, v in summ.items() if k != "generated_pkt"})):
                return old  # unchanged data: don't rewrite (keeps commits meaningful)
        except Exception:
            pass
    tmp = P_SUM + ".tmp"; json.dump(summ, open(tmp, "w"), indent=1); os.replace(tmp, P_SUM)
    return summ


def bt_cached():
    return json.load(open(P_BT)), [dict(r, equity=float(r["equity"]), spy_equity=float(r["spy_equity"])) for r in read_csv(P_BT_EQ)], read_csv(P_BT_LEDGER)

def trade_window_open():
    """Trade at most once per PKT date, and only while the US regular session is closed and after
    03:00 PKT, so signals use a completed close and fills use that close (or the latest quote)."""
    return (not us_market_open_now()) and now_pkt().hour >= 3

def update(force_trade=False):
    """Hourly job: fetch real prices, run the once-per-PKT-day rebalance if due, mark to market, write JSON."""
    with Lock():
        hist = load_hist(True, "2y")
        if not os.path.exists(P_BT):          # backtest window is fixed; computed once and frozen
            backtest(hist)
        bt, bt_eq, bl = bt_cached()
        port = load_port()
        made = []
        if force_trade or trade_window_open():
            made, ran = live_rebalance(hist, port)
            if ran:
                json.dump(port, open(P_PORT, "w"), indent=2)
        if port["last_trade_date"]:
            prices, eqv, spy_eq = mark(hist, port)
            rows = upsert_live_eq(port, hist, eqv, spy_eq)
        else:
            rows = read_csv(P_LIVE_EQ)
        return build_summary(hist, port, rows, bt, bt_eq, bl)


if __name__ == "__main__":
    import sys
    s = update(force_trade="--force-trade" in sys.argv)
    print(json.dumps({"live": s["live"], "data_source": s["data_source"], "market_date": s["data_market_date"],
                      "quote_time": s["data_price_time_pkt"], "stale": s["stale_symbols"]}, indent=2))
