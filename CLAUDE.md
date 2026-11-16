# Folio: briefing for agents working in this folder

Folio is the user's finance website: a shared market dashboard for a small group of friends. This folder is the
**master copy** of the code. A copy runs as a **group server** on this box. Read this whole file before changing anything.

## The user
- Non-technical. Never ask them to run commands, edit files or judge technical choices. Do it yourself and explain in plain words.
- They don't read progress messages while you work. Keep going on your own as far as is safe.
- End every reply with a short plain-language summary (a few bullets), and make the very last line one clearly marked
  action for them, e.g. `👉 Your next step: ...` ("Nothing, it's all done" is fine).
- They like being asked questions when a request is open-ended (they started with "I have no idea what I want, ask me").
  Use multiple-choice questions with a recommended option.
- Never put secrets in chat. For keys, use the secure secret card (T3 `request_secret`, `delivery: "file"`).

## Where things live
| What | Where |
|---|---|
| Master code (edit here, commit here) | `/home/mk30/.t3/projects/finance` (git repo, no remote yet: nothing pushed to GitHub) |
| Running group server | `/srv/shared/folio`, port **19001**, runs `./start.sh` (= `./.venv/bin/python app.py`) |
| Live data (accounts, portfolios) | `/srv/shared/folio/data/users.json`, `session.key`, `meta.json` |
| Anthropic API key | `/srv/shared/folio/.env` (`ANTHROPIC_API_KEY=...`). Readable by everyone on the box; the user accepted this. |
| Friends' address (tailnet) | http://100.105.248.23:19001 |
| User's private address | https://treehouse.anwar.app/p/20201/ (box serve `folio`: socat forward 20201 → 19001; only works for the user's login) |

`data/` and `.env` in this folder are git-ignored. The local `data/` here is an old copy from before the move; the live data is in `/srv/shared/folio/data/`.

## Shipping a change
1. Edit files here.
2. Test on a throwaway copy with throwaway data (never create test accounts in the live data):
   `FOLIO_DATA=<scratch dir> PORT=20212 timeout 600 .venv/bin/python app.py` (in the background), sign up a test user
   in the browser test, check, then stop it **by PID from the port** (`ss -ltnp | grep 20212`). Don't use
   `pkill -f` with a pattern that matches your own shell command; that kills your shell.
3. Visual check: Playwright with Chromium (`uv run --with playwright python script.py`, launch with `--no-sandbox`).
   Screenshot desktop (1280) and phone (400, dark) and look at them. One look, fix, move on.
4. `./deploy.sh`: copies `app.py`, `pyproject.toml`, `start.sh`, `static/` to `/srv/shared/folio` and restarts it.
5. `git commit` here. End commit messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

New Python packages: install into both venvs (`.venv/bin/pip` here and `/srv/shared/folio/.venv/bin/pip`) and add to `pyproject.toml`.
Group server commands: `box shared restart|logs|stop|start folio`. Anyone on the box can manage it.

## How it's built
- `app.py`: FastAPI backend (one file). `static/index.html`: the whole front end (vanilla JS, Chart.js 4.4.1 from cdnjs). No build step.
- Market data: **yfinance** (Yahoo Finance), with an in-memory cache (`cached(key, ttl, fn)`): quotes 30s, history 1-15 min, news 10 min, sentiment 15-30 min, analyst data 1-6 h.
- AI: official `anthropic` SDK, model `claude-opus-5-5`, `effort: low`, server-side refusal fallback (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`).
  All AI calls go through `ask_claude()`; answers are cached 20-30 min per topic to keep costs to a few cents.
  The "buy or wait" answers use a fixed line format (SIGNAL/SUMMARY/BULL/BEAR/TIMING) parsed by `verdictHtml()` in the front end.
  They must stay balanced summaries with a "not investment advice" note, never telling anyone what to do with their money.
- Accounts: name + 4-8 digit PIN, scrypt-hashed, HMAC-signed session cookie (60 days), 5 wrong PINs → 5-minute lockout.
  Everyone can see everyone's holdings (the user chose this). Only the owner can edit their own portfolio.
- Amounts are converted to USD for portfolio totals (`usdRate`); London pence (GBp) are converted to pounds.
- `FOLIO_DATA` env var overrides the data folder (used for testing).

### Tabs (hash routes)
- `#portfolio`: your value/today/total gain, allocation bar, **group leaderboard** (click someone to view their portfolio read-only), holdings table + add form, price chart, news for holdings + AI briefing, watchlist.
- `#markets`: Today / This week toggle. Index tiles (S&P, Nasdaq, Dow, Russell, VIX, 10-yr yield, oil, gold, dollar index, Bitcoin) with sparklines, AI market brief, sector heatmap (SPDR sector funds), top gainers/losers/most active (Yahoo screens, daily only), market headlines.
- `#sentiment`: Folio's **own** fear & greed index (not CNN's): 6 signals (momentum vs 125-day avg, breadth RSP/SPY, VIX vs 50-day, stocks vs bonds 20d, junk vs safe bonds 20d, RSI) each ranked against the past year (0-100) and averaged; 6-month history chart; "Is now a good time to buy?" AI.
  Industry cards: Technology (XLK), Semiconductors (SMH), Memory & storage (MU, WDC, STX, SNDK basket), Neoclouds (CRWV, NBIS, IREN, APLD basket), Financials (XLF), Consumer (XLY). Each gets a mood score, analyst consensus and member list.
- `#stock`: search any ticker. Returns (1D-1Y), vs S&P and sector, "Why is it moving?" AI, chart, **Sentiment & analyst consensus** (stock mood score, rating breakdown, average target, "Buy now or wait?" AI, then the **analyst price target table**: every rating action in the past 12 months with date, firm, action, rating change, target (and previous target), upside vs today, and target date = rating date + 12 months), key numbers, about, headlines.

### Added 16 Nov 2026 (second feature batch)
- Portfolio: base currency switch USD/CAD/EUR/GBP (localStorage `folio-base`; `/api/fx`). Holdings now store `cur` + `fx`
  (USD per unit of the holding's currency and each base currency on the purchase date, or the day added). Optional "date bought"
  in the add form. Gains split into stock vs currency (`stats()` in the front end). `backfill_fx()` fills old holdings at startup.
- Portfolio mood = value-weighted stock mood (`/api/moods`). Value-over-time chart with S&P/Nasdaq overlay (`/api/portfolio/history`,
  uses today's holdings: no transaction history exists).
- Group: leaderboard Today/1W/1M/All time (`/api/group/returns`), mood column, opt-in activity feed (`shareActivity` pref,
  `data/activity.json`, `/api/activity`, logged in `put_state`; shows % of portfolio, never dollars), group consensus (most held + group mood).
- Stock: earnings (E) / dividend (D) markers on the price chart (`/api/stock/events`), peers table (`/api/stock/peers`: `PEER_HINTS`,
  then our industry lists, then Yahoo industry top companies), what-if forward P/E slider (uses `feps`), market value in the stock's
  own currency + USD equivalent (was wrongly shown with $).
- Markets: "Coming up" strip (`/api/calendar`: hardcoded FOMC dates incl. tentative 2027, Yahoo economic calendar filtered to big US
  releases, Yahoo earnings calendar for >$150B companies or anything the group holds; top 5 prioritised). Clickable sector tiles open
  the fund's top 10 holdings (`/api/sector`). "+ Watch" buttons on movers, sector holdings and peers (global `[data-watch]` handler).
- Sentiment: up to 2 custom baskets per user (`PUT /api/baskets`, `/api/sentiment/custom`), same card as industries (`industry_card()`).
- Yahoo's economic calendar fails when hit in parallel ("Invalid Crumb"): query it sequentially (see `macro_events`).

### Added 16 Nov 2026 (third batch)
- Portfolio: Exposure tabs (asset type / sector / country / currency, `/api/portfolio/diagnostics`) with warnings for any sector >35%
  and holding pairs with 6-month correlation >=0.85; personal events strip (`/api/myevents`: earnings, ex-dividend/pay dates,
  projected next dividend from past rhythm, payout = last dividend × shares); AI stress test (`/api/portfolio/stress`, line format
  EXPOSURE/SUMMARY/HOLDING/CUSHION/WATCH); private trade journal (holding `thesis` + `opened`; removing a holding opens a dialog for a
  takeaway, sent as `takeaways` in PUT /api/state, stored in `users[k].journal`; thesis is stripped from other members' view).
- Watchlist is now a full-width table: target buy price (stored in private research notes), % away, fwd P/E, mood, next earnings (`/api/watchinfo`).
- Earnings TL;DR: `/api/earnings/tldr` finds holdings/watchlist stocks that reported in the last 10 days, generates RESULT/GUIDANCE/TONE
  in a background thread (EPS + SEC 8-K press release + headlines), stored in `data/tldr.json`. Front end polls while `pending`.
- Alerts: `users[k].alerts` rules (price/mood/market F&G above/below), one-shot; `alert_loop` checks every 5 min; triggered → `inbox`.
  Header "Alerts" button + dialog; browser pop-ups only while a tab is open (no email/push set up).
- Stock: Compare drawer (2-4 stocks, `/api/compare`, `/api/stock/peerlist`), valuation multiples with 5-year ranges (`/api/stock/valuation`;
  monthly price vs trailing-12-month statements, today measured the same way), financials & margins charts (`/api/stock/financials`),
  SEC filing summarizer (`/api/stock/filings`: EDGAR submissions → latest 10-Q/10-K MD&A + risk factors vs previous filing + 8-K
  press release; User-Agent is a generic contact, never the user's email; US filers only — SEDAR+/Korea have no free API),
  private research notes with simple markdown (`/api/notes`).
- Markets: plain-English screener (`/api/screen`): Claude turns the query into JSON filters over `universe()` (~130 stocks: sector-fund
  top holdings, industry members, everything the group holds/watches), applied server-side.

### Added 16 Nov 2026 (fourth batch)
- Holdings rows have a "Manage" button: dialog with "I sold some or all" (shares + sale price; optional add proceeds to cash;
  takeaway → journal entry, partial sales recorded with `partial`/`left`; sent as `sells` in PUT /api/state) and "Fix a mistake"
  (edit shares/cost/date/note or delete; sent with `quiet: true` = no activity post, no journal entry; changing the date drops `fx` so it's refilled).
- Cash: `users[k].cash = {amount, currency}` (USD/CAD/EUR/GBP), set via the Cash row in holdings (PUT /api/state with `cash`).
  Counts in portfolio value, allocation ("Cash"), leaderboard value and the value-over-time chart (`cash_usd()`, constant at
  today's balance); gain % and today % are on invested money only.
- Value-over-time ranges: 1D, 1W (30-min), 1M, 3M, 6M, YTD, 1Y, 2Y, 5Y (weekly); price charts also have 2Y.

### Added 16 Nov 2026 (fifth batch)
- User asked to REMOVE the group leaderboard, group activity and group consensus from the Portfolio page (front end removed;
  backend endpoints /api/group/returns and /api/activity still exist but are unused). Per-holding Mood column added instead.
  Holdings and the single-asset chart are now full width (stacked).
- Markets: Macro regime card + cross-asset correlation (`/api/macro`: regime from SPY vs 50/200d, VIX, 10y move over a month;
  `REGIME_SECTORS` rule-of-thumb sector lists; 30-day correlation of SPY/QQQ/TLT/GLD/USO/BTC-USD). Yield curve (10Y minus 2Y,
  2Y from Yahoo `2YY=F` futures) shown inside the 10-yr tile.
- Share image: canvas-drawn 1200×675 PNG cards (`drawCard`) for the market fear & greed gauge and a stock's mood + "Buy now or wait?"
  (includes the AI verdict if it was asked). Download or copy to clipboard.
- Watchlist: column picker (localStorage `folio-wcols`), 7-day sparkline and distance from 52-week high (`/api/watchinfo` adds spark/high52/fromHigh).
- CSV import (client-side parser `readHoldings`: Wealthsimple holdings, Questrade positions, Robinhood activity aggregated to positions,
  Folio export, generic Symbol/Quantity/Average cost; TSX/TSXV/NEO/CSE exchange → .TO/.V/.NE/.CN; preview + ticker check; add or replace;
  saved with `quiet`) and CSV export (incl. a CASH row).
- `tcal()` wraps Yahoo calendars and doesn't cache empty answers (Yahoo sometimes returns {} when rate-limiting).

### Known data limits (tell the user honestly; don't invent data)
- Yahoo lists the **firm**, not the individual analyst's name, for ratings.
- Top movers are daily only; "this week" figures are the past 5 trading sessions.
- Some exchanges are delayed up to 15 minutes.

### Gotchas already hit
- The page is served directly (not as a claude.ai artifact), so the CSS needs its own `[hidden] { display: none !important; }`. It's there; keep it.
- `box serve add` needs an absolute path to the program (systemd rejects `.venv/bin/python`).
- `box shared create` timed out for a while on 15 Nov 2026 but the server had actually been created. Check `box shared ls` before retrying.

## History (15 Nov 2026)
1. User asked for "a finance website" without knowing what they wanted; Q&A gave: investment/stock dashboard, clean & minimal, live prices,
   US stocks + crypto + other markets, portfolio + watchlist + charts + news with AI summary.
2. Made it a group server: personal portfolios, name + PIN, see everything, AI via the user's Anthropic API key.
3. Added Markets (today/week), Stock lookup with "why is it moving".
4. Added Sentiment: fear & greed + industry moods + consensus + AI buy/wait read.
5. Added the analyst price target table under analyst consensus.
6. Code moved into this finance folder (the original T3 starter README/icon were replaced; they're in git history).

## Open items / ideas offered to the user
- Not backed up online: offer to create a private GitHub repo and push (needs `gh auth status`; log in via device code if needed).
- Ideas mentioned but not built: price alerts, a real buy/sell log (would make the value-over-time chart and FX split exact).

## DCF (stock page)
- `/api/stock/dcf` returns per-share inputs in the quote currency: price, fcfPs, niPs, netCashPs, default growth (bear/base/bull),
  terminal growth, discount rate (10-yr yield + beta x 5%, 7-14%). CDRs/foreign listings whose statements are in another
  currency use the parent ticker's figures scaled by the price ratio. Financials: earnings, net cash ignored.
- The math runs in the browser (`dcfValue`): years 1-5 growth, years 6-10 growth, then perpetual growth; fair value today,
  value in 1/3/5 years, reverse-DCF implied growth. Slider settings saved per ticker in localStorage (`folio-dcf-<SYM>`).
- Start options: fcf, fcf3 (3-yr avg), ni, fwd (price / forwardPE), rev (revenue/share x target FCF margin slider). SBC deduction
  applies only to cash-flow starts. Terminal value: perpetuity growth or exit multiple (per case). Share count change %/yr divides
  each year's per-share flow by (1+d)^t. Discount tooltip shows CAPM (rf + beta [0.6-2] x 5% ERP, clamped 7-14%).
- 5x5 sensitivity (discount +-2pp x base growth +-5pp) at the bottom. `POST /api/stock/dcf/suggest` (AI, JSON out, clamped,
  cached 30 min per symbol/start/tv) fills all three cases plus a one-sentence reason each.
- Expected value badge = 25% bear + 50% base + 25% bull fair value. SBC box is hidden in earnings modes. Sensitivity cells
  show 2 decimals; clicking one sets the discount rate and the base case's years 1-5 growth.

