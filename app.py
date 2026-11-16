"""Folio: a shared market dashboard for a group of friends.
Each person signs in with a name + PIN and keeps their own portfolio; everyone can see everyone's.
Prices from Yahoo Finance via yfinance. AI briefings via the Anthropic API (ANTHROPIC_API_KEY in .env)."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("FOLIO_DATA", ROOT / "data"))
DATA.mkdir(exist_ok=True)
USERS_FILE = DATA / "users.json"
META_FILE = DATA / "meta.json"
SECRET_FILE = DATA / "session.key"

DEFAULT_WATCHLIST = ["^GSPC", "BTC-USD", "GC=F", "EURUSD=X"]


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

app = FastAPI()
pool = ThreadPoolExecutor(16)
lock = threading.Lock()
_cache: dict = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


meta = load_json(META_FILE, {})

TYPE_CLASS = {
    "EQUITY": "Stocks", "ETF": "Funds", "MUTUALFUND": "Funds",
    "CRYPTOCURRENCY": "Crypto", "CURRENCY": "Forex", "FUTURE": "Commodities",
    "INDEX": "Index",
}


def get_meta(sym):
    if sym in meta:
        return meta[sym]
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        info = {}
    qt = info.get("quoteType", "")
    m = {
        "name": info.get("shortName") or info.get("longName") or sym,
        "type": qt,
        "cls": TYPE_CLASS.get(qt, "Other"),
        "exchange": info.get("fullExchangeName") or info.get("exchange") or "",
    }
    if info:
        with lock:
            meta[sym] = m
            save_json(META_FILE, meta)
    return m


def fx_to_usd(cur):
    if cur in ("USD", None, ""):
        return 1.0
    def f():
        return float(yf.Ticker(f"{cur}USD=X").fast_info.last_price)
    try:
        return cached(("fx", cur), 300, f)
    except Exception:
        return None


def one_quote(sym):
    def f():
        fi = yf.Ticker(sym).fast_info
        price = float(fi.last_price)
        prev = float(fi.previous_close or fi.regular_market_previous_close or price)
        cur = fi.currency or "USD"
        if cur == "GBp":  # London quotes in pence
            price, prev, cur = price / 100, prev / 100, "GBP"
        m = get_meta(sym)
        return {
            "symbol": sym, "price": price, "prev": prev,
            "change": price - prev, "pct": (price / prev - 1) * 100 if prev else 0,
            "currency": cur, "usdRate": fx_to_usd(cur), **m, "ts": time.time(),
        }
    try:
        return cached(("q", sym), 30, f)
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:120]}


@app.get("/api/quotes")
def quotes(symbols: str):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:60]
    return list(pool.map(one_quote, syms))


RANGES = {
    "1D": ("1d", "5m"), "5D": ("5d", "30m"), "1M": ("1mo", "1d"),
    "6M": ("6mo", "1d"), "1Y": ("1y", "1d"), "5Y": ("5y", "1wk"),
}


@app.get("/api/history")
def history(symbol: str, range: str = "1M"):
    if range not in RANGES:
        raise HTTPException(400, "bad range")
    period, interval = RANGES[range]
    def f():
        h = yf.Ticker(symbol).history(period=period, interval=interval)
        div = 100 if (yf.Ticker(symbol).fast_info.currency == "GBp") else 1
        return [[int(ts.timestamp() * 1000), round(float(c) / div, 6)]
                for ts, c in zip(h.index, h["Close"]) if c == c]
    return cached(("h", symbol, range), 60 if range in ("1D", "5D") else 900, f)


@app.get("/api/search")
def search(q: str):
    def f():
        res = yf.Search(q, max_results=8, news_count=0).quotes
        return [{"symbol": r.get("symbol"), "name": r.get("shortname") or r.get("longname") or "",
                 "type": r.get("quoteType", ""), "exchange": r.get("exchDisp", "")}
                for r in res if r.get("symbol")]
    try:
        return cached(("s", q.lower()), 3600, f)
    except Exception:
        return []


def news_for(sym):
    def f():
        out = []
        for n in (yf.Ticker(sym).news or [])[:6]:
            c = n.get("content", n)
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
            if not c.get("title") or not url:
                continue
            out.append({"id": c.get("id"), "title": c["title"], "summary": c.get("summary") or "",
                        "url": url, "source": (c.get("provider") or {}).get("displayName", ""),
                        "time": c.get("pubDate"), "symbol": sym})
        return out
    try:
        return cached(("n", sym), 600, f)
    except Exception:
        return []


@app.get("/api/news")
def news(symbols: str):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    seen, items = set(), []
    for lst in pool.map(news_for, syms):
        for it in lst:
            if it["title"] not in seen:
                seen.add(it["title"])
                items.append(it)
    items.sort(key=lambda x: x.get("time") or "", reverse=True)
    return items[:40]


# ---------- accounts ----------
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32))
SECRET = SECRET_FILE.read_text().strip().encode()
SESSION_DAYS = 60
users = load_json(USERS_FILE, {})
fails: dict = {}  # key -> (count, first_fail_ts)


def ukey(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


def make_token(key: str) -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    body = f"{key.encode().hex()}.{exp}"
    return body + "." + hmac.new(SECRET, body.encode(), "sha256").hexdigest()


def read_token(tok: str | None) -> str | None:
    try:
        hexkey, exp, sig = tok.split(".")
        body = f"{hexkey}.{exp}"
        if not hmac.compare_digest(sig, hmac.new(SECRET, body.encode(), "sha256").hexdigest()):
            return None
        if int(exp) < time.time():
            return None
        key = bytes.fromhex(hexkey).decode()
        return key if key in users else None
    except Exception:
        return None


def current_user(folio_s: str | None = Cookie(None)) -> str:
    key = read_token(folio_s)
    if not key:
        raise HTTPException(401, "Sign in first")
    return key


def set_session(resp: Response, key: str):
    resp.set_cookie("folio_s", make_token(key), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")


def public(key):
    u = users[key]
    return {"name": u["name"], "holdings": u["holdings"], "watchlist": u["watchlist"], "joined": u.get("joined"),
            "baskets": u.get("baskets", []), "shareActivity": bool(u.get("shareActivity"))}


class Creds(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    pin: str = Field(pattern=r"^\d{4,8}$")


@app.post("/api/signup")
def signup(c: Creds, resp: Response):
    name = re.sub(r"\s+", " ", c.name.strip())
    if not re.fullmatch(r"[\w .'-]{1,24}", name):
        raise HTTPException(400, "Use letters, numbers and spaces for your name.")
    key = ukey(name)
    with lock:
        if key in users:
            raise HTTPException(409, "That name is taken. Sign in instead, or pick another name.")
        salt = secrets.token_hex(16)
        users[key] = {"name": name, "salt": salt, "pin": hash_pin(c.pin, salt), "holdings": [],
                      "watchlist": list(DEFAULT_WATCHLIST), "joined": int(time.time())}
        save_json(USERS_FILE, users)
    set_session(resp, key)
    return public(key)


@app.post("/api/login")
def login(c: Creds, resp: Response):
    key = ukey(c.name)
    n, t0 = fails.get(key, (0, 0))
    if n >= 5 and time.time() - t0 < 300:
        raise HTTPException(429, "Too many wrong PINs. Wait 5 minutes and try again.")
    u = users.get(key)
    if not u or not hmac.compare_digest(u["pin"], hash_pin(c.pin, u["salt"])):
        fails[key] = (n + 1 if time.time() - t0 < 300 else 1, t0 if n and time.time() - t0 < 300 else time.time())
        raise HTTPException(401, "That name and PIN don't match.")
    fails.pop(key, None)
    set_session(resp, key)
    return public(key)


@app.post("/api/logout")
def logout(resp: Response):
    resp.delete_cookie("folio_s")
    return {"ok": True}


@app.get("/api/me")
def me(key: str = Depends(current_user)):
    return public(key)


class Holding(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    shares: float = Field(gt=0)
    cost: float = Field(ge=0)
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    cur: str | None = Field(None, max_length=5)
    fx: dict[str, float] | None = None  # USD per unit of each currency when bought


class State(BaseModel):
    holdings: list[Holding] = Field(max_length=200)
    watchlist: list[str] = Field(max_length=100)


BASES = ["USD", "CAD", "EUR", "GBP"]
ACTIVITY_FILE = DATA / "activity.json"
activity = load_json(ACTIVITY_FILE, [])


def fx_on(currencies, date=None):
    """USD per unit of each currency, today or at the close on/before `date`."""
    out = {"USD": 1.0}
    need = sorted({c for c in currencies if c and c != "USD"})
    if not need:
        return out
    if not date:
        for c in need:
            r = fx_to_usd(c)
            if r:
                out[c] = r
        return out
    def f():
        d = pd.Timestamp(date)
        df = yf.download([f"{c}USD=X" for c in need], start=(d - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                         end=(d + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), progress=False, auto_adjust=True)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame(f"{need[0]}USD=X")
        res = {}
        for c in need:
            col = df.get(f"{c}USD=X")
            if col is not None and len(col.dropna()):
                res[c] = float(col.dropna().iloc[-1])
        return res
    try:
        out.update(cached(("fxon", tuple(need), date), 86400, f))
    except Exception:
        pass
    for c in need:  # fall back to today's rate
        if c not in out and fx_to_usd(c):
            out[c] = fx_to_usd(c)
    return out


def fill_fx(holdings):
    """Give each holding its currency and an FX snapshot from when it was bought (or added)."""
    for h in holdings:
        if not h.get("cur"):
            q = one_quote(h["symbol"])
            if q.get("currency"):
                h["cur"] = q["currency"]
        if not h.get("fx") and h.get("cur"):
            h["fx"] = fx_on([h["cur"], *BASES], h.get("date"))
    return holdings


def log_activity(key, old, new):
    u = users[key]
    if not u.get("shareActivity"):
        return
    o = {h["symbol"]: h["shares"] for h in old}
    n = {h["symbol"]: h["shares"] for h in new}
    vals = {}
    for h in new:
        q = one_quote(h["symbol"])
        if q.get("price") and q.get("usdRate"):
            vals[h["symbol"]] = q["price"] * h["shares"] * q["usdRate"]
    tot = sum(vals.values()) or 0
    w = lambda s: round(vals[s] / tot * 100, 1) if tot and s in vals else None
    now = int(time.time())
    for sym in n:
        if sym not in o:
            activity.append({"ts": now, "name": u["name"], "kind": "added", "symbol": sym, "weight": w(sym)})
        elif abs(n[sym] - o[sym]) > 1e-9:
            ch = (n[sym] / o[sym] - 1) * 100
            activity.append({"ts": now, "name": u["name"], "kind": "added to" if ch > 0 else "trimmed", "symbol": sym,
                             "change": round(ch, 1), "weight": w(sym)})
    for sym in o:
        if sym not in n:
            activity.append({"ts": now, "name": u["name"], "kind": "sold", "symbol": sym})
    del activity[:-300]
    save_json(ACTIVITY_FILE, activity)


@app.put("/api/state")
def put_state(s: State, key: str = Depends(current_user)):
    new = fill_fx([{**{k: v for k, v in h.model_dump().items() if v is not None}, "symbol": h.symbol.upper()} for h in s.holdings])
    with lock:
        old = users[key]["holdings"]
        users[key]["holdings"] = new
        users[key]["watchlist"] = [w.upper()[:20] for w in s.watchlist]
        log_activity(key, old, new)
        save_json(USERS_FILE, users)
    return public(key)


class Prefs(BaseModel):
    shareActivity: bool


@app.put("/api/prefs")
def put_prefs(p: Prefs, key: str = Depends(current_user)):
    with lock:
        users[key]["shareActivity"] = p.shareActivity
        save_json(USERS_FILE, users)
    return public(key)


@app.get("/api/activity")
def get_activity(key: str = Depends(current_user)):
    return list(reversed(activity[-40:]))


@app.get("/api/fx")
def fx_rates(key: str = Depends(current_user)):
    return fx_on(BASES)


def backfill_fx():
    changed = False
    for k, u in list(users.items()):
        if any(not h.get("fx") or not h.get("cur") for h in u["holdings"]):
            fill_fx(u["holdings"])
            changed = True
    if changed:
        with lock:
            save_json(USERS_FILE, users)


@app.get("/api/group")
def group(key: str = Depends(current_user)):
    return [public(k) for k in users]


# ---------- AI ----------
claude = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
STYLE = ("Plain language for a private investor, no hype, no investment advice, no preamble or closing line. "
         "Only use the data and headlines given; if they don't explain something, say so rather than guessing.")


def ask_claude(system: str, user: str, max_tokens: int = 2000) -> str:
    if not claude:
        raise HTTPException(503, "The AI isn't set up on this server yet (it needs an Anthropic API key).")
    try:
        r = claude.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=max_tokens,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system + " " + STYLE,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError:
        raise HTTPException(429, "The AI is busy. Try again in a minute.")
    except anthropic.AuthenticationError:
        raise HTTPException(503, "The server's Anthropic API key was rejected.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"The AI request failed ({e.status_code}).")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Couldn't reach the AI service.")
    if r.stop_reason == "refusal":
        raise HTTPException(502, "The AI declined this request.")
    return "".join(b.text for b in r.content if b.type == "text").strip()


def headlines_text(items, n=25):
    return "\n".join(f"- [{i.get('symbol', '')}] {i['title']} ({i.get('source', '')}, {i.get('time', '')}) — {i.get('summary', '')[:240]}"
                     for i in items[:n])


class SummaryReq(BaseModel):
    symbols: list[str]


@app.post("/api/summary")
def summary(req: SummaryReq, key: str = Depends(current_user)):
    syms = ",".join(sorted(set(s.upper() for s in req.symbols))[:20])
    def f():
        items = news(syms)[:25]
        if not items:
            return {"text": "No recent headlines for these holdings.", "ts": time.time()}
        text = ask_claude("You brief an investor on news about their holdings. Write 4-6 short bullet points, "
                          "each starting with '- ' and naming the ticker(s) involved and why it matters.",
                          "Headlines:\n" + headlines_text(items))
        return {"text": text, "ts": time.time()}
    return cached(("sum", syms), 1800, f)


# ---------- market intel ----------
INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^RUT", "Russell 2000"),
           ("^VIX", "VIX (fear gauge)"), ("^TNX", "10-yr Treasury yield"), ("CL=F", "Oil (WTI)"),
           ("GC=F", "Gold"), ("DX-Y.NYB", "US dollar index"), ("BTC-USD", "Bitcoin")]
SECTORS = [("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Consumer discretionary"),
           ("XLP", "Consumer staples"), ("XLF", "Financials"), ("XLV", "Health care"), ("XLI", "Industrials"),
           ("XLE", "Energy"), ("XLB", "Materials"), ("XLU", "Utilities"), ("XLRE", "Real estate")]
SECTOR_ETF = {"Technology": "XLK", "Communication Services": "XLC", "Consumer Cyclical": "XLY",
              "Consumer Defensive": "XLP", "Financial Services": "XLF", "Healthcare": "XLV",
              "Industrials": "XLI", "Energy": "XLE", "Basic Materials": "XLB", "Utilities": "XLU",
              "Real Estate": "XLRE"}


def daily_closes(symbols):
    def f():
        d = yf.download(list(symbols), period="3mo", interval="1d", progress=False, auto_adjust=True)["Close"]
        out = {}
        for sym in symbols:
            if sym in d:
                col = d[sym].dropna()
                out[sym] = [(ts.strftime("%Y-%m-%d"), float(v)) for ts, v in col.items()]
        return out
    return cached(("closes", tuple(symbols)), 300, f)


def moves(series):
    """Last-session and 5-session % change from a list of (date, close)."""
    if len(series) < 2:
        return None
    last = series[-1][1]
    wk = series[-6][1] if len(series) >= 6 else series[0][1]
    return {"price": last, "day": (last / series[-2][1] - 1) * 100, "week": (last / wk - 1) * 100,
            "date": series[-1][0], "weekFrom": (series[-6] if len(series) >= 6 else series[0])[0],
            "spark": [round(v, 4) for _, v in series[-22:]]}


def screen(kind):
    def f():
        res = yf.screen(kind, count=8)
        return [{"symbol": q["symbol"], "name": q.get("shortName") or q.get("longName") or q["symbol"],
                 "price": q.get("regularMarketPrice"), "pct": q.get("regularMarketChangePercent"),
                 "volume": q.get("regularMarketVolume")} for q in res.get("quotes", [])]
    try:
        return cached(("screen", kind), 300, f)
    except Exception:
        return []


def market_news():
    def f():
        out, seen = [], set()
        for q in ("stock market today", "stocks week ahead", "Wall Street"):
            try:
                for n in yf.Search(q, news_count=10).news:
                    t = n.get("title")
                    if t and t not in seen:
                        seen.add(t)
                        out.append({"title": t, "url": n.get("link"), "source": n.get("publisher", ""),
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(n.get("providerPublishTime", 0))),
                                    "symbol": ",".join((n.get("relatedTickers") or [])[:3]), "summary": ""})
            except Exception:
                pass
        for it in news_for("^GSPC") + news_for("SPY"):
            if it["title"] not in seen:
                seen.add(it["title"])
                out.append({**it, "symbol": ""})
        out = [o for o in out if o.get("url")]
        out.sort(key=lambda x: x["time"] or "", reverse=True)
        return out[:30]
    return cached(("mnews",), 600, f)


@app.get("/api/market")
def market(key: str = Depends(current_user)):
    def f():
        syms = tuple(s for s, _ in INDEXES + SECTORS)
        closes = daily_closes(syms)
        def row(sym, label):
            m = moves(closes.get(sym, []))
            return {"symbol": sym, "label": label, **m} if m else {"symbol": sym, "label": label}
        return {
            "indexes": [row(s, l) for s, l in INDEXES],
            "sectors": [row(s, l) for s, l in SECTORS],
            "movers": {k: screen(k) for k in ("day_gainers", "day_losers", "most_actives")},
            "news": market_news(),
            "ts": time.time(),
        }
    return cached(("market",), 120, f)


class BriefReq(BaseModel):
    period: str = Field(pattern="^(day|week)$")


@app.post("/api/market/brief")
def market_brief(req: BriefReq, key: str = Depends(current_user)):
    def f():
        m = market(key)
        k = req.period
        fmt = lambda r: f"{r['label']}: {r['price']:.2f} ({r[k]:+.2f}%)" if "price" in r else f"{r['label']}: n/a"
        data = ["Markets (" + ("last session" if k == "day" else "past 5 sessions") + "):"]
        data += [fmt(r) for r in m["indexes"]]
        data += ["", "Sectors:"] + [fmt(r) for r in sorted(m["sectors"], key=lambda r: -r.get(k, 0))]
        if k == "day":
            for name, lst in m["movers"].items():
                data += ["", name.replace("_", " ").title() + ":"] + [f"{x['symbol']} {x['name']} {x['pct']:+.1f}%" for x in lst[:6] if x.get("pct") is not None]
        asof = next((r["date"] for r in m["indexes"] if r.get("date")), "")
        text = ask_claude(
            f"You write a short market briefing for {'the latest trading day' if k == 'day' else 'the past trading week'} "
            f"(data as of {asof}). Start with one sentence on the overall picture, then 4-6 bullet points "
            "(each starting with '- ') covering the main drivers, which sectors led and lagged, rates/oil/dollar/crypto "
            "where notable, and anything coming up that headlines mention. Tie moves to headlines when they explain them.",
            "\n".join(data) + "\n\nHeadlines:\n" + headlines_text(m["news"], 30), 2500)
        return {"text": text, "ts": time.time(), "asof": asof}
    return cached(("brief", req.period), 1800, f)


# ---------- single-stock intel ----------
def fnum(v):
    try:
        return float(v)
    except Exception:
        return None


@app.get("/api/stock")
def stock(symbol: str, key: str = Depends(current_user)):
    sym = symbol.strip().upper()[:20]
    def f():
        t = yf.Ticker(sym)
        try:
            info = t.info or {}
        except Exception:
            info = {}
        q = one_quote(sym)
        sector = info.get("sector")
        etf = SECTOR_ETF.get(sector)
        try:
            h = t.history(period="1y", interval="1d")["Close"].dropna()
            series = [(ts.strftime("%Y-%m-%d"), float(v)) for ts, v in h.items()]
        except Exception:
            series = []
        if q.get("error") and not series:
            raise HTTPException(404, f"Couldn't find a stock called {sym}. Try searching by company name.")
        def chg(n):
            return (series[-1][1] / series[-1 - n][1] - 1) * 100 if len(series) > n else None
        ytd = None
        yr = time.strftime("%Y")
        prior = [v for d, v in series if d < yr]
        if prior and series:
            ytd = (series[-1][1] / prior[-1] - 1) * 100
        bench = daily_closes(tuple(x for x in ("^GSPC", etf) if x))
        cal = {}
        try:
            c = t.calendar or {}
            ed = c.get("Earnings Date") or []
            cal = {"earnings": str(ed[0]) if ed else None, "epsEst": fnum(c.get("Earnings Average"))}
        except Exception:
            pass
        vol, avg = fnum(info.get("volume") or info.get("regularMarketVolume")), fnum(info.get("averageVolume"))
        return {
            "symbol": sym, "quote": q,
            "name": info.get("longName") or info.get("shortName") or q.get("name") or sym,
            "sector": sector, "industry": info.get("industry"), "type": info.get("quoteType"),
            "about": (info.get("longBusinessSummary") or "")[:600],
            "website": info.get("website"),
            "changes": {"1D": q.get("pct"), "1W": chg(5), "1M": chg(21), "3M": chg(63), "YTD": ytd, "1Y": chg(len(series) - 1) if series else None},
            "vs": {"market": moves(bench.get("^GSPC", [])), "sector": moves(bench.get(etf, [])) if etf else None,
                   "sectorEtf": etf, "sectorName": sector},
            "stats": {
                "marketCap": fnum(info.get("marketCap")), "pe": fnum(info.get("trailingPE")), "fpe": fnum(info.get("forwardPE")),
                "low52": fnum(info.get("fiftyTwoWeekLow")), "high52": fnum(info.get("fiftyTwoWeekHigh")),
                "volume": vol, "avgVolume": avg, "volRatio": vol / avg if vol and avg else None,
                "target": fnum(info.get("targetMeanPrice")), "rating": info.get("recommendationKey"),
                "analysts": info.get("numberOfAnalystOpinions"), "dividendYield": fnum(info.get("dividendYield")),
                "beta": fnum(info.get("beta")),
                "eps": fnum(info.get("trailingEps")),
                "feps": fnum(info.get("forwardEps")) or (q["price"] / fnum(info.get("forwardPE")) if q.get("price") and fnum(info.get("forwardPE")) else None),
                "capCurrency": (info.get("currency") or q.get("currency") or "USD").replace("GBp", "GBP"),
                "marketCapUsd": fnum(info.get("marketCap")) * (fx_to_usd((info.get("currency") or "USD").replace("GBp", "GBP")) or 0) if fnum(info.get("marketCap")) else None,
            },
            "calendar": cal,
            "news": news_for(sym),
            "ts": time.time(),
        }
    return cached(("stock", sym), 300, f)


class WhyReq(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)


@app.post("/api/stock/why")
def stock_why(req: WhyReq, key: str = Depends(current_user)):
    sym = req.symbol.strip().upper()
    def f():
        d = stock(sym, key)
        c, st, vs = d["changes"], d["stats"], d["vs"]
        pct = lambda v: "n/a" if v is None else f"{v:+.2f}%"
        lines = [f"{d['name']} ({sym}), {d.get('sector') or ''} / {d.get('industry') or ''}",
                 f"Price {d['quote'].get('price')} {d['quote'].get('currency', '')}",
                 "Moves: " + ", ".join(f"{k} {pct(v)}" for k, v in c.items())]
        if vs.get("market"):
            lines.append(f"S&P 500: day {pct(vs['market']['day'])}, week {pct(vs['market']['week'])}")
        if vs.get("sector"):
            lines.append(f"Sector ETF {vs['sectorEtf']}: day {pct(vs['sector']['day'])}, week {pct(vs['sector']['week'])}")
        if st.get("volRatio"):
            lines.append(f"Volume today is {st['volRatio']:.1f}x the average")
        if st.get("low52") and st.get("high52"):
            lines.append(f"52-week range {st['low52']:.2f} - {st['high52']:.2f}")
        if st.get("target"):
            lines.append(f"Analyst mean target {st['target']:.2f}, rating {st.get('rating')}")
        if d["calendar"].get("earnings"):
            lines.append(f"Next earnings date: {d['calendar']['earnings']}")
        text = ask_claude(
            "Explain why this stock is moving. Start with a one-sentence answer. Then 3-5 bullet points "
            "(each starting with '- '): the main news driver(s), whether the move is stock-specific or follows the "
            "market/sector (compare the numbers), and anything coming up (earnings etc.). Say clearly if the headlines "
            "don't explain the move.",
            "\n".join(lines) + "\n\nRecent headlines:\n" + (headlines_text(d["news"], 15) or "(none)"), 1500)
        return {"text": text, "ts": time.time()}
    return cached(("why", sym), 1200, f)


# ---------- sentiment ----------

INDUSTRIES = [
    {"key": "tech", "name": "Technology", "etf": "XLK", "members": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE"]},
    {"key": "semis", "name": "Semiconductors", "etf": "SMH", "members": ["NVDA", "TSM", "AVGO", "AMD", "QCOM", "ASML", "INTC"]},
    {"key": "memory", "name": "Memory & storage", "etf": None, "members": ["MU", "WDC", "STX", "SNDK"]},
    {"key": "neocloud", "name": "Neoclouds", "etf": None, "members": ["CRWV", "NBIS", "IREN", "APLD"]},
    {"key": "finance", "name": "Financials", "etf": "XLF", "members": ["JPM", "BAC", "WFC", "GS", "MS", "V", "MA"]},
    {"key": "consumer", "name": "Consumer", "etf": "XLY", "members": ["AMZN", "TSLA", "HD", "MCD", "NKE", "COST", "WMT"]},
]
MOOD = [(25, "Extreme fear"), (45, "Fear"), (55, "Neutral"), (75, "Greed"), (101, "Extreme greed")]


def mood(v):
    if v is None:
        return None
    return next(l for t, l in MOOD if v < t)


def closes_df(symbols, period="3y"):
    def f():
        d = yf.download(sorted(set(symbols)), period=period, interval="1d", progress=False, auto_adjust=True)["Close"]
        return d.ffill(limit=3)
    return cached(("df", tuple(sorted(set(symbols))), period), 900, f)


def rsi(s, n=14):
    d = s.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n).mean()
    return 100 - 100 / (1 + up / dn)


def pct_rank(s, win=252):
    """Where today's value sits within the trailing year, 0-100."""
    return s.rolling(win, min_periods=60).apply(lambda x: (x[:-1] < x[-1]).mean() * 100 + (x[:-1] == x[-1]).mean() * 50, raw=True)


def composite(parts: dict):
    df = pd.DataFrame({k: pct_rank(v) for k, v in parts.items()}).dropna(how="all")
    score = df.mean(axis=1, skipna=True)
    return score, df


def summarize(score, df, labels):
    score = score.dropna()
    if score.empty:
        return None
    hist = score.iloc[-130:]
    last = float(score.iloc[-1])
    ago = lambda n: float(score.iloc[-1 - n]) if len(score) > n else None
    comps = [{"key": k, **labels[k], "score": None if pd.isna(df[k].iloc[-1]) else round(float(df[k].iloc[-1]))} for k in labels if k in df]
    return {"score": round(last), "mood": mood(last), "prevClose": ago(1), "weekAgo": ago(5), "monthAgo": ago(21), "yearAgo": ago(252),
            "components": comps, "history": [[d.strftime("%Y-%m-%d"), round(float(v), 1)] for d, v in hist.items()]}


MARKET_LABELS = {
    "momentum": {"name": "Market momentum", "desc": "S&P 500 vs its 125-day average"},
    "strength": {"name": "Breadth", "desc": "Equal-weight S&P vs the regular S&P (are most stocks joining in?)"},
    "volatility": {"name": "Volatility", "desc": "VIX vs its 50-day average (high VIX = fear)"},
    "safehaven": {"name": "Safe-haven demand", "desc": "Stocks vs Treasury bonds over 20 days"},
    "junk": {"name": "Junk bond demand", "desc": "High-yield vs safe bonds over 20 days"},
    "rsi": {"name": "Overbought / oversold", "desc": "S&P 500 14-day RSI"},
}
GROUP_LABELS = {
    "trend50": {"name": "Short-term trend", "desc": "Price vs 50-day average"},
    "trend200": {"name": "Long-term trend", "desc": "Price vs 200-day average"},
    "relative": {"name": "Vs the market", "desc": "20-day return vs the S&P 500"},
    "rsi": {"name": "Overbought / oversold", "desc": "14-day RSI"},
    "calm": {"name": "Calm vs turbulence", "desc": "20-day volatility (lower = more confident)"},
}


def market_fng():
    def f():
        d = closes_df(["SPY", "^VIX", "TLT", "HYG", "IEF", "RSP"])
        spy, vix = d["SPY"].dropna(), d["^VIX"].dropna()
        parts = {
            "momentum": spy / spy.rolling(125).mean() - 1,
            "strength": (d["RSP"] / d["SPY"]) / (d["RSP"] / d["SPY"]).rolling(50).mean() - 1,
            "volatility": -(vix / vix.rolling(50).mean() - 1),
            "safehaven": spy.pct_change(20) - d["TLT"].pct_change(20),
            "junk": d["HYG"].pct_change(20) - d["IEF"].pct_change(20),
            "rsi": rsi(spy),
        }
        score, df = composite(parts)
        out = summarize(score, df, MARKET_LABELS)
        out["asof"] = spy.index[-1].strftime("%Y-%m-%d")
        return out
    return cached(("fng",), 900, f)


def series_score(px, spy):
    px = px.dropna()
    if len(px) < 80:
        return None, None
    ret = px.pct_change()
    parts = {
        "trend50": px / px.rolling(50).mean() - 1,
        "trend200": px / px.rolling(200, min_periods=100).mean() - 1,
        "relative": px.pct_change(20) - spy.reindex(px.index).pct_change(20),
        "rsi": rsi(px),
        "calm": -ret.rolling(20).std(),
    }
    return composite(parts)


def basket(d, members):
    rets = d[[m for m in members if m in d]].pct_change(fill_method=None)
    idx = (1 + rets.mean(axis=1, skipna=True).fillna(0)).cumprod()
    first = rets.dropna(how="all").index
    return idx.loc[first[0]:] if len(first) else idx


def analyst(sym):
    def f():
        t = yf.Ticker(sym)
        try:
            info = t.info or {}
        except Exception:
            info = {}
        counts = None
        try:
            r = t.recommendations
            if r is not None and not r.empty:
                row = r.iloc[0]
                counts = {k: int(row.get(k, 0)) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        except Exception:
            pass
        price = fnum(info.get("currentPrice") or info.get("regularMarketPrice"))
        target = fnum(info.get("targetMeanPrice"))
        return {"symbol": sym, "name": info.get("shortName") or sym, "mean": fnum(info.get("recommendationMean")),
                "key": info.get("recommendationKey"), "target": target, "price": price,
                "upside": (target / price - 1) * 100 if target and price else None,
                "analysts": info.get("numberOfAnalystOpinions"), "counts": counts,
                "fpe": fnum(info.get("forwardPE")), "pe": fnum(info.get("trailingPE"))}
    return cached(("analyst", sym), 6 * 3600, f)


def rating_label(mean):
    if mean is None:
        return None
    return "Strong buy" if mean < 1.5 else "Buy" if mean < 2.3 else "Hold" if mean < 3.2 else "Sell"


def industry_card(ind, d, spy, an):
    members = [m for m in ind["members"] if m in d]
    px = d[ind["etf"]].dropna() if ind.get("etf") else basket(d, members)
    score, df = series_score(px, spy)
    summ = summarize(score, df, GROUP_LABELS) if score is not None else None
    mem = []
    raw = daily_closes(tuple(ind["members"]))  # un-filled, so markets on different calendars don't show fake 0% days
    for m in ind["members"]:
        a = an.get(m) or {"symbol": m}
        col = [v for _, v in raw.get(m, [])]
        mem.append({**a, "day": (col[-1] / col[-2] - 1) * 100 if len(col) > 1 else None,
                    "month": (col[-1] / col[-22] - 1) * 100 if len(col) > 22 else None,
                    "rating": rating_label(a.get("mean"))})
    means = [x["mean"] for x in mem if x.get("mean")]
    ups = [x["upside"] for x in mem if x.get("upside") is not None]
    counts = {k: sum((x.get("counts") or {}).get(k, 0) for x in mem) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    return {
        "key": ind["key"], "name": ind["name"], "etf": ind.get("etf"),
        "basis": f"{ind['etf']} fund" if ind.get("etf") else "equal-weight basket of " + ", ".join(ind["members"]),
        "sentiment": summ,
        "week": float(px.iloc[-1] / px.iloc[-6] - 1) * 100 if len(px) > 6 else None,
        "month": float(px.iloc[-1] / px.iloc[-22] - 1) * 100 if len(px) > 22 else None,
        "consensus": {"mean": float(np.mean(means)) if means else None, "rating": rating_label(float(np.mean(means))) if means else None,
                      "upside": float(np.median(ups)) if ups else None, "counts": counts},
        "members": mem,
    }


def industry_sentiment():
    def f():
        syms = {"SPY"} | {i["etf"] for i in INDUSTRIES if i["etf"]} | {m for i in INDUSTRIES for m in i["members"]}
        d = closes_df(list(syms))
        spy = d["SPY"].dropna()
        allm = sorted({m for i in INDUSTRIES for m in i["members"]})
        an = dict(zip(allm, pool.map(analyst, allm)))
        return [industry_card(ind, d, spy, an) for ind in INDUSTRIES]
    return cached(("industries",), 1800, f)


@app.get("/api/sentiment")
def sentiment(key: str = Depends(current_user)):
    return {"market": market_fng(), "industries": industry_sentiment(), "ts": time.time()}


VERDICT_FORMAT = (
    "Reply in exactly this line format, nothing else:\n"
    "SIGNAL: <one of: Leans positive | Mixed | Leans cautious>\n"
    "SUMMARY: <one or two sentences>\n"
    "BULL: <point> (2-3 lines)\n"
    "BEAR: <point> (2-3 lines)\n"
    "TIMING: <one line on whether the current price/mood looks stretched, washed-out or in between>\n"
    "This is general information summarizing signals and consensus, not personal advice; don't tell the reader what to do with their money."
)


@app.post("/api/sentiment/brief")
def sentiment_brief(key: str = Depends(current_user)):
    def f():
        s = sentiment(key)
        m = s["market"]
        lines = [f"Market fear & greed (0-100, our composite): {m['score']} ({m['mood']}); a week ago {m['weekAgo']:.0f}, a month ago {m['monthAgo']:.0f}"]
        lines += [f"  {c['name']}: {c['score']}" for c in m["components"]]
        for i in s["industries"]:
            se, co = i["sentiment"] or {}, i["consensus"]
            lines.append(f"{i['name']}: sentiment {se.get('score')} ({se.get('mood')}), 1-month {i['month']:+.1f}%, "
                         f"analyst consensus {co['rating']} (mean {co['mean'] or 0:.2f} on 1-5 scale), median target upside {co['upside'] or 0:+.0f}%")
        news = market_news()
        text = ask_claude(
            "You assess whether the overall US stock market looks like a good time to buy, based on sentiment and consensus "
            "data. Be balanced: contrarian investors treat extreme fear as opportunity and extreme greed as a warning. "
            "Mention which industries look stretched or washed-out. " + VERDICT_FORMAT,
            "\n".join(lines) + "\n\nHeadlines:\n" + headlines_text(news, 15), 1500)
        return {"text": text, "ts": time.time()}
    return cached(("sbrief",), 1800, f)


def stock_sentiment(sym):
    def f():
        d = closes_df([sym, "SPY"])
        if sym not in d:
            return None
        score, df = series_score(d[sym], d["SPY"].dropna())
        return summarize(score, df, GROUP_LABELS) if score is not None else None
    try:
        return cached(("ssent", sym), 900, f)
    except Exception:
        return None


@app.get("/api/stock/sentiment")
def stock_sent(symbol: str, key: str = Depends(current_user)):
    sym = symbol.strip().upper()[:20]
    a = analyst(sym)
    return {"symbol": sym, "sentiment": stock_sentiment(sym), "analyst": {**a, "rating": rating_label(a.get("mean"))}}


ACTION = {"up": "Upgrade", "down": "Downgrade", "main": "Maintains", "init": "Initiates", "reit": "Reiterates"}


@app.get("/api/stock/targets")
def stock_targets(symbol: str, key: str = Depends(current_user)):
    """Individual analyst rating / price-target actions, at least the past 12 months."""
    sym = symbol.strip().upper()[:20]
    def f():
        try:
            u = yf.Ticker(sym).upgrades_downgrades
        except Exception:
            u = None
        if u is None or u.empty:
            return {"symbol": sym, "rows": [], "since": None}
        u = u.sort_index(ascending=False)
        cut = pd.Timestamp.now() - pd.Timedelta(days=366)
        recent = u[u.index >= cut]
        if len(recent) < 10:  # thinly covered stock: fall back to the latest 10 actions
            recent = u.head(max(10, len(recent)))
        rows = []
        for ts, r in recent.iterrows():
            cur, prior = fnum(r.get("currentPriceTarget")), fnum(r.get("priorPriceTarget"))
            cur = cur or None
            prior = prior or None
            rows.append({
                "date": ts.strftime("%Y-%m-%d"),
                "targetFor": (ts + pd.DateOffset(years=1)).strftime("%Y-%m-%d") if cur else None,
                "firm": r.get("Firm") or "",
                "action": ACTION.get(r.get("Action"), r.get("Action") or ""),
                "from": r.get("FromGrade") or "", "to": r.get("ToGrade") or "",
                "ptAction": r.get("priceTargetAction") or "",
                "target": cur, "prior": prior,
            })
        return {"symbol": sym, "rows": rows, "since": rows[-1]["date"] if rows else None}
    return cached(("targets", sym), 3600, f)


@app.post("/api/stock/verdict")
def stock_verdict(req: WhyReq, key: str = Depends(current_user)):
    sym = req.symbol.strip().upper()
    def f():
        d = stock(sym, key)
        ss = stock_sent(sym, key)
        m = market_fng()
        se, a, st, c = ss["sentiment"] or {}, ss["analyst"], d["stats"], d["changes"]
        pct = lambda v: "n/a" if v is None else f"{v:+.1f}%"
        lines = [f"{d['name']} ({sym}), {d.get('sector') or ''} / {d.get('industry') or ''}, price {d['quote'].get('price')}",
                 "Returns: " + ", ".join(f"{k} {pct(v)}" for k, v in c.items()),
                 f"Stock sentiment score (0-100, trend/RSI/relative strength/volatility vs its past year): {se.get('score')} ({se.get('mood')})"]
        lines += [f"  {x['name']}: {x['score']}" for x in se.get("components", [])]
        if a.get("mean"):
            lines.append(f"Analysts: {a['rating']} (mean {a['mean']:.2f} on 1=strong buy..5=sell), {a.get('analysts')} analysts, counts {a.get('counts')}, "
                         f"mean target {a.get('target')} ({pct(a.get('upside'))} vs price)")
        lines.append(f"Valuation: P/E {st.get('pe')}, forward P/E {st.get('fpe')}; 52-week range {st.get('low52')} - {st.get('high52')}")
        if d["calendar"].get("earnings"):
            lines.append(f"Next earnings: {d['calendar']['earnings']}")
        lines.append(f"Overall market fear & greed: {m['score']} ({m['mood']})")
        text = ask_claude(
            "You assess whether this stock looks attractive to buy at the current price, weighing analyst consensus, "
            "valuation, momentum/sentiment, upcoming events and news. Be balanced and concrete with numbers. " + VERDICT_FORMAT,
            "\n".join(lines) + "\n\nRecent headlines:\n" + (headlines_text(d["news"], 12) or "(none)"), 1500)
        return {"text": text, "ts": time.time()}
    return cached(("verdict", sym), 1800, f)


# ---------- portfolio extras ----------
def member_key(member):
    k = ukey(member or "")
    if k not in users:
        raise HTTPException(404, "No such member")
    return k


@app.get("/api/moods")
def moods(symbols: str, key: str = Depends(current_user)):
    syms = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})[:60]
    def one(sym):
        se = stock_sentiment(sym)
        return sym, se["score"] if se else None
    return dict(pool.map(one, syms))


def raw_currency(sym):
    def f():
        return yf.Ticker(sym).fast_info.currency or "USD"
    try:
        return cached(("rawcur", sym), 86400, f)
    except Exception:
        return "USD"


PRANGES = {"1D": ("1d", "5m"), "1M": ("1mo", "1d"), "6M": ("6mo", "1d"), "YTD": ("ytd", "1d"), "1Y": ("1y", "1d")}


def value_frame(holdings, period, interval, extra=()):
    """Per-holding USD value over time for today's holdings (+ extra columns like benchmarks)."""
    syms = sorted({h["symbol"] for h in holdings})
    curs = {s: raw_currency(s) for s in syms}
    fxs = sorted({c.replace("GBp", "GBP") for c in curs.values() if c not in ("USD",)})
    tick = syms + [f"{c}USD=X" for c in fxs] + list(extra)
    df = yf.download(tick, period=period, interval=interval, progress=False, auto_adjust=True)["Close"]
    if isinstance(df, pd.Series):
        df = df.to_frame(tick[0])
    df = df.sort_index().ffill()
    vals = pd.DataFrame(index=df.index)
    for h in holdings:
        s = h["symbol"]
        if s not in df:
            continue
        c = curs[s]
        px = df[s] / (100 if c == "GBp" else 1)
        c = c.replace("GBp", "GBP")
        if c == "USD":
            rate = 1.0
        elif interval == "1d" and f"{c}USD=X" in df:
            rate = df[f"{c}USD=X"].bfill()
        else:
            rate = fx_to_usd(c) or 0
        v = px * h["shares"] * rate
        vals[s] = vals[s] + v if s in vals else v
    return vals, df


@app.get("/api/portfolio/history")
def portfolio_history(member: str, range: str = "6M", bench: str = "^GSPC", key: str = Depends(current_user)):
    if range not in PRANGES:
        raise HTTPException(400, "bad range")
    if bench not in ("^GSPC", "^IXIC", "none"):
        raise HTTPException(400, "bad benchmark")
    k = member_key(member)
    holdings = users[k]["holdings"]
    if not holdings:
        return {"points": [], "bench": None}
    sig = tuple(sorted((h["symbol"], h["shares"]) for h in holdings))
    def f():
        period, interval = PRANGES[range]
        b = [] if bench == "none" else [bench]
        vals, df = value_frame(holdings, period, interval, b)
        vals = vals.bfill() if range == "1D" else vals
        tot = vals.dropna().sum(axis=1)
        if range == "1D":  # keep the latest trading day only
            last_day = tot.index[-1].normalize() if len(tot) else None
            tot = tot[tot.index.normalize() == last_day] if last_day is not None else tot
        tot = tot[tot > 0]
        if len(tot) < 2:
            return {"points": [], "bench": None}
        out = {"points": [[int(t.timestamp() * 1000), round(float(v), 2)] for t, v in tot.items()],
               "ret": float(tot.iloc[-1] / tot.iloc[0] - 1) * 100, "bench": None, "benchName": None}
        if b and bench in df:
            bs = df[bench].reindex(tot.index).ffill().bfill()
            if bs.notna().all() and bs.iloc[0]:
                scaled = bs / bs.iloc[0] * tot.iloc[0]
                out["bench"] = [[int(t.timestamp() * 1000), round(float(v), 2)] for t, v in scaled.items()]
                out["benchRet"] = float(bs.iloc[-1] / bs.iloc[0] - 1) * 100
                out["benchName"] = "S&P 500" if bench == "^GSPC" else "Nasdaq"
        return out
    return cached(("phist", sig, range, bench), 120 if range == "1D" else 900, f)


@app.get("/api/group/returns")
def group_returns(key: str = Depends(current_user)):
    snap = {k: tuple(sorted((h["symbol"], h["shares"]) for h in u["holdings"])) for k, u in users.items()}
    def f():
        allh = [h for u in users.values() for h in u["holdings"]]
        if not allh:
            return {}
        out = {}
        for k, u in users.items():
            if not u["holdings"]:
                continue
            try:
                vals, _ = value_frame(u["holdings"], "3mo", "1d")
                tot = vals.dropna().sum(axis=1)
                tot = tot[tot > 0]
                last = tot.index[-1]
                r = {}
                for lab, days in (("1W", 7), ("1M", 30)):
                    then = tot.asof(last - pd.Timedelta(days=days))
                    r[lab] = float(tot.iloc[-1] / then - 1) * 100 if then == then and then else None
                out[u["name"]] = r
            except Exception:
                pass
        return out
    return cached(("greturns", tuple(sorted(snap.items()))), 900, f)


class Basket(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    members: list[str] = Field(min_length=2, max_length=10)


class Baskets(BaseModel):
    baskets: list[Basket] = Field(max_length=2)


@app.put("/api/baskets")
def put_baskets(b: Baskets, key: str = Depends(current_user)):
    clean = []
    for x in b.baskets:
        mem = list(dict.fromkeys(m.strip().upper()[:20] for m in x.members if m.strip()))
        if len(mem) < 2:
            raise HTTPException(400, f"“{x.name}” needs at least 2 tickers.")
        bad = [m for m in mem if one_quote(m).get("error")]
        if bad:
            raise HTTPException(400, "Couldn't find: " + ", ".join(bad) + ". Check the tickers (use the search box to look them up).")
        clean.append({"name": x.name.strip(), "members": mem})
    with lock:
        users[key]["baskets"] = clean
        save_json(USERS_FILE, users)
    return public(key)


@app.get("/api/sentiment/custom")
def custom_sentiment(key: str = Depends(current_user)):
    bs = users[key].get("baskets", [])
    if not bs:
        return []
    def f():
        mem = sorted({m for b in bs for m in b["members"]})
        d = closes_df(mem + ["SPY"])
        spy = d["SPY"].dropna()
        an = dict(zip(mem, pool.map(analyst, mem)))
        return [industry_card({"key": f"custom{i}", "name": b["name"], "etf": None, "members": b["members"]}, d, spy, an)
                for i, b in enumerate(bs)]
    return cached(("custom", json.dumps(bs, sort_keys=True)), 1800, f)


# ---------- stock extras ----------
@app.get("/api/stock/events")
def stock_events(symbol: str, key: str = Depends(current_user)):
    sym = symbol.strip().upper()[:20]
    def f():
        t = yf.Ticker(sym)
        earn, divs = [], []
        try:
            e = t.get_earnings_dates(limit=24)
            if e is not None:
                for ts, r in e.iterrows():
                    earn.append({"date": ts.strftime("%Y-%m-%d"), "est": fnum(r.get("EPS Estimate")),
                                 "eps": fnum(r.get("Reported EPS")), "surprise": fnum(r.get("Surprise(%)"))})
        except Exception:
            pass
        try:
            dv = t.dividends
            cut = pd.Timestamp.now(tz=dv.index.tz) - pd.Timedelta(days=5 * 366) if len(dv) else None
            for ts, v in (dv[dv.index >= cut].items() if cut is not None else []):
                divs.append({"date": ts.strftime("%Y-%m-%d"), "amount": float(v)})
        except Exception:
            pass
        clean = lambda xs: [{k: (None if isinstance(v, float) and v != v else v) for k, v in x.items()} for x in xs]
        return {"earnings": clean(earn), "dividends": divs}
    return cached(("events", sym), 6 * 3600, f)


PEER_HINTS = {"000660.KS": ["MU", "005930.KS", "NVDA", "TSM"], "005930.KS": ["000660.KS", "MU", "TSM", "AAPL"],
              "TSM": ["NVDA", "AVGO", "ASML", "INTC"], "SKHY": ["MU", "005930.KS", "SNDK", "TSM"]}
PEER_ORDER = ["memory", "neocloud", "semis", "finance", "consumer", "tech"]


def peers_for(sym):
    if sym in PEER_HINTS:
        return PEER_HINTS[sym]
    byk = {i["key"]: i for i in INDUSTRIES}
    for k in PEER_ORDER:
        if sym in byk[k]["members"]:
            return [m for m in byk[k]["members"] if m != sym][:4]
    def f():
        ik = (yf.Ticker(sym).info or {}).get("industryKey")
        if not ik:
            return []
        tc = yf.Industry(ik).top_companies
        return [s for s in (list(tc.index) if tc is not None else []) if s != sym][:4]
    try:
        return cached(("peers", sym), 86400, f)
    except Exception:
        return []


@app.get("/api/stock/peers")
def stock_peers(symbol: str, key: str = Depends(current_user)):
    sym = symbol.strip().upper()[:20]
    def f():
        ps = peers_for(sym)
        allp = [sym] + ps
        d = closes_df(allp + ["SPY"])
        spy = d["SPY"].dropna()
        an = dict(zip(allp, pool.map(analyst, allp)))
        raw = daily_closes(tuple(allp))
        rows = []
        for s in allp:
            if s not in d:
                continue
            sc, df = series_score(d[s].dropna(), spy)
            col = pd.Series([v for _, v in raw.get(s, [])], dtype=float)
            summ = summarize(sc, df, GROUP_LABELS) if sc is not None else None
            a = an.get(s) or {}
            rows.append({"symbol": s, "name": a.get("name") or s, "self": s == sym,
                         "day": float(col.iloc[-1] / col.iloc[-2] - 1) * 100 if len(col) > 1 else None,
                         "month": float(col.iloc[-1] / col.iloc[-22] - 1) * 100 if len(col) > 22 else None,
                         "fpe": a.get("fpe"), "mood": summ["score"] if summ else None,
                         "rating": rating_label(a.get("mean")), "upside": a.get("upside")})
        return {"symbol": sym, "peers": rows}
    try:
        return cached(("speers", sym), 1800, f)
    except Exception:
        return {"symbol": sym, "peers": []}


# ---------- markets extras ----------
@app.get("/api/sector")
def sector_detail(symbol: str, key: str = Depends(current_user)):
    sym = symbol.upper()
    names = dict(SECTORS)
    if sym not in names:
        raise HTTPException(404, "Unknown sector fund")
    def f():
        th = yf.Ticker(sym).funds_data.top_holdings
        hold = [(s, str(r.get("Name") or s), float(r.get("Holding Percent") or 0) * 100) for s, r in th.head(10).iterrows()]
        closes = daily_closes(tuple(s for s, _, _ in hold))
        rows = []
        for s, n, w in hold:
            m = moves(closes.get(s, []))
            rows.append({"symbol": s, "name": n, "weight": w, **({k: m[k] for k in ("price", "day", "week")} if m else {})})
        return {"symbol": sym, "name": names[sym], "holdings": rows}
    return cached(("sector", sym), 1800, f)


# FOMC decision days (second day of each meeting). 2027 dates are the Fed's tentative schedule.
FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]
MACRO = [("Non-Farm", "Jobs report", "Monthly US hiring and unemployment"), ("Unemployment Rate", "Jobs report", "Monthly US hiring and unemployment"),
         ("CPI", "CPI inflation", "Consumer prices: the inflation number the Fed watches"),
         ("PCE", "PCE inflation", "The Fed's preferred inflation gauge"), ("PPI", "Producer prices", "Wholesale inflation"),
         ("GDP", "GDP", "How fast the US economy is growing"), ("Retail Sales", "Retail sales", "How much US consumers are spending"),
         ("ISM", "ISM business survey", "Factory / service sector health")]


def macro_events(days=28):
    """Major US economic releases over the next few weeks (Yahoo's economic calendar, queried in small windows)."""
    def f():
        cal = yf.Calendars()
        today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
        res, ok = [], 0
        for i in range(0, days, 2):
            d = today + pd.Timedelta(days=i)
            e = None
            for _ in range(2):
                try:
                    e = cal.get_economic_events_calendar(start=d.strftime("%Y-%m-%d"), end=(d + pd.Timedelta(days=2)).strftime("%Y-%m-%d"), limit=100)
                    break
                except Exception:
                    time.sleep(1)
            if e is None:
                continue
            ok += 1
            if e.empty:
                continue
            for name, r in e[e["Region"] == "US"].iterrows():
                for kw, label, desc in MACRO:
                    if kw.lower() in str(name).lower():
                        ts = r["Event Time"].tz_convert("America/New_York")
                        res.append({"date": ts.strftime("%Y-%m-%d"), "time": ts.strftime("%H:%M"), "title": label, "desc": desc,
                                    "kind": "macro", "for": str(r.get("For") or "")})
                        break
        if ok < days // 4:
            raise RuntimeError("economic calendar unavailable")
        seen, out = set(), []
        for x in sorted(res, key=lambda x: (x["date"], x["time"])):
            if (x["date"], x["title"]) not in seen:
                seen.add((x["date"], x["title"]))
                out.append(x)
        return out
    try:
        return cached(("macro",), 6 * 3600, f)
    except Exception:
        return []


def big_earnings(days=21):
    def f():
        today = pd.Timestamp.now(tz="America/New_York").normalize()
        e = yf.Calendars().get_earnings_calendar(start=today.strftime("%Y-%m-%d"), end=(today + pd.Timedelta(days=days)).strftime("%Y-%m-%d"), limit=100)
        out = []
        for s, r in e.iterrows():
            cap = fnum(r.get("Marketcap")) or 0
            ts = r["Event Start Date"]
            out.append({"symbol": s, "name": r.get("Company"), "cap": cap, "timing": r.get("Timing"),
                        "date": ts.tz_convert("America/New_York").strftime("%Y-%m-%d"), "est": fnum(r.get("EPS Estimate"))})
        return out
    try:
        return cached(("bigearn",), 6 * 3600, f)
    except Exception:
        return []


@app.get("/api/calendar")
def calendar(key: str = Depends(current_user)):
    def f():
        today = time.strftime("%Y-%m-%d")
        held = {h["symbol"] for u in users.values() for h in u["holdings"]}
        ev = [{"date": d, "time": "14:00", "title": "Fed rate decision", "desc": "FOMC interest-rate decision and press conference", "kind": "fed"}
              for d in FOMC if d >= today][:1]
        ev += macro_events()
        for x in big_earnings():
            if x["cap"] >= 150e9 or x["symbol"] in held:
                when = {"BMO": "before the open", "AMC": "after the close"}.get(x["timing"], "")
                ev.append({"date": x["date"], "time": "08:00" if x["timing"] == "BMO" else "16:05", "title": f"{x['symbol']} earnings",
                           "desc": f"{x['name']} reports {when}".strip(), "kind": "earnings", "symbol": x["symbol"],
                           "held": x["symbol"] in held})
        ev = [e for e in ev if e["date"] >= today]
        ev.sort(key=lambda e: (e["date"], e["time"]))
        # the strip shows 5: make sure the Fed, key inflation/jobs data and the group's holdings make the cut
        rank = lambda e: (0 if e["kind"] == "fed" else 1 if e["title"] in ("CPI inflation", "Jobs report", "PCE inflation") else
                          2 if e.get("held") else 3 if e["kind"] == "macro" else 4)
        top = sorted(range(len(ev)), key=lambda i: (rank(ev[i]), ev[i]["date"]))[:5]
        for i, e in enumerate(ev):
            e["top"] = i in top
        return {"events": ev[:16], "ts": time.time()}
    return cached(("calendar",), 1800, f)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


threading.Thread(target=backfill_fx, daemon=True).start()
threading.Thread(target=macro_events, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 19050)))
