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
- Ideas mentioned but not built: price alerts, logging buys/sells, dividends, portfolio value over time.
