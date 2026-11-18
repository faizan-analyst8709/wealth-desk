# Wealth Desk: paper-trading dashboard (SIMULATED, NOT FINANCIAL ADVICE)

**Paper trading only.** It has no brokerage connection, places no real orders and uses no real money. Every trade is simulated
against real end-of-day/intraday prices fetched from free public sources. The backtest is hypothetical and does not guarantee future results.

- Live dashboard (GitHub Pages): https://faizan-analyst8709.github.io/wealth-desk/
- Updated hourly by `.github/workflows/update.yml` (cron `0 * * * *` + manual `workflow_dispatch`).

## How it works
1. `engine.py update` (run by Actions) fetches 2 years of daily prices per symbol via `data.py`:
   Yahoo chart v8 endpoint (curl_cffi Chrome impersonation), then Nasdaq public API, then Stooq CSV as fallbacks.
   If every source fails for a symbol, the last real cached prices in `data/` are reused and flagged `stale` in the JSON. Nothing is invented.
2. The rules run **at most once per Asia/Karachi (PKT) date**, only while the US regular session is closed and at 03:00 PKT or later,
   so signals use a completed close. Every trade is appended to `state/ledger.csv` with a plain-words reason.
3. It marks the portfolio to market and writes `docs/data/summary.json`. The workflow commits only when the data actually changed, then
   deploys `docs/` to GitHub Pages. The page re-reads the JSON every 60s.

## Strategy (plain words)
- Universe: NVDA, GOOGL, AMZN, AMD, META, TSLA, MSFT, AAPL, plus AVGO (AI networking/custom chips), JPM (largest US bank,
  diversifies away from tech), BRK-B (defensive, low-beta ballast). Benchmark: SPY buy-and-hold with the same $100,000.
- Each stock targets 1/11 (9.09%) of the portfolio. A stock is held only while its last close is above its 50-day moving average; otherwise that slice is cash.
- On the first trading run of each new month, holdings are trimmed or topped up to target when they've drifted more than 5% of the slice.
- Whole shares only, with an assumed 0.1% cost per trade. Live paper portfolio started 2026-11-18 with $100,000.
- Hypothetical backtest window: 2025-11-18 to 2026-11-17 (frozen in `state/backtest*.{json,csv}`).

## Files
- `state/portfolio.json`: cash, shares, cost basis, last trade date
- `state/ledger.csv`: live simulated trades (timestamp, side, shares, price, fee, reason)
- `state/live_equity.csv`: daily live equity vs SPY
- `state/backtest_*.csv`, `state/backtest.json`: hypothetical backtest
- `data/<SYM>.json`: last fetched real price history (also the stale fallback)
- `docs/`: static dashboard (`index.html`, vendored Chart.js, `data/summary.json`)
- `scripts/write_note.py`: writes a short daily markdown note from `docs/data/summary.json`

## Limits
GitHub's scheduled workflows can start several minutes late (or occasionally be skipped at busy times). GitHub disables schedules in
public repos after 60 days with no repository activity; the hourly data commits count as activity, but there are none on days when nothing changes.
