"""Price data fetcher (free, no API key).
Primary: Yahoo chart endpoint (v8) via curl_cffi browser impersonation
        (plain curl/requests with a UA gets HTTP 429 from this box).
Fallback 1: Nasdaq public historical API. Fallback 2: Stooq CSV.
Caches each symbol to data/<SYM>.json. Prices are Yahoo 'close' (split-adjusted, not dividend-adjusted).
"""
import json, os, time, datetime as dt
from curl_cffi import requests as creq

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_s = None

def sess():
    global _s
    if _s is None:
        _s = creq.Session(impersonate="chrome")
    return _s

def _yahoo(sym, rng):
    r = sess().get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d", timeout=20)
    if r.status_code != 200:
        r = sess().get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d", timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    m = res["meta"]
    closes = res["indicators"]["quote"][0]["close"]
    bars = []
    for t, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(t + m.get("gmtoffset", -18000), dt.timezone.utc).date().isoformat()
        bars.append([d, round(float(c), 4)])
    reg = m.get("currentTradingPeriod", {}).get("regular", {})
    now = time.time()
    session_open = bool(reg) and reg["start"] <= now < reg["end"]
    last_complete = not session_open  # last bar is a finished daily close unless session is live
    return {"symbol": sym, "source": "yahoo-chart-v8", "bars": bars,
            "price": float(m.get("regularMarketPrice") or bars[-1][1]),
            "price_time": int(m.get("regularMarketTime") or 0),
            "session_open": session_open, "last_bar_complete": last_complete}

def _nasdaq(sym):
    ns = sym.replace("-", ".")
    to = dt.date.today(); fr = to - dt.timedelta(days=800)
    r = sess().get(f"https://api.nasdaq.com/api/quote/{ns}/historical?assetclass=stocks&fromdate={fr}&todate={to}&limit=9999",
                   headers={"Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    rows = r.json()["data"]["tradesTable"]["rows"]
    bars = []
    for row in reversed(rows):
        m, d, y = row["date"].split("/")
        bars.append([f"{y}-{m}-{d}", float(row["close"].replace("$", "").replace(",", ""))])
    return {"symbol": sym, "source": "nasdaq-api", "bars": bars, "price": bars[-1][1], "price_time": 0,
            "session_open": False, "last_bar_complete": True}

def _stooq(sym):
    r = sess().get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d", timeout=20)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 10 or not lines[0].startswith("Date"):
        raise RuntimeError("stooq returned no data")
    bars = [[l.split(",")[0], float(l.split(",")[4])] for l in lines[1:]][-520:]
    return {"symbol": sym, "source": "stooq-csv", "bars": bars, "price": bars[-1][1], "price_time": 0,
            "session_open": False, "last_bar_complete": True}

def fetch(sym, rng="2y"):
    errs = []
    for fn in (lambda: _yahoo(sym, rng), lambda: _nasdaq(sym), lambda: _stooq(sym)):
        try:
            d = fn()
            if len(d["bars"]) < 5:
                raise RuntimeError("too few bars")
            d["fetched_at"] = int(time.time())
            path = os.path.join(DATA, f"{sym}.json")
            if rng != "2y" and os.path.exists(path):  # merge short refresh into cached history
                old = json.load(open(path))
                hist = {b[0]: b[1] for b in old["bars"]}
                hist.update({b[0]: b[1] for b in d["bars"]})
                d["bars"] = [[k, hist[k]] for k in sorted(hist)]
            if os.path.exists(path):
                try:
                    old = json.load(open(path))
                    if old.get("bars") == d["bars"] and old.get("price") == d["price"] and not old.get("stale") \
                            and old.get("session_open") == d["session_open"]:
                        return old  # unchanged: keep cache file identical (no empty commits)
                except Exception:
                    pass
            json.dump(d, open(path, "w"))
            return d
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
    path = os.path.join(DATA, f"{sym}.json")
    if os.path.exists(path):
        d = json.load(open(path)); d["stale"] = True; d["errors"] = errs  # last real prices, flagged stale; nothing invented
        return d
    raise RuntimeError(f"all sources failed for {sym}: {errs}")

def fetch_all(syms, rng="2y"):
    out = {}
    for s in syms:
        out[s] = fetch(s, rng)
        time.sleep(0.3)
    return out

if __name__ == "__main__":
    import sys
    for s in (sys.argv[1:] or ["AAPL", "SPY"]):
        d = fetch(s)
        print(s, d["source"], len(d["bars"]), d["bars"][-1], "price", d["price"], "session_open", d["session_open"])
