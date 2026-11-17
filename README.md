# Folio

A personal investing dashboard for a small group of friends. Each person signs in with a name and PIN, tracks
their own portfolio and watchlist, and gets market data, stock research and plain-English AI explanations in one page.

Built with a single-file **FastAPI** backend and a single-file **vanilla JavaScript** front end (Chart.js), with no
build step. Market data comes from Yahoo Finance via `yfinance`; filings from SEC EDGAR; AI from Claude (Anthropic API).

## Features

### Portfolio
- Holdings with live prices, today's move, gain/loss, mood (fear & greed) per holding, and a cash balance
- Base currency switch (USD / CAD), with gains split into stock move vs. currency move
- Portfolio breakdown donut: by holding, sector, country, currency or asset type (top 7 + "Other"),
  linked by hover to the holdings table
- Concentration and correlation warnings (any sector over 35%, pairs of holdings moving in lockstep)
- Value over time (1D to 5Y) against the S&P 500 or Nasdaq, including cash
- Best / worst contributor, portfolio mood, AI stress test
- Sell / edit / remove holdings, trade thesis and post-mortem journal, CSV import and export
- Personalised earnings and dividend calendar, watchlist with custom columns and 7-day sparklines,
  smart alerts (price, move, earnings, mood)

### Markets & Sentiment
- Index and macro tiles, yield-curve snapshot, macro regime indicator, 6×6 cross-asset correlation matrix
- Sector drill-downs, biggest movers, catalyst calendar (FOMC, CPI, jobs, big earnings)
- Market and per-industry fear & greed, custom industry baskets
- Natural-language stock screener ("profitable chip stocks under 25x earnings")

### Stock lookup
- Price chart with earnings and dividend markers, "why is it moving?" AI explanation
- Key numbers (colour-coded forward P/E and analyst rating, 52-week position line)
- Financials and margin trends with year-over-year changes
- Peer comparison and a side-by-side compare drawer that highlights the best value per metric
- Sentiment and analyst consensus, individual analyst price targets, "buy now or wait?" AI verdict
- Valuation vs. its own 5-year history (P/E, EV/EBITDA, P/FCF, debt/equity)
- What-if P/E slider
- **DCF model** with bear / base / bull cases: five starting metrics, stock-based-pay deduction, perpetuity or
  exit-multiple terminal value, share dilution/buybacks, CAPM discount rate, reverse DCF, expected value,
  clickable 5×5 sensitivity grid, and an "AI suggest" button that fills in realistic assumptions with reasons
- Latest SEC 10-Q / 10-K summarised by AI, earnings TL;DRs, per-ticker research notes
- Sticky section menu (Overview · Financials · Sentiment · Valuation & DCF · Notes)

Light and dark themes; works on phones.

## Running it

Requires Python 3.12+.

```sh
python -m venv .venv
.venv/bin/pip install fastapi uvicorn yfinance anthropic pandas numpy
.venv/bin/python app.py
```

Then open `http://localhost:19050` and create an account.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | none | Enables the AI features. Without it, everything else still works. |
| `PORT` | `19050` | Port to listen on (binds to `0.0.0.0`). |
| `FOLIO_DATA` | `./data` | Folder for accounts, holdings and caches (JSON files). |

Variables can also be put in a `.env` file next to `app.py`. `.env` and `data/` are git-ignored: never commit them.

## Project layout

```
app.py            FastAPI backend: auth, portfolio state, market data, analysis and AI endpoints
static/index.html The whole front end (HTML, CSS and JS in one file)
start.sh          Starts the server from the local virtualenv
deploy.sh         Copies the app to the shared server folder and restarts it
pyproject.toml    Dependencies
```

## Notes

- Market data is from Yahoo Finance and can be delayed or occasionally missing; Folio retries rather than
  caching blank answers.
- Analyst data lists firms, not individual analysts. SEC filings cover US-listed companies only.
- Alerts appear in the app (and as browser pop-ups while a tab is open); there are no emails.
- Nothing here is investment advice.
