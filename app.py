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
import yfinance as yf
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = ROOT / "data"
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
    return {"name": u["name"], "holdings": u["holdings"], "watchlist": u["watchlist"], "joined": u.get("joined")}


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


class State(BaseModel):
    holdings: list[Holding] = Field(max_length=200)
    watchlist: list[str] = Field(max_length=100)


@app.put("/api/state")
def put_state(s: State, key: str = Depends(current_user)):
    with lock:
        users[key]["holdings"] = [{**h.model_dump(), "symbol": h.symbol.upper()} for h in s.holdings]
        users[key]["watchlist"] = [w.upper()[:20] for w in s.watchlist]
        save_json(USERS_FILE, users)
    return public(key)


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


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 19050)))
