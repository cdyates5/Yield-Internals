#!/usr/bin/env python3
"""
rebuild.py - US 10-year Treasury yield: market internals model (Acheron Insights)

Self-contained: fetches all data and fonts, runs the model, writes a single-file HTML dashboard.
Built to run unattended (GitHub Actions) as well as locally.

  python rebuild.py                          # -> ./site/index.html, ./site/latest.json
  python rebuild.py --out site --cache cache --max-age-days 7

Data
  Yahoo Finance daily adjusted closes (ETFs, FX, continuous front-month futures)
  Treasury.gov daily par (nominal) and real yield curves, 10y column. FRED republishes these as DGS10 / DFII10
  the next business day, so Treasury is primary and FRED's fredgraph CSV is the fallback. Breakeven = nominal - real.

Model
  Weekly (Friday close) log changes of each internal ratio vs weekly change in the 10y yield (bp), 2004 onward.
  Screen (R1-R5) selects members greedily; membership is frozen in MEMBERS and re-screened on every rebuild
  (drift is flagged in the dashboard, not silently re-selected).
  Composite = equal weight across pillars of equal-weight members; each member is sign-aligned to its economic
  prior and scaled by trailing 52w vol (lagged one week), clipped +/-4.
  Implied 13w yield change = trailing 156w no-intercept beta of 13w dy on 13w composite (lagged one week).

Failure policy (exit codes)
  0  built and fresh
  2  built, but data older than --max-age-days (outputs written; CI should not deploy)
  3  sanity check failed (outputs written for inspection; CI should not deploy)
  1  unrecoverable fetch or runtime error
  A ticker whose download fails falls back to its cached copy and is flagged stale in the dashboard.
"""
import os, io, re, sys, json, time, base64, argparse, tempfile, subprocess, datetime as dt
import urllib.parse
urllib_quote = urllib.parse.quote
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
TTL_HOURS = 10
START = "2004-01-01"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
YSRC = {}      # which source served each yield series
STALE = {}     # ticker -> last cached date, for downloads that failed and fell back to cache

def log(*a): print(*a, flush=True)

# ---------------------------------------------------------------- fetch
import random

BROWSER_HEADERS = [
    "Accept: text/html,application/json,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language: en-US,en;q=0.9",
    "Sec-Fetch-Mode: navigate",
]

def _curl(url, headers=(), cookie=None, save_cookie=None):
    """One curl request. Returns (http_code:str, body:bytes)."""
    with tempfile.NamedTemporaryFile(delete=False) as tf: tmp = tf.name
    try:
        cmd = ["curl", "-s", "-L", "--compressed", "--max-time", "60", "-H", f"User-Agent: {UA}"]
        for h in headers: cmd += ["-H", h]
        if cookie: cmd += ["-b", cookie]
        if save_cookie: cmd += ["-c", save_cookie]
        cmd += ["-o", tmp, "-w", "%{http_code}", url]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip(), open(tmp, "rb").read()
    finally:
        os.unlink(tmp)

def http_get(url, binary=False, tries=6, headers=(), cookie=None, base_wait=3.0, max_wait=90.0):
    """curl with retry + jittered exponential backoff on 429, 5xx and network errors. Raises after `tries`."""
    last = ""
    for i in range(tries):
        code, body = _curl(url, headers=headers, cookie=cookie)
        if code == "200" and body:
            return body if binary else body.decode("utf-8", "replace")
        last = f"HTTP {code or 'none'} {body[:120]!r}"
        if code not in ("", "000", "429") and not code.startswith("5"):
            break                                  # 4xx other than 429: retrying will not help
        if i < tries - 1:
            wait = min(max_wait, base_wait * (2 ** i)) * (0.6 + 0.8 * random.random())   # full jitter
            time.sleep(wait)
    raise RuntimeError(f"GET failed {url}: {last}")

def fresh(path, hours=None):
    hours = TTL_HOURS if hours is None else hours
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < hours * 3600

_YSESSION = {"cookie": None, "crumb": None}

def _yahoo_session(force=False):
    """Establish (or refresh) a Yahoo cookie + crumb. Authorized requests are far less likely to be 429'd."""
    if _YSESSION["crumb"] and not force:
        return _YSESSION
    ck = os.path.join(CACHE, "yahoo_cookies.txt")
    for consent in ("https://fc.yahoo.com", "https://finance.yahoo.com/quote/SPY"):
        _curl(consent, headers=BROWSER_HEADERS, save_cookie=ck)                       # sets A1/A3 consent cookies
        if os.path.exists(ck) and os.path.getsize(ck) > 0: break
    crumb = ""
    for i in range(3):
        code, body = _curl("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=BROWSER_HEADERS, cookie=ck, save_cookie=ck)
        crumb = body.decode("utf-8", "replace").strip()
        if code == "200" and crumb and "<" not in crumb and len(crumb) < 40:
            break
        time.sleep(min(12, 3 * (2 ** i)) * (0.6 + 0.8 * random.random()))
    _YSESSION.update(cookie=ck if os.path.exists(ck) else None, crumb=crumb or None)
    return _YSESSION

def _yahoo_raw(sym, fast=False):
    """Fetch a full daily series from Yahoo and return it (no disk cache; fetch_series owns caching).
    fast=True uses a short retry budget for when Yahoo is only a fallback (e.g. on CI, where it is usually blocked)."""
    sess = _yahoo_session()
    esym = urllib_quote(sym)
    err = None
    tries, bw, mw = (2, 1.5, 6) if fast else (6, 3.0, 90)
    for attempt in range(1 if fast else 2):              # no re-handshake in fast mode
        for host in ("query1", "query2"):
            crumb = f"&crumb={urllib_quote(sess['crumb'])}" if sess.get("crumb") else ""
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{esym}?period1=946684800&period2={int(time.time())}"
                   f"&interval=1d&events=div%2Csplit&includeAdjustedClose=true{crumb}")
            try:
                j = json.loads(http_get(url, headers=BROWSER_HEADERS, cookie=sess.get("cookie"), tries=tries, base_wait=bw, max_wait=mw))["chart"]["result"][0]
                q = j["indicators"]
                vals = q["adjclose"][0]["adjclose"] if q.get("adjclose") else q["quote"][0]["close"]
                meta = j["meta"]
                tz = meta.get("exchangeTimezoneName") or "America/New_York"   # session date in exchange-local time (FX bars stamp 23:00 UTC)
                stamps = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(tz)
                ix = stamps.tz_localize(None).normalize()
                vals = list(vals)
                # Yahoo leaves the day's daily close null for a while after the US close. For US-listed ETFs/equities only,
                # fill it from regularMarketPrice once regularMarketTime is at/after 16:00 New York on that session date.
                if (vals and vals[-1] is None and meta.get("instrumentType") in ("ETF", "EQUITY")
                        and tz == "America/New_York" and meta.get("regularMarketPrice") and meta.get("regularMarketTime")):
                    rmt = pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC").tz_convert(tz)
                    if rmt.normalize().tz_localize(None) == ix[-1] and rmt.hour >= 16:
                        vals[-1] = float(meta["regularMarketPrice"])
                ser = pd.Series(vals, index=ix, dtype=float)
                return ser[~ser.index.duplicated(keep="last")].dropna()
            except Exception as e:
                err = e
        sess = _yahoo_session(force=True)                 # crumb likely stale/blocked; re-handshake and retry once
        time.sleep(2 + 3 * random.random())
    raise RuntimeError(f"Yahoo {sym}: {err}")

def _treasury(kind):
    """Treasury.gov par (nominal) or real yield curve, 10y column, 2003->today."""
    frames = []
    this_year = dt.date.today().year
    for yr in range(2003, this_year + 1):
        p = os.path.join(CACHE, f"t_{kind}_{yr}.csv")
        recent = yr >= this_year - 1                 # prior year too, so late-December prints land after New Year
        if not os.path.exists(p) or (recent and not fresh(p)):
            url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                   f"{yr}/all?type={kind}&field_tdr_date_value={yr}&page&_format=csv")
            txt = http_get(url)
            if not txt.startswith("Date"):
                raise RuntimeError(f"Treasury {kind} {yr} bad payload: {txt[:80]!r}")
            open(p, "w").write(txt)
        df = pd.read_csv(p)
        col = [c for c in df.columns if c.strip().upper() == "10 YR"][0]
        frames.append(pd.Series(pd.to_numeric(df[col], errors="coerce").values, index=pd.to_datetime(df["Date"], format="%m/%d/%Y")))
    return pd.concat(frames).sort_index().dropna()

def _fredgraph(fid, tries=3, base_wait=2, max_wait=15):
    """FRED fredgraph CSV. Bounded retry budget: fredgraph 503s intermittently and long backoff there can
    blow the CI time budget, so callers of non-critical series keep this short and fall back to cache."""
    path = os.path.join(CACHE, f"f_{fid}.csv")
    if not fresh(path):
        txt = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fid}&cosd=2000-01-01",
                       tries=tries, base_wait=base_wait, max_wait=max_wait)
        if not txt[:40].lower().startswith(("observation_date", "date")):
            raise RuntimeError(f"FRED {fid} bad payload: {txt[:80]!r}")
        open(path, "w").write(txt)
    df = pd.read_csv(path)
    return pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(df.iloc[:, 0])).dropna()

def yields():
    """(nominal 10y, real 10y, breakeven 10y). Treasury.gov first (same-day), FRED fallback (T+1)."""
    try:
        n, r = _treasury("daily_treasury_yield_curve"), _treasury("daily_treasury_real_yield_curve")
        YSRC.update(DGS10="Treasury.gov", DFII10="Treasury.gov", T10YIE="Treasury.gov")
        return n, r, (n - r).dropna().round(2)
    except Exception as e:
        log(f"  Treasury.gov failed ({e}); falling back to FRED")
        n, r, b = _fredgraph("DGS10"), _fredgraph("DFII10"), _fredgraph("T10YIE")
        YSRC.update(DGS10="FRED", DFII10="FRED", T10YIE="FRED")
        return n, r, b

# ---------------------------------------------------------------- market data sources
# Yahoo blocks GitHub's shared runner IPs, so CI uses Tiingo (ETFs + FX, one free token) with FRED for the
# broad dollar. Locally, with no token, it falls back to Yahoo, which works fine from a normal IP.
# Commodities are represented by their liquid ETFs (CPER/GLD/USO/SLV); the model uses log-change ratios, where
# an ETF is a faithful proxy for the front-month future (weekly-change corr 0.92-0.98). This is stated on the page.
TIINGO_TOKEN = os.environ.get("TIINGO_TOKEN", "").strip()

TIINGO_ETF = {  # our ticker -> Tiingo daily symbol (adjusted close). Commodity futures map to their ETF proxy.
    "SPY":"spy","XLI":"xli","XLB":"xlb","XLY":"xly","XLF":"xlf","XLE":"xle","XLK":"xlk","XLU":"xlu","XLP":"xlp",
    "XLV":"xlv","KRE":"kre","KBE":"kbe","TIP":"tip","IEF":"ief","SHY":"shy","IWD":"iwd","IWF":"iwf","IWM":"iwm",
    "ITB":"itb","IYT":"iyt","HYG":"hyg","RSP":"rsp","KIE":"kie",
    "HG=F":"cper","GC=F":"gld","CL=F":"uso","SI=F":"slv",
}
TIINGO_FX = {}   # Tiingo Forex only has history from 2020; the model needs 2004, so FX comes from FRED instead
# FX from FRED daily rates (full history, one-business-day lag, works from CI):
#   USD/JPY = DEXJPUS (JPY per USD), matches Yahoo JPY=X orientation
#   AUD/JPY = DEXUSAL (USD per AUD) x DEXJPUS (JPY per USD) = JPY per AUD, matches Yahoo AUDJPY=X
FRED_FX = {"JPY=X": ("DEXJPUS",), "AUDJPY=X": ("DEXUSAL", "DEXJPUS")}
FRED_PROXY = {"DX-Y.NYB":"DTWEXBGS"}   # ICE DXY is not on Tiingo; broad trade-weighted dollar is the CI proxy
PROXIED = {"HG=F":"CPER ETF", "GC=F":"GLD ETF", "CL=F":"USO ETF", "SI=F":"SLV ETF", "DX-Y.NYB":"Fed broad USD index",
           "JPY=X":"FRED daily rate", "AUDJPY=X":"FRED daily rates"}
SRC = {}     # ticker -> source actually used ("Tiingo", "FRED FX", "FRED", "Yahoo")
OPTIONAL = {"DX-Y.NYB"}   # context-only, excluded from the composite: never fail the build over these

def _cache_series(sym):
    return os.path.join(CACHE, "y_" + re.sub(r"[^A-Za-z0-9]", "_", sym) + ".csv")

def tiingo_daily(sym, tsym):
    url = (f"https://api.tiingo.com/tiingo/daily/{tsym}/prices?startDate=2000-01-01"
           f"&format=csv&columns=date,adjClose&token={TIINGO_TOKEN}")
    txt = http_get(url, headers=["Accept: text/csv"], tries=4, base_wait=2, max_wait=20)
    if not txt[:4].lower().startswith("date"):
        raise RuntimeError(f"Tiingo {tsym}: {txt[:100]!r}")
    df = pd.read_csv(io.StringIO(txt))
    s = pd.Series(pd.to_numeric(df["adjClose"], errors="coerce").values, index=pd.to_datetime(df["date"])).dropna()
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    return s

def fred_fx(sym):
    """FX cross from FRED daily rates: a single series, or a product of two (see FRED_FX).
    These are composite members, so use a more patient retry budget than the optional dollar proxy."""
    ids = FRED_FX[sym]
    out = _fredgraph(ids[0], tries=5, base_wait=3, max_wait=40)
    for fid in ids[1:]:
        out = (out * _fredgraph(fid, tries=5, base_wait=3, max_wait=40)).dropna()
    return out

def tiingo_check():
    """One quick call to confirm the token works, so a bad/missing token fails fast and loud
    instead of silently falling through to Yahoo (which GitHub's IPs cannot reach) for 30 tickers."""
    if not TIINGO_TOKEN:
        return False, "TIINGO_TOKEN not set"
    try:
        url = f"https://api.tiingo.com/tiingo/daily/spy/prices?startDate=2026-01-01&format=csv&columns=date,adjClose&token={TIINGO_TOKEN}"
        txt = http_get(url, headers=["Accept: text/csv"], tries=3, base_wait=2, max_wait=15)
        if txt[:4].lower().startswith("date") and len(txt.splitlines()) > 3:
            return True, "ok"
        return False, txt[:160].strip().replace("\n", " ")
    except Exception as e:
        return False, str(e)[:160]

# True after a successful tiingo_check(); lets fetch_series skip the slow Yahoo fallback on CI.
TIINGO_OK = False

def fetch_series(sym):
    """One daily price/level series for `sym`, from the best available source, cached to disk."""
    path = _cache_series(sym)
    if not fresh(path):
        errs = []
        plan = []                                     # ordered (source_label, callable) to try
        if TIINGO_OK and sym in TIINGO_ETF: plan.append(("Tiingo", lambda: tiingo_daily(sym, TIINGO_ETF[sym])))
        if sym in FRED_FX:                  plan.append(("FRED FX", lambda: fred_fx(sym)))
        if sym in FRED_PROXY:               plan.append(("FRED", lambda: _fredgraph(FRED_PROXY[sym])))
        # Yahoo backs up any symbol whose Tiingo/FRED sources are exhausted or missing. On CI (TIINGO_OK) it runs in
        # fast mode (short budget) and, when the symbol already has a Tiingo/FRED mapping, is skipped entirely (blocked IPs).
        if not (TIINGO_OK and (sym in TIINGO_ETF or sym in FRED_FX or sym in FRED_PROXY)):
            plan.append(("Yahoo", lambda: _yahoo_raw(sym, fast=TIINGO_OK)))
        got = None
        for label, fn in plan:
            t0 = time.time()
            try:
                s = fn()
                s = s[~s.index.duplicated(keep="last")].dropna()
                if len(s) < 250: raise RuntimeError(f"only {len(s)} rows")
                s.to_csv(path, header=["v"]); SRC[sym] = label; got = label
                log(f"  {sym:9s} {label:9s} {len(s):5d} rows to {s.index[-1].date()}  {time.time()-t0:4.1f}s")
                break
            except Exception as e:
                errs.append(f"{label}: {e}")
                log(f"  {sym:9s} {label:9s} failed ({time.time()-t0:4.1f}s): {str(e)[:80]}")
        if got is None:
            if os.path.exists(path):
                last = pd.read_csv(path, index_col=0, parse_dates=True).index.max()
                STALE[sym] = str(last.date()); SRC[sym] = "cache"
                log(f"  {sym:9s} CACHE     using copy to {last.date()} (all sources failed)")
            elif sym in OPTIONAL:                 # context-only series: drop it rather than fail the build
                SRC[sym] = "missing"
                log(f"  {sym:9s} MISSING   optional series unavailable, dropped -> " + " | ".join(errs)[:100])
                return None
            else:
                raise RuntimeError(f"{sym} failed from all sources -> " + " | ".join(errs))
    else:
        SRC.setdefault(sym, "cache")
    s = pd.read_csv(path, index_col=0, parse_dates=True)["v"]
    return s.where(s > 0)

FONT_FILES = {
    ("Space Grotesk", 500, "normal"): "space-grotesk@latest/latin-500-normal",
    ("Space Grotesk", 700, "normal"): "space-grotesk@latest/latin-700-normal",
    ("IBM Plex Sans", 400, "normal"): "ibm-plex-sans@latest/latin-400-normal",
    ("IBM Plex Sans", 600, "normal"): "ibm-plex-sans@latest/latin-600-normal",
    ("IBM Plex Sans", 400, "italic"): "ibm-plex-sans@latest/latin-400-italic",
    ("IBM Plex Mono", 400, "normal"): "ibm-plex-mono@latest/latin-400-normal",
    ("IBM Plex Mono", 500, "normal"): "ibm-plex-mono@latest/latin-500-normal",
}
def font_css():
    out = []
    for (fam, w, st), stem in FONT_FILES.items():
        p = os.path.join(CACHE, stem.replace("/", "_").replace("@", "_") + ".woff2")
        if not os.path.exists(p):
            open(p, "wb").write(http_get(f"https://cdn.jsdelivr.net/fontsource/fonts/{stem}.woff2", binary=True))
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        out.append(f"@font-face{{font-family:'{fam}';font-style:{st};font-weight:{w};font-display:swap;"
                   f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}")
    return "\n".join(out)

# ---------------------------------------------------------------- universe
TICKERS = ("SPY XLI XLB XLY XLF XLE XLK XLU XLP XLV KRE KBE TIP IEF SHY DX-Y.NYB JPY=X AUDJPY=X "
           "HG=F GC=F CL=F SI=F IWD IWF IWM ITB IYT HYG RSP KIE").split()

def lr(s): return np.log(s).diff()
def basket(W, names): return pd.concat([lr(W[n]) for n in names], axis=1).mean(axis=1, skipna=False)

# key, label, legs, origin (requested/suggested/tested), prior sign, pillar, builder
CANDS = [
 ("CYC_DEF","Cyclicals / Defensives","XLI+XLB+XLY+XLF vs XLU+XLP+XLV","requested",1,"EQ", lambda W: basket(W,["XLI","XLB","XLY","XLF"])-basket(W,["XLU","XLP","XLV"])),
 ("TIP_IEF","TIPS / Treasuries","TIP vs IEF","requested",1,"INF", lambda W: lr(W.TIP)-lr(W.IEF)),
 ("KRE_XLU","Regional banks / Utilities","KRE vs XLU","requested",1,"EQ", lambda W: lr(W.KRE)-lr(W.XLU)),
 ("DXY","US dollar index","DXY","requested",1,"FX", lambda W: lr(W["DX-Y.NYB"])),
 ("XLU_SPY","Utilities / S&P 500","XLU vs SPY, inverted","requested",-1,"EQ", lambda W: lr(W.XLU)-lr(W.SPY)),
 ("CU_AU","Copper / Gold","COMEX HG vs GC front month","suggested",1,"CMD", lambda W: lr(W["HG=F"])-lr(W["GC=F"])),
 ("USDJPY","USD / JPY","USDJPY spot","suggested",1,"FX", lambda W: lr(W["JPY=X"])),
 ("INFL_DEFL","Inflation / Deflation stocks","XLE+XLB vs XLU+XLP","suggested",1,"INF", lambda W: basket(W,["XLE","XLB"])-basket(W,["XLU","XLP"])),
 ("AUDJPY","AUD / JPY","AUDJPY spot","tested",1,"FX", lambda W: lr(W["AUDJPY=X"])),
 ("OIL_AU","Crude oil / Gold","NYMEX CL vs COMEX GC front month","tested",1,"CMD", lambda W: lr(W["CL=F"])-lr(W["GC=F"])),
 ("IYT_XLU","Transports / Utilities","IYT vs XLU","tested",1,"EQ", lambda W: lr(W.IYT)-lr(W.XLU)),
 ("KBE_SPY","Banks / S&P 500","KBE vs SPY","tested",1,"EQ", lambda W: lr(W.KBE)-lr(W.SPY)),
 ("XLY_XLP","Discretionary / Staples","XLY vs XLP","tested",1,"EQ", lambda W: lr(W.XLY)-lr(W.XLP)),
 ("VAL_GRO","Value / Growth","IWD vs IWF","tested",1,"EQ", lambda W: lr(W.IWD)-lr(W.IWF)),
 ("IWM_SPY","Small caps / S&P 500","IWM vs SPY","tested",1,"EQ", lambda W: lr(W.IWM)-lr(W.SPY)),
 ("RSP_SPY","Equal weight / Cap weight","RSP vs SPY","tested",1,"EQ", lambda W: lr(W.RSP)-lr(W.SPY)),
 ("ITB_SPY","Homebuilders / S&P 500","ITB vs SPY, inverted","tested",-1,"EQ", lambda W: lr(W.ITB)-lr(W.SPY)),
 ("KIE_SPY","Insurers / S&P 500","KIE vs SPY","tested",1,"EQ", lambda W: lr(W.KIE)-lr(W.SPY)),
 ("XLE_SPY","Energy / S&P 500","XLE vs SPY","tested",1,"INF", lambda W: lr(W.XLE)-lr(W.SPY)),
 ("XLK_SPY","Tech / S&P 500","XLK vs SPY, inverted","tested",-1,"EQ", lambda W: lr(W.XLK)-lr(W.SPY)),
 ("AG_AU","Silver / Gold","COMEX SI vs GC front month","tested",1,"CMD", lambda W: lr(W["SI=F"])-lr(W["GC=F"])),
 ("GOLD","Gold","COMEX GC front month, inverted","tested",-1,"CMD", lambda W: lr(W["GC=F"])),
 ("SPY","S&P 500","SPY total return","tested",1,"EQ", lambda W: lr(W.SPY)),
 ("HYG_IEF","High yield / 7-10y Treasuries","HYG vs IEF","tested",1,"CR", lambda W: lr(W.HYG)-lr(W.IEF)),
 ("HYG_SHY","High yield / 1-3y Treasuries","HYG vs SHY, duration-matched","tested",1,"CR", lambda W: lr(W.HYG)-lr(W.SHY)),
]
MECHANICAL = {"HYG_IEF"}
SEMI_MECH = {"TIP_IEF"}
MEMBERS = {"EQ": ["CYC_DEF", "KRE_XLU", "XLU_SPY"], "INF": ["TIP_IEF", "INFL_DEFL"],
           "CMD": ["CU_AU", "OIL_AU"], "FX": ["USDJPY", "AUDJPY"]}
PILLAR_NAMES = {"EQ": "Equity leadership", "INF": "Inflation pricing", "CMD": "Commodities", "FX": "Rate-differential FX", "CR": "Credit"}
CARDS_EXTRA = ["DXY", "HYG_IEF", "IYT_XLU", "KBE_SPY", "VAL_GRO", "SPY", "GOLD"]
RULE = dict(r1=0.25, r2min=0.10, r3=0.85, r5=0.75)

def main():
    global TIINGO_OK
    ok, why = tiingo_check()
    TIINGO_OK = ok
    if ok:
        log("Tiingo token OK: ETFs from Tiingo, FX and dollar from FRED, yields from Treasury.gov")
    elif TIINGO_TOKEN:
        log(f"WARNING: TIINGO_TOKEN is set but not working ({why}). Falling back to Yahoo, which GitHub's IPs usually block.")
    elif os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError("No TIINGO_TOKEN on CI. Yahoo blocks GitHub runner IPs; add the TIINGO_TOKEN secret. "
                           "See the README section 'Add the market-data token'.")
    else:
        log("No TIINGO_TOKEN: using Yahoo (fine locally).")
    log("fetching %d tickers ..." % len(TICKERS))
    D = {}
    for t in TICKERS:
        s = fetch_series(t)
        if s is not None: D[t] = s
        if SRC.get(t) == "Yahoo": time.sleep(0.5 + 1.5 * random.random())   # pace Yahoo only; Tiingo/FRED are fine at speed
    D = pd.DataFrame(D)
    for t in TICKERS:                                     # optional series that were dropped get an all-NaN column
        if t not in D.columns: D[t] = np.nan
    y10, real, bei = yields()
    member_keys = [k for m in MEMBERS.values() for k in m]
    need = ["SPY","XLI","XLB","XLY","XLF","XLE","XLU","XLP","XLV","KRE","TIP","IEF","JPY=X","AUDJPY=X","HG=F","GC=F","CL=F"]
    asof = min([y10.index.max()] + [D[t].dropna().index.max() for t in need])
    D = D.loc[:asof]; y10 = y10.loc[:asof]
    log("as of", asof.date(), "| yield source", YSRC.get("DGS10"))

    wk = lambda s: s.resample("W-FRI").last()
    W = D.resample("W-FRI").last()
    Y = wk(y10); dy = Y.diff() * 100
    dreal = wk(real.loc[:asof]).diff() * 100; dbei = wk(bei.loc[:asof]).diff() * 100
    idx = W.loc[START:].index
    rename_last = lambda ix: ix[:-1].append(pd.DatetimeIndex([asof])) if ix[-1] > asof else ix

    X = pd.DataFrame({c[0]: c[6](W) for c in CANDS}).loc[START:]
    dy, Y, dreal, dbei = [s.reindex(idx) for s in (dy, Y, dreal, dbei)]
    dropped = [k for k in X.columns if X[k].notna().sum() < 52]      # candidates whose data source was unavailable this run
    if dropped:
        log("dropping candidates with no data:", dropped)
        X = X.drop(columns=dropped)
    CANDS_LIVE = [c for c in CANDS if c[0] in X.columns]
    meta = {c[0]: dict(key=c[0], label=c[1], legs=c[2], origin=c[3], sign=c[4], pillar=c[5]) for c in CANDS_LIVE}

    regimes = [("2004-01-01","2008-12-31","2004–08"),("2009-01-01","2013-12-31","2009–13"),("2014-01-01","2019-12-31","2014–19"),
               ("2020-01-01","2022-12-31","2020–22"),("2023-01-01",str(asof.date()),"2023–"+asof.strftime("%y"))]

    def nonoverlap(x, d, h):
        xs, ds = x.rolling(h, min_periods=h).sum(), d.rolling(h, min_periods=h).sum()
        v = [xs.iloc[o::h].corr(ds.iloc[o::h]) for o in range(h)]
        return float(np.nanmean(v))

    def screen(Xs, dys, regs):
        st = {}
        for k in Xs:
            x, s = Xs[k], meta[k]["sign"]
            rc = x.rolling(52, min_periods=52).corr(dys)
            st[k] = dict(
                start=str(x.first_valid_index().date()),
                rw=x.corr(dys), r4=nonoverlap(x, dys, 4), r13=nonoverlap(x, dys, 13),
                reg=[x.loc[a:b].corr(dys.loc[a:b]) for a, b, _ in regs],
                pct=float((np.sign(rc) == s).sum() / rc.notna().sum()))
        return st

    def select(st, Xs):
        order = ([k for k in meta if meta[k]["origin"] == "requested"] + [k for k in meta if meta[k]["origin"] == "suggested"] +
                 sorted([k for k in meta if meta[k]["origin"] == "tested"], key=lambda k: -abs(st[k]["rw"])))
        C = Xs.corr(); chosen = []; why = {}
        for k in order:
            s, t = meta[k]["sign"], st[k]
            fails = []
            if s * t["rw"] < RULE["r1"]: fails.append("R1")
            if min(s * r for r in t["reg"]) <= 0: fails.append("R2")
            if t["pct"] < RULE["r3"]: fails.append("R3")
            if k in MECHANICAL: fails.append("R4")
            red = [(j, C.loc[k, j]) for j in chosen if abs(C.loc[k, j]) >= RULE["r5"]]
            if red and not fails: fails.append("R5")
            why[k] = dict(fails=fails, red=red, marginal=(not fails and min(s * r for r in t["reg"]) < RULE["r2min"]))
            if not fails: chosen.append(k)
        return chosen, why

    st = screen(X, dy, regimes)
    chosen, why = select(st, X)
    C = X.corr()
    drift = sorted(set(chosen) ^ set(member_keys))
    log("selected:", chosen, "| drift vs frozen:", drift)

    # durations for the credit note
    def emp_dur(sym):
        r = lr(W[sym]).reindex(idx) * 1e4; ok = r.notna() & dy.notna()
        return float(-np.polyfit(dy[ok], r[ok], 1)[0])
    dur = {s: emp_dur(s) for s in ["HYG", "IEF", "SHY", "TIP"]}

    # ------------------------------------------------ composite
    def build_comp(members_by_pillar, Xs):
        Z = {}
        for p, ks in members_by_pillar.items():
            for k in ks:
                x = meta[k]["sign"] * Xs[k]
                Z[k] = (x / x.rolling(52, min_periods=26).std().shift(1)).clip(-4, 4)
        Z = pd.DataFrame(Z)
        P = pd.DataFrame({p: Z[ks].mean(axis=1) for p, ks in members_by_pillar.items() if ks})
        return Z, P, P.mean(axis=1)
    Z, P, comp = build_comp(MEMBERS, X)
    first = comp.first_valid_index()
    c13 = comp.rolling(13, min_periods=13).sum(); d13 = dy.rolling(13, min_periods=13).sum()
    num = (c13 * d13).rolling(156, min_periods=104).sum(); den = (c13 * c13).rolling(156, min_periods=104).sum()
    beta = (num / den).shift(1)
    implied = beta * c13; gap = d13 - implied
    gapz = gap / gap.rolling(156, min_periods=104).std().shift(1)
    c13z = c13 / c13.rolling(156, min_periods=104).std().shift(1)
    level = comp.fillna(0).cumsum().where(comp.notna().cummax())
    # weekly beta (bp per composite unit) for the implied level path; first ~2y backfilled with the first PIT estimate
    bw = ((comp * dy).rolling(156, min_periods=104).sum() / (comp * comp).rolling(156, min_periods=104).sum()).shift(1)
    bw_filled = bw.bfill().where(comp.notna())
    impw = bw_filled * comp
    trend = []
    wins = [(a, b, lab) for a, b, lab in regimes] + [(str(first.date()), str(asof.date()), "Full sample"), ("2022-01-01", str(asof.date()), "Since 2022")]
    for a, b, lab in wins:
        w = idx[(idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))]
        w = w[w >= first]
        if len(w) < 10: continue
        pre = idx[idx < w[0]]
        y0 = Y.loc[pre[-1]] if len(pre) else Y.loc[w[0]]
        act_chg = float((Y.loc[w[-1]] - y0) * 100); imp_chg = float(impw.loc[w].sum())
        abs_act = float(dy.loc[w].abs().sum()); abs_imp = float(impw.loc[w].abs().sum())
        trend.append(dict(label=lab, actual=act_chg, implied=imp_chg, r2=float(comp.loc[w].corr(dy.loc[w]) ** 2)))

    def corr_block(x):
        ok = x.notna() & dy.notna()
        return dict(rw=x[ok].corr(dy[ok]), r4=nonoverlap(x[ok], dy[ok], 4), r13=nonoverlap(x[ok], dy[ok], 13),
                    reg=[x.loc[a:b].corr(dy.loc[a:b]) for a, b, _ in regimes])
    comp_stats = corr_block(comp)
    pillar_stats = {p: corr_block(P[p]) for p in P}
    member_aligned = {k: corr_block(meta[k]["sign"] * X[k]) for k in member_keys}
    best_reg = [max(member_aligned[k]["reg"][i] for k in member_keys) for i in range(len(regimes))]
    med_reg = [float(np.median([member_aligned[k]["reg"][i] for k in member_keys])) for i in range(len(regimes))]
    implied_fit = dict(r13_overlap=float(implied.corr(d13)), r13=nonoverlap(comp, dy, 13))

    leadlag = [dict(k=k, r=float(comp.corr(dy.shift(-k)))) for k in range(-8, 9)]

    div = []
    for h in (4, 13, 26):
        fwd = dy.rolling(h, min_periods=h).sum().shift(-h)
        ok = gapz.notna() & fwd.notna(); g, f = gapz[ok], fwd[ok]
        hi, lo, mid = f[g > 1], f[g < -1], f[(g >= -1) & (g <= 1)]
        cm = c13z[ok]
        div.append(dict(h=h, r=float(g.corr(f)), rno=float(np.nanmean([g.iloc[o::h].corr(f.iloc[o::h]) for o in range(h)])),
                        n_eff=int(len(f) / h), hi_mean=float(hi.mean()), hi_n=int(len(hi)), hi_down=float((hi < 0).mean()),
                        lo_mean=float(lo.mean()), lo_n=int(len(lo)), lo_up=float((lo > 0).mean()), mid_mean=float(mid.mean()),
                        unc=float(f.mean()), mom_r=float(cm.corr(f)),
                        mom_rno=float(np.nanmean([cm.iloc[o::h].corr(f.iloc[o::h]) for o in range(h)])),
                        d13_r=float(d13[ok].corr(f))))

    # robustness variants
    def variant(mbp):
        _, _, cv = build_comp(mbp, X); return corr_block(cv), cv
    robust = []
    for k in member_keys:
        mbp = {p: [j for j in ks if j != k] for p, ks in MEMBERS.items()}
        mbp = {p: ks for p, ks in mbp.items() if ks}
        rb, _ = variant(mbp)
        robust.append(dict(name="Without " + meta[k]["label"], rw=rb["rw"], r13=rb["r13"], minreg=min(rb["reg"])))
    rb, _ = variant({"ALL": member_keys}); robust.append(dict(name="No pillars, nine equal weights", rw=rb["rw"], r13=rb["r13"], minreg=min(rb["reg"])))
    # split-sample selection on 2004-13
    Xe, dye = X.loc[:"2013-12-31"], dy.loc[:"2013-12-31"]
    st_e = screen(Xe, dye, regimes[:2]); ch_e, _ = select(st_e, Xe)
    mbp_e = {}
    for k in ch_e: mbp_e.setdefault(meta[k]["pillar"], []).append(k)
    _, _, cv_e = build_comp(mbp_e, X)
    oos = slice("2014-01-01", None)
    def blk(x): 
        x, d = x.loc[oos], dy.loc[oos]; ok = x.notna() & d.notna()
        return dict(rw=float(x[ok].corr(d[ok])), r13=nonoverlap(x[ok], d[ok], 13))
    split = dict(early_members=[meta[k]["label"] for k in ch_e],
                 added=[meta[k]["label"] for k in ch_e if k not in member_keys],
                 dropped=[meta[k]["label"] for k in member_keys if k not in ch_e],
                 early_oos=blk(cv_e), full_oos=blk(comp))

    # ------------------------------------------------ attribution (latest 13w)
    last = comp.last_valid_index()
    npil = len(MEMBERS)
    attrib = []
    for p, ks in MEMBERS.items():
        for k in ks:
            z13 = Z[k].rolling(13, min_periods=13).sum()
            raw13 = float(np.expm1(X[k].rolling(13, min_periods=13).sum().loc[last]))
            attrib.append(dict(key=k, pillar=p, contrib=float(z13.loc[last] / len(ks) / npil),
                               z13=float(z13.loc[last]), raw13=raw13))

    # ------------------------------------------------ per-candidate table + reasons
    def f2(v): return f"{v:+.2f}"
    table = []
    for k in meta:
        m, t, w = meta[k], st[k], why[k]
        s = m["sign"]; reg = t["reg"]
        rreal = float(X[k].corr(dreal)); rbei = float(X[k].corr(dbei))
        inmem = k in member_keys
        reason = []
        if inmem:
            verdict = "Included"
            reason.append(f"{PILLAR_NAMES[m['pillar']]} pillar.")
            if w["fails"]: reason.append("Screen drift on rebuild: now fails " + ", ".join(w["fails"]) + ".")
            if w["marginal"]:
                i = int(np.argmin([s * r for r in reg]))
                reason.append(f"Marginal: weakest regime {regimes[i][2]} at {reg[i]:+.3f}, below the 0.10 soft floor but right-signed.")
            if k in SEMI_MECH:
                reason.append(f"Semi-mechanical: tracks the breakeven leg of the yield (ρ {rbei:+.2f} to ΔBEI); TIP's empirical duration to nominal yields is {dur['TIP']:.1f} vs IEF {dur['IEF']:.1f}.")
        else:
            verdict = "Excluded"
            if k == "HYG_IEF":
                reason.append(f"Mechanical: HYG's empirical rate duration is {dur['HYG']:+.1f} (spread compression offsets carry duration) vs IEF {dur['IEF']:.1f}, so the ratio is effectively short IEF.")
            if k == "HYG_SHY":
                reason.append(f"Duration-matched credit check: HY vs 1–3y Treasuries (SHY duration {dur['SHY']:.1f}) keeps only {t['rw']:+.2f}.")
            if k == "DXY":
                reason.append(f"Prices real yields ({rreal:+.2f}) but runs against breakevens ({rbei:+.2f}); on the nominal yield the legs offset. USD/JPY carries the rate-differential channel cleanly.")
            if "R5" in w["fails"]:
                j, cv = max(w["red"], key=lambda z: abs(z[1]))
                reason.append(f"Redundant: ρ {cv:+.2f} with {meta[j]['label']}.")
            if "R1" in w["fails"]: reason.append(f"Too weak: weekly ρ {t['rw']:+.2f}.")
            if "R2" in w["fails"]:
                bad = [regimes[i][2] + f" ({reg[i]:+.2f})" for i in range(len(reg)) if s * reg[i] <= 0]
                reason.append("Wrong sign in " + ", ".join(bad) + ".")
            if "R3" in w["fails"]: reason.append(f"Rolling 52w sign holds only {t['pct']*100:.0f}% of the time.")
        table.append(dict(key=k, label=m["label"], legs=m["legs"], origin=m["origin"], sign=s, pillar=m["pillar"],
                          start=t["start"], rw=t["rw"], r4=t["r4"], r13=t["r13"], reg=reg, pct=t["pct"],
                          rreal=rreal, rbei=rbei, verdict=verdict, fails=w["fails"], reason=" ".join(reason)))

    # member correlation matrix (sign-aligned)
    Al = pd.DataFrame({k: meta[k]["sign"] * X[k] for k in member_keys})
    cmat = Al.corr().round(3).values.tolist()

    # ------------------------------------------------ series payload
    out_idx = rename_last(idx)
    dates = [d.strftime("%Y-%m-%d") for d in out_idx]
    def arr(s, nd=4):
        return [None if (v is None or not np.isfinite(v)) else round(float(v), nd) for v in s.reindex(idx).values]
    cards = []
    for k in member_keys + CARDS_EXTRA:
        if k not in X.columns: continue               # candidate dropped this run (optional series, e.g. DXY)
        x = X[k]
        if x.notna().sum() < 52: continue
        lv = np.exp(x.fillna(0).cumsum()).where(x.notna().cummax())
        cards.append(dict(key=k, level=arr(lv, 5), rc=arr(x.rolling(52, min_periods=52).corr(dy), 3)))
    series = dict(dates=dates, y10=arr(Y, 3), dy=arr(dy, 1), comp=arr(comp, 3), level=arr(level, 3),
                  c13=arr(c13, 3), d13=arr(d13, 1), implied=arr(implied, 1), gap=arr(gap, 1), gapz=arr(gapz, 2),
                  beta=arr(beta, 2), impw=arr(impw, 2), rc_comp=arr(comp.rolling(52, min_periods=52).corr(dy), 3),
                  pillars={p: arr(P[p].rolling(13, min_periods=13).sum(), 3) for p in P})

    li = idx.get_loc(last)
    latest = dict(asof=str(asof.date()), y10=float(Y.loc[last]), d13=float(d13.loc[last]), implied=float(implied.loc[last]),
                  gap=float(gap.loc[last]), gapz=float(gapz.loc[last]), c13z=float(c13z.loc[last]), beta=float(beta.loc[last]),
                  d4=float(dy.rolling(4).sum().loc[last]), y10_52w_hi=float(Y.iloc[max(0, li-51):li+1].max()),
                  y10_52w_lo=float(Y.iloc[max(0, li-51):li+1].min()))

    payload = dict(meta=dict(built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), asof=str(asof.date()),
                             first_comp=str(first.date()), start=START, rules=RULE, drift=drift, selected_now=chosen,
                             pillar_names=PILLAR_NAMES, ysrc=YSRC, src=SRC, proxied=PROXIED, tiingo=bool(TIINGO_TOKEN),
                             stale=STALE, run_url=run_url(), members=MEMBERS, regimes=[r[2] for r in regimes], dur=dur,
                             n_weeks=int(comp.notna().sum())),
                   latest=latest, table=table, comp_stats=comp_stats, pillar_stats=pillar_stats, member_aligned=member_aligned,
                   best_reg=best_reg, med_reg=med_reg, implied_fit=implied_fit, leadlag=leadlag, div=div,
                   robust=robust, split=split, trend=trend, attrib=attrib, cmat=dict(keys=member_keys, m=cmat), cards=cards, series=series)
    return payload

def clean(o):
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 4)
    if isinstance(o, np.integer): return int(o)
    return o

def run_url():
    e = os.environ
    if e.get("GITHUB_RUN_ID") and e.get("GITHUB_REPOSITORY"):
        return f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e['GITHUB_REPOSITORY']}/actions/runs/{e['GITHUB_RUN_ID']}"
    return None

def sanity(p):
    """Cheap invariants that catch a silently broken build before it is deployed."""
    L, S = p["latest"], p["series"]
    problems = []
    if not (0 < L["y10"] < 20): problems.append(f"10y yield out of range: {L['y10']}")
    if L["implied"] is None or L["gap"] is None: problems.append("latest implied/gap missing")
    if sum(len(v) for v in p["meta"]["members"].values()) != 9: problems.append("member count changed")
    if sum(v is not None for v in S["comp"][-60:]) < 55: problems.append("composite has gaps in the last 60 weeks")
    if p["comp_stats"]["rw"] is None or p["comp_stats"]["rw"] < 0.3: problems.append(f"composite fit collapsed: {p['comp_stats']['rw']}")
    if len(S["dates"]) < 1000: problems.append(f"history too short: {len(S['dates'])} weeks")
    return problems

def latest_json(p):
    L, M = p["latest"], p["meta"]
    return dict(asof=L["asof"], built=M["built"], yield_source=M["ysrc"].get("DGS10"), stale=M["stale"], drift=M["drift"],
                y10=L["y10"], change_13w_bp=L["d13"], implied_13w_bp=L["implied"], unconfirmed_13w_bp=L["gap"],
                unconfirmed_z=L["gapz"], composite_13w_z=L["c13z"], beta_bp_per_unit=L["beta"],
                pillar_contrib={k: round(sum(a["contrib"] for a in p["attrib"] if a["pillar"] == k), 4) for k in M["members"]},
                members={a["key"]: dict(ratio_13w=a["raw13"], contrib=a["contrib"]) for a in p["attrib"]},
                composite_weekly_rho=p["comp_stats"]["rw"], run_url=M["run_url"])

def step_summary(j, status):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path: return
    f = lambda v, d=0: "n/a" if v is None else (f"{v:+.{d}f}")
    lines = [f"### 10y yield internals, {j['asof']} ({status})", "",
             "| | |", "|---|---|",
             f"| 10-year yield | {j['y10']:.2f}% |",
             f"| 13-week change | {f(j['change_13w_bp'])}bp |",
             f"| Implied by internals | {f(j['implied_13w_bp'])}bp |",
             f"| Unconfirmed | {f(j['unconfirmed_13w_bp'])}bp ({f(j['unconfirmed_z'], 1)}σ) |",
             f"| Composite weekly ρ | {j['composite_weekly_rho']:.2f} |",
             f"| Yield source | {j['yield_source']} |"]
    if j["stale"]: lines.append(f"| Stale tickers | {', '.join(f'{k} ({v})' for k, v in j['stale'].items())} |")
    if j["drift"]: lines.append(f"| Screen drift | {', '.join(j['drift'])} |")
    open(path, "a").write("\n".join(lines) + "\n")

TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>US 10-year yield market internals model</title>
<style>
/*__FONTS__*/
:root{
  --paper:#F7F1E6; --card:#FDFAF3; --ink:#221C14; --muted:#6E6556; --rule:#E4DAC7; --soft:#EFE7D8;
  --orange:#D2622A; --teal:#0E756C; --ochre:#A67A22; --plum:#7A4A66; --slate:#4E6577;
  --display:'Space Grotesk', 'Helvetica Neue', Arial, sans-serif;
  --body:'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif;
  --mono:'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --paper:#16130E; --card:#1F1B15; --ink:#EDE4D3; --muted:#A59A89; --rule:#39322A; --soft:#2A251E;
    --orange:#E8804A; --teal:#3FAE9F; --ochre:#D2A650; --plum:#BE88A8; --slate:#93A8BA;
  }
}
:root[data-theme="dark"]{
  --paper:#16130E; --card:#1F1B15; --ink:#EDE4D3; --muted:#A59A89; --rule:#39322A; --soft:#2A251E;
  --orange:#E8804A; --teal:#3FAE9F; --ochre:#D2A650; --plum:#BE88A8; --slate:#93A8BA;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink)}
body{font-family:var(--body);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:28px 28px 64px}
h1,h2,h3{font-family:var(--display);font-weight:500;margin:0;letter-spacing:-0.01em}
h1{font-size:15px;font-weight:500;color:var(--muted);letter-spacing:0}
h2{font-size:24px;line-height:1.2}
h3{font-size:16px;line-height:1.3}
p{margin:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}
a{color:var(--teal)}
:focus-visible{outline:2px solid var(--orange);outline-offset:2px}

header.top{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid var(--rule)}
header.top .asof{font-size:13px;color:var(--muted)}

.hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:40px;padding:34px 0 30px;align-items:end}
.hero .lede{font-family:var(--display);font-weight:500;font-size:clamp(30px,4.2vw,50px);line-height:1.06;letter-spacing:-0.025em;max-width:16ch}
.hero .sub{margin-top:18px;max-width:62ch;color:var(--ink)}
.hero .sub + .sub{margin-top:8px;color:var(--muted);font-size:14px}
.gauge{border-left:1px solid var(--rule);padding-left:28px}
.bar-legend{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px}
.stack{position:relative;height:120px;margin:6px 0 12px}
.stack .col{position:absolute;bottom:0;border-radius:2px 2px 0 0}
.stack .axis{position:absolute;left:0;right:0;bottom:0;border-top:1px solid var(--ink)}
.stat-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:4px}
.stat .v{font-family:var(--mono);font-size:22px;font-weight:500;line-height:1.1}
.stat .k{font-size:12px;color:var(--muted);line-height:1.3;margin-top:3px}

.controls{position:sticky;top:0;z-index:5;background:var(--paper);display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--rule);border-top:1px solid var(--rule)}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:6px;overflow:hidden;background:var(--card)}
.seg button{font:500 13px var(--body);color:var(--muted);background:transparent;border:0;padding:6px 12px;cursor:pointer}
.seg.small button{font-size:12px;padding:4px 10px}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
.controls nav{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}
.controls nav a{color:var(--muted);text-decoration:none}
.controls nav a:hover{color:var(--ink)}

section{padding-top:40px;scroll-margin-top:52px}
.nb{white-space:nowrap}
.sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:24px;flex-wrap:wrap;margin-bottom:16px}
.sec-head p{max-width:72ch;color:var(--muted);font-size:14px}
.panel{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:18px 18px 14px;min-width:0}
.caption{font-size:13px;color:var(--muted);max-width:90ch;margin:-2px 0 10px}
.span2{grid-column:1 / -1}
.heatwrap{position:relative;width:100%;height:236px}
.heatwrap canvas{display:block;width:100%;height:100%}
.panel + .panel{margin-top:16px}
.grid2 > .panel{margin-top:0}
.panel .ph{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.panel .ph p{font-size:13px;color:var(--muted);max-width:70ch}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.chart{position:relative;width:100%}
.h420{height:420px}.h320{height:320px}.h260{height:260px}.h200{height:200px}.h150{height:150px}.h90{height:78px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.legend i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
.legend i.sq{height:10px;width:10px}

.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:16px 16px 10px;display:flex;flex-direction:column}
.card.excluded{background:transparent;border-style:dashed}
.card .top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.card .legs{font-size:12.5px;color:var(--muted)}
.chip{font-size:12px;border-radius:999px;padding:2px 10px;white-space:nowrap;border:1px solid currentColor}
.chip.in{color:var(--teal)}
.chip.out{color:var(--muted)}
.card .kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:12px 0 8px;padding:8px 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.card .kv div span{display:block}
.card .kv .v{font-family:var(--mono);font-size:15px}
.card .kv .k{font-size:11.5px;color:var(--muted);line-height:1.25}
.card .why{font-size:13px;color:var(--muted);margin:2px 0 8px}
.card .strip-label{font-size:11.5px;color:var(--muted);margin-top:2px}

.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule)}
th{font-weight:600;font-size:12px;color:var(--muted);background:var(--card);vertical-align:bottom}
table th.n{white-space:normal;min-width:48px}
#screenTable td:first-child{min-width:170px}
#screenTable td.n{padding-left:6px;padding-right:6px}
td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tr.grp td{background:var(--soft);font-family:var(--display);font-weight:500;font-size:13px;color:var(--ink)}
td.reason{min-width:280px;max-width:420px;color:var(--muted);font-size:12.5px}
td .lbl{font-weight:600}
td .sub{display:block;color:var(--muted);font-size:12px}
.heat td.n{min-width:54px}

.note{font-size:13.5px;color:var(--ink);max-width:78ch}
.note + .note{margin-top:10px}
.cols{columns:2;column-gap:40px}
.cols p{break-inside:avoid;margin-bottom:12px;font-size:13.5px}
.banner{margin-top:16px;padding:10px 14px;border:1px solid var(--orange);border-radius:8px;color:var(--orange);font-size:13.5px}
.kicker{font-size:13px;color:var(--muted);margin-bottom:6px}
footer{margin-top:48px;padding-top:14px;border-top:1px solid var(--rule);font-size:12.5px;color:var(--muted)}

@media (max-width:900px){
  .hero{grid-template-columns:1fr;gap:24px}
  .gauge{border-left:0;padding-left:0;border-top:1px solid var(--rule);padding-top:20px}
  .grid2,.cards{grid-template-columns:minmax(0,1fr)}
  .cols{columns:1}
  .h420{height:340px}
  .wrap{padding:18px 16px 48px}
}
@media (max-width:520px){
  .stat-row{grid-template-columns:repeat(2,minmax(0,1fr))}
  .card .kv{grid-template-columns:repeat(2,minmax(0,1fr))}
  .controls nav{display:none}
}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Acheron Insights quantitative research</h1>
    <div class="asof" id="asof"></div>
  </header>

  <div class="hero">
    <div>
      <div class="kicker">US 10-year Treasury yield, market internals model</div>
      <div class="lede" id="lede"></div>
      <p class="sub" id="sub1"></p>
      <p class="sub" id="sub2"></p>
    </div>
    <div class="gauge" aria-label="Latest 13-week decomposition">
      <div class="bar-legend"><span>13-week change, basis points</span><span id="gaugeScale"></span></div>
      <div class="stack" id="stack"></div>
      <div class="stat-row" id="stats"></div>
    </div>
  </div>
  <div id="driftBanner"></div>

  <div class="controls">
    <div class="seg" role="group" aria-label="Chart range" id="rangeSeg"></div>
    <nav>
      <a href="#composite">Composite</a><a href="#internals">Each internal</a><a href="#screen">The screen</a><a href="#tests">Does it hold up</a><a href="#method">Method</a>
    </nav>
  </div>

  <section id="composite">
    <div class="sec-head">
      <h2>Composite internals index against the 10-year yield</h2>
      <p id="compBlurb"></p>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="legend" id="lgLevel"></div>
        <div class="seg small" role="group" aria-label="Level view" id="levelSeg"><button aria-pressed="true" data-v="index">Index</button><button aria-pressed="false" data-v="path">Implied yield path</button></div>
      </div>
      <p class="caption" id="levelNote"></p>
      <div class="chart h420"><canvas id="cLevel" aria-label="Composite internals index against the 10-year yield"></canvas></div>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="legend"><span><i id="lgA"></i>Actual 13-week change in the 10-year</span><span><i id="lgI"></i>Change implied by internals</span></div>
        <p>Implied = trailing 3-year beta of 13-week yield changes on the 13-week composite, lagged one week.</p>
      </div>
      <div class="chart h320"><canvas id="cImplied" aria-label="Actual versus internals-implied 13-week yield change"></canvas></div>
      <div class="ph" style="margin-top:14px">
        <div class="legend"><span><i class="sq" id="lgGp"></i>Yields above what internals confirm</span><span><i class="sq" id="lgGn"></i>Yields below what internals confirm</span></div>
        <p>Unconfirmed move = actual minus implied, basis points.</p>
      </div>
      <div class="chart h200"><canvas id="cGap" aria-label="Gap between actual and implied yield change"></canvas></div>
    </div>
    <div class="grid2" style="margin-top:16px">
      <div class="panel">
        <div class="ph"><h3>What is driving the latest reading</h3><p>Each member's contribution to the 13-week composite, sign-aligned so positive points to higher yields.</p></div>
        <div class="chart h320"><canvas id="cAttrib" aria-label="Contribution by internal"></canvas></div>
      </div>
      <div class="panel">
        <div class="ph"><h3>Pillar scores over time</h3><p>13-week sum of each pillar's vol-scaled weekly signal. Orange leans to higher yields, slate to lower; saturation at ±8.</p></div>
        <div class="heatwrap" id="heatWrap"><canvas id="cHeat" role="img" aria-label="Heatmap of pillar scores over time"></canvas></div>
        <p class="caption" id="heatRead" style="margin-top:10px" aria-live="polite"></p>
      </div>
    </div>
  </section>

  <section id="internals">
    <div class="sec-head">
      <h2>Each internal on its own</h2>
      <p>Ratio as a log change from the start of the range (right axis) against the 10-year yield (left), with the trailing 52-week correlation of weekly changes underneath. Inverted internals are drawn on a reversed axis so that up always means higher yields. Dashed cards were tested and left out; the reason sits on the card.</p>
    </div>
    <div class="cards" id="cards"></div>
  </section>

  <section id="screen">
    <div class="sec-head">
      <h2>The screen</h2>
      <p id="screenBlurb"></p>
    </div>
    <div class="tablewrap"><table class="heat" id="screenTable"></table></div>
  </section>

  <section id="tests">
    <div class="sec-head">
      <h2>Does it hold up</h2>
      <p id="testsBlurb"></p>
    </div>
    <div class="grid2">
      <div class="panel">
        <div class="ph"><h3>Fit by regime</h3><p>Correlation with weekly 10-year changes. The composite against the best and the median single member in each regime.</p></div>
        <div class="legend" id="lgRegime" style="margin-bottom:6px"></div>
        <div class="chart h260"><canvas id="cRegime" aria-label="Fit by regime"></canvas></div>
        <p class="caption" id="regimeNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Lead or lag</h3><p>Correlation of this week's composite with the yield change k weeks later. Positive k would mean internals lead.</p></div>
        <div class="chart h260"><canvas id="cLead" aria-label="Lead lag correlations"></canvas></div>
        <p class="caption" id="leadNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Texture, not trend</h3><p>Change in the 10-year over each window against the sum of weekly internals-implied changes.</p></div>
        <div class="tablewrap" style="border:0"><table id="trendTable"></table></div>
        <p class="caption" id="trendNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Which leg of the yield each internal prices</h3><p>Weekly correlation with changes in the 10-year real yield (x) and the 10-year breakeven (y).</p></div>
        <div class="chart h320"><canvas id="cLegs" aria-label="Real yield versus breakeven loadings"></canvas></div>
        <p class="caption" id="legsNote" style="margin-top:10px"></p>
      </div>
      <div class="panel span2">
        <div class="ph"><h3>Do divergences close</h3><p>Forward 10-year change after the unconfirmed move is stretched (gap z-score beyond ±1, trailing 3-year scale).</p></div>
        <div class="tablewrap" style="border:0"><table id="divTable"></table></div>
        <p class="caption" id="divNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Robustness</h3><p>Leave-one-out, no pillar structure, and a split-sample check where membership is chosen on 2004–13 data only.</p></div>
        <div class="tablewrap" style="border:0"><table id="robTable"></table></div>
        <p class="caption" id="splitNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Overlap between members</h3><p>Correlation of weekly changes, sign-aligned. The screen rejects any candidate at |ρ| ≥ 0.75 with an existing member.</p></div>
        <div class="tablewrap" style="border:0"><table id="cmatTable" style="font-size:12px"></table></div>
      </div>
    </div>
  </section>

  <section id="method">
    <div class="sec-head"><h2>Method and data</h2></div>
    <div class="cols" id="methodCols"></div>
  </section>

  <footer id="foot"></footer>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const DATA = /*__DATA__*/null;
(function(){
"use strict";
const S = DATA.series, M = DATA.meta, L = DATA.latest;
const byKey = Object.fromEntries(DATA.table.map(r => [r.key, r]));
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const minus = s => s.replace(/-/g, "\u2212");
const fR = (v, d=2) => v == null ? "–" : minus((v >= 0 ? "+" : "") + v.toFixed(d));
const fBp = v => v == null ? "–" : minus((v >= 0 ? "+" : "") + Math.round(v)) + "bp";
const fPct = (v, d=1) => v == null ? "–" : minus((v >= 0 ? "+" : "") + (v*100).toFixed(d)) + "%";
const dLong = s => { const [y,m,d] = s.split("-"); return `${+d} ${MONTHS[+m-1]} ${y}`; };
const dMon = s => { const [y,m] = s.split("-"); return `${MONTHS[+m-1]} ${y}`; };
const el = (tag, attrs={}, html="") => { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (html) e.innerHTML = html; return e; };
const $ = id => document.getElementById(id);
const PCOL = {EQ:"slate", INF:"ochre", CMD:"plum", FX:"orange"};
const lc = t => t.replace(/^./, c => c.toLowerCase());

// ---------------------------------------------------------------- theme tokens (explicit strings for canvas)
let T = {};
function readTokens(){
  const cs = getComputedStyle(document.documentElement);
  ["paper","card","ink","muted","rule","soft","orange","teal","ochre","plum","slate"].forEach(k => T[k] = cs.getPropertyValue("--"+k).trim());
}
function rgba(hex, a){
  const h = hex.replace("#",""); const n = parseInt(h.length === 3 ? h.split("").map(c=>c+c).join("") : h, 16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}
readTokens();

// ---------------------------------------------------------------- range
const RANGES = [["All", null], ["10y", 10], ["5y", 5], ["2y", 2], ["1y", 1]];
let rangeYears = null;
function startIndex(){
  if (!rangeYears) return 0;
  const last = S.dates[S.dates.length-1]; const [y,m,d] = last.split("-").map(Number);
  const cut = `${y - rangeYears}-${String(m).padStart(2,"0")}-${String(d).padStart(2,"0")}`;
  const i = S.dates.findIndex(x => x >= cut); return Math.max(0, i);
}
function boundaryTicks(labels){
  const n = labels.length, out = [];
  const span = n / 52;
  if (span <= 2.6){
    for (let i = 1; i < n; i++){ const a = labels[i-1].slice(5,7), b = labels[i].slice(5,7); if (a !== b && ["01","04","07","10"].includes(b)) out.push(i); }
  } else {
    const step = span > 14 ? 2 : 1;
    for (let i = 1; i < n; i++){ const ya = +labels[i-1].slice(0,4), yb = +labels[i].slice(0,4); if (ya !== yb && yb % step === 0) out.push(i); }
  }
  return out;
}
function xAxis(labels, opts={}){
  const span = labels.length / 52;
  return Object.assign({
    type: "category", offset: false,
    grid: {display: false}, border: {color: T.rule},
    afterBuildTicks: ax => { ax.ticks = boundaryTicks(labels).map(i => ({value: i})); },
    ticks: {autoSkip: false, maxRotation: 0, color: T.muted, font: {family: "IBM Plex Mono", size: 11},
      callback: v => { const s = labels[v]; if (!s) return ""; return span <= 2.6 ? `${MONTHS[+s.slice(5,7)-1]} ${s.slice(2,4)}` : s.slice(0,4); }}
  }, opts);
}
function yAxis(opts={}){
  return Object.assign({grid: {color: T.rule, drawTicks: false}, border: {display: false},
    ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}, padding: 6}}, opts);
}
const baseOpts = () => ({
  responsive: true, maintainAspectRatio: false, animation: false, normalized: true,
  interaction: {mode: "index", intersect: false},
  plugins: {legend: {display: false},
    tooltip: {backgroundColor: T.ink, titleColor: T.paper, bodyColor: T.paper, borderWidth: 0, padding: 9,
      titleFont: {family: "IBM Plex Sans", weight: "600", size: 12}, bodyFont: {family: "IBM Plex Mono", size: 11.5}}},
  layout: {padding: {top: 4, right: 2}}
});
const line = (label, data, color, extra={}) => Object.assign({label, data, borderColor: color, backgroundColor: color, borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: false}, extra);

// ---------------------------------------------------------------- chart registry (lazy + rebuildable)
const REG = [];   // {canvas, build, chart}
function register(canvas, build, ranged=true){
  const r = {canvas, build, chart: null, ranged, visible: false}; REG.push(r); io.observe(canvas); canvas._reg = r; return r;
}
function render(r){ if (r.chart) r.chart.destroy(); r.chart = new Chart(r.canvas.getContext("2d"), r.build()); }
const io = new IntersectionObserver(entries => {
  entries.forEach(e => { const r = e.target._reg; if (e.isIntersecting && !r.visible){ r.visible = true; render(r); } });
}, {rootMargin: "300px 0px"});
function rerender(filter){ REG.forEach(r => { if (r.visible && filter(r)) render(r); }); }

// ---------------------------------------------------------------- header + hero
$("asof").textContent = `Weekly closes to ${dLong(L.asof)}. Built ${M.built}.`;
const upDown = v => v >= 0 ? "up" : "down";
const pill = {}; DATA.attrib.forEach(a => { pill[a.pillar] = (pill[a.pillar] || 0) + a.contrib; });
const conf = L.implied, act = L.d13, gap = L.gap;
const sameSign = Math.sign(conf) === Math.sign(act);
let lede;
if (Math.abs(act) < 10) lede = `The 10-year is roughly flat over 13 weeks; internals imply ${fBp(conf)}.`;
else if (!sameSign) lede = `Yields are ${upDown(act)} ${Math.abs(Math.round(act))}bp in 13 weeks. Internals point the other way.`;
else if (Math.abs(conf) >= Math.abs(act)*0.75) lede = `Yields are ${upDown(act)} ${Math.abs(Math.round(act))}bp in 13 weeks, and internals confirm the move.`;
else lede = `Yields are ${upDown(act)} ${Math.abs(Math.round(act))}bp in 13 weeks. Internals back ${Math.abs(Math.round(conf))}bp of it.`;
$("lede").textContent = lede;
const pOrder = Object.keys(M.members);
const pos = pOrder.filter(p => pill[p] > 0.05).map(p => lc(M.pillar_names[p]));
const neg = pOrder.filter(p => pill[p] < -0.05).map(p => lc(M.pillar_names[p]));
const joinList = a => a.length <= 1 ? (a[0] || "") : a.slice(0,-1).join(", ") + " and " + a[a.length-1];
let s1 = `The 10-year sits at ${L.y10.toFixed(2)}%. `;
if (pos.length && neg.length) s1 += `${joinList(pos).replace(/^./, c => c.toUpperCase())} ${pos.length>1?"point":"points"} to higher yields; ${joinList(neg)} ${neg.length>1?"point":"points"} lower.`;
else if (pos.length) s1 += `${joinList(pos).replace(/^./, c => c.toUpperCase())} ${pos.length>1?"point":"points"} to higher yields; nothing leans materially lower.`;
else if (neg.length) s1 += `${joinList(neg).replace(/^./, c => c.toUpperCase())} ${neg.length>1?"point":"points"} to lower yields; nothing leans materially higher.`;
$("sub1").textContent = s1;
// how unusual is the gap
let lastMatch = null;
if (L.gapz != null){
  for (let i = S.gapz.length - 9; i >= 0; i--){ const g = S.gapz[i]; if (g != null && (L.gapz >= 0 ? g >= L.gapz : g <= L.gapz)){ lastMatch = S.dates[i]; break; } }
}
const d13 = DATA.div.find(d => d.h === 13), d26 = DATA.div.find(d => d.h === 26);
let s2 = `Unconfirmed move ${fBp(gap)} (${fR(L.gapz,1)}σ on a trailing 3-year scale)`;
s2 += lastMatch ? `, the most stretched since ${dMon(lastMatch)}. ` : ". ";
if (Math.abs(L.gapz) >= 1){
  const dd = L.gapz > 0 ? d26.hi_mean : d26.lo_mean, pr = L.gapz > 0 ? d26.hi_down : d26.lo_up;
  s2 += `Historically, from here the 10-year moved ${fBp(dd)} over the next 26 weeks on average (${Math.round(pr*100)}% of cases in the closing direction). Weak evidence; see the divergence test.`;
} else s2 += `Within normal range; no divergence signal.`;
$("sub2").textContent = s2;

// gauge: three columns actual / implied / gap
(function gauge(){
  const vals = [["Actual", act, T.ink], ["Internals", conf, T.teal], ["Unconfirmed", gap, T.orange]];
  const mx = Math.max(25, ...vals.map(v => Math.abs(v[1]))) * 1.1;
  const hasNeg = vals.some(v => v[1] < 0);
  const box = $("stack"); box.innerHTML = "";
  const H = 120, zero = hasNeg ? H/2 : H;
  const axis = el("div", {class: "axis"}); axis.style.bottom = (H - zero) + "px"; box.appendChild(axis);
  vals.forEach((v, i) => {
    const h = Math.abs(v[1]) / mx * (hasNeg ? H/2 : H);
    const c = el("div", {class: "col"}); c.style.left = `calc(${i*33.33}% + 10px)`; c.style.width = `calc(33.33% - 20px)`;
    c.style.background = v[2]; c.style.height = h + "px";
    if (v[1] >= 0) c.style.bottom = (H - zero) + "px"; else { c.style.bottom = (H - zero - h) + "px"; c.style.borderRadius = "0 0 2px 2px"; }
    box.appendChild(c);
  });
  $("gaugeScale").textContent = `beta ${L.beta.toFixed(1)}bp per unit`;
  const st = $("stats");
  [[L.y10.toFixed(2)+"%", "10-year yield"], [fBp(act), "Actual 13-week change"], [fBp(conf), "Implied by internals"], [fBp(gap), "Unconfirmed"]]
    .forEach(([v,k], i) => { const d = el("div", {class: "stat"}, `<div class="v">${minus(v)}</div><div class="k">${k}</div>`); if (i===2) d.querySelector(".v").style.color = "var(--teal)"; if (i===3) d.querySelector(".v").style.color = "var(--orange)"; st.appendChild(d); });
})();

if (M.stale && Object.keys(M.stale).length){
  $("driftBanner").appendChild(el("div", {class: "banner"}, `Stale inputs: the latest download failed for ${Object.entries(M.stale).map(([k,v]) => `${k} (cached to ${dLong(v)})`).join(", ")}. Readings use the cached history; the next scheduled run will retry.`));
}
if (M.drift && M.drift.length){
  $("driftBanner").appendChild(el("div", {class: "banner"}, `Screen drift: re-running the screen on current data would change membership for ${M.drift.map(k => byKey[k].label).join(", ")}. Membership is frozen; review before changing it.`));
}

// ---------------------------------------------------------------- range control
RANGES.forEach(([lab, yrs]) => {
  const b = el("button", {"aria-pressed": String(yrs === rangeYears)}, lab);
  b.addEventListener("click", () => { rangeYears = yrs; [...$("rangeSeg").children].forEach(x => x.setAttribute("aria-pressed", String(x === b))); rerender(r => r.ranged); drawHeat(null); });
  $("rangeSeg").appendChild(b);
});

const cs = DATA.comp_stats;
$("compBlurb").textContent = `Nine internals in four pillars. Weekly correlation with 10-year changes ${fR(cs.rw)}, ${fR(cs.r13)} on non-overlapping 13-week changes, and never below ${fR(Math.min(...cs.reg))} in any regime since 2004.`;

// legends
let levelView = "index";
const sw = (id, c) => { $(id).style.background = c; };
function paintLegends(){
  sw("lgA", T.ink); sw("lgI", T.teal); sw("lgGp", T.orange); sw("lgGn", T.slate);
  $("lgLevel").innerHTML = levelView === "index"
    ? `<span><i style="background:${T.ink}"></i>10-year yield, % (left)</span><span><i style="background:${T.teal}"></i>Internals index, cumulative vol units (right)</span>`
    : `<span><i style="background:${T.ink}"></i>10-year yield, %</span><span><i style="background:${T.teal}"></i>Yield path implied by internals, from the start of the range</span>`;
  $("lgRegime").innerHTML = `<span><i class="sq" style="background:${T.teal}"></i>Composite</span><span><i class="sq" style="background:${T.slate}"></i>Best single member</span><span><i class="sq" style="background:${T.rule}"></i>Median member</span>`;
}
paintLegends();

// ---------------------------------------------------------------- composite charts
const sl = a => a.slice(startIndex());
const levelReg = register($("cLevel"), () => {
  const i0 = startIndex(); const labels = S.dates.slice(i0); const o = baseOpts();
  const y = S.y10.slice(i0);
  if (levelView === "index"){
    const lv = S.level.slice(i0); const b = lv.find(v => v != null) ?? 0;
    o.scales = {x: xAxis(labels), y: yAxis({position: "left", ticks: Object.assign(yAxis().ticks, {callback: v => v.toFixed(1)+"%"})}),
                y1: yAxis({position: "right", grid: {display: false}, ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(0))})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => c.datasetIndex === 0 ? ` 10-year ${c.parsed.y.toFixed(2)}%` : ` Internals ${fR(c.parsed.y,1)}`};
    $("levelNote").textContent = "Cumulative sum of the weekly composite, zeroed at the start of the range. The overlay is for the eye; every statistic on this page is computed on changes, because level-on-level fit between trending series is spurious.";
    return {type: "line", data: {labels, datasets: [line("10-year", y, T.ink, {yAxisID: "y", borderWidth: 1.8}), line("Internals", lv.map(v => v == null ? null : v - b), T.teal, {yAxisID: "y1"})]}, options: o};
  }
  const w = S.impw.slice(i0); let acc = 0; const y0 = y.find(v => v != null);
  const path = w.map((v, i) => { if (i > 0 && v != null) acc += v; return y0 + acc/100; });
  o.scales = {x: xAxis(labels), y: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => v.toFixed(1)+"%"})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` ${c.dataset.label} ${c.parsed.y.toFixed(2)}%`};
  const act = (y[y.length-1] - y0) * 100, imp = acc;
  $("levelNote").textContent = `From ${dLong(labels[0])}: the 10-year moved ${fBp(act)}; internals imply ${fBp(imp)}. Weekly changes are mapped to basis points with the trailing 3-year beta (the first two years use the first available estimate). Whatever internals miss accumulates, so the paths drift apart over long windows; that drift is the policy-driven part of the yield.`;
  return {type: "line", data: {labels, datasets: [line("10-year", y, T.ink, {borderWidth: 1.8}), line("Implied path", path, T.teal, {borderWidth: 1.6})]}, options: o};
});
$("levelSeg").querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
  levelView = btn.dataset.v; $("levelSeg").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === btn)));
  paintLegends(); if (levelReg.visible) render(levelReg);
}));
register($("cImplied"), () => {
  const labels = sl(S.dates); const o = baseOpts();
  o.scales = {x: xAxis(labels), y: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(String(v))})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` ${c.dataset.label} ${fBp(c.parsed.y)}`};
  return {type: "line", data: {labels, datasets: [line("Actual", sl(S.d13), T.ink, {borderWidth: 1.5}), line("Implied", sl(S.implied), T.teal, {borderWidth: 1.8})]}, options: o};
});
register($("cGap"), () => {
  const labels = sl(S.dates), g = sl(S.gap); const o = baseOpts();
  o.scales = {x: xAxis(labels), y: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(String(v)), maxTicksLimit: 5})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` Unconfirmed ${fBp(c.parsed.y)}  (z ${fR(sl(S.gapz)[c.dataIndex],1)})`};
  return {type: "bar", data: {labels, datasets: [{label: "Gap", data: g, backgroundColor: g.map(v => v >= 0 ? T.orange : T.slate), barPercentage: 1, categoryPercentage: 1, borderWidth: 0}]}, options: o};
});
register($("cAttrib"), () => {
  const A = DATA.attrib; const o = baseOpts(); o.indexAxis = "y"; o.interaction = {mode: "nearest", intersect: true, axis: "y"};
  o.scales = {x: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(1))})}),
              y: {grid: {display: false}, border: {color: T.rule}, ticks: {color: T.ink, font: {family: "IBM Plex Sans", size: 12}}}};
  o.plugins.tooltip.callbacks = {title: it => byKey[A[it[0].dataIndex].key].label,
    label: c => { const a = A[c.dataIndex]; return [` Contribution ${fR(a.contrib)}`, ` Ratio 13w ${fPct(a.raw13)}`, ` Member 13w score ${fR(a.z13,1)}`, ` Pillar ${M.pillar_names[a.pillar]}`]; }};
  return {type: "bar", data: {labels: A.map(a => byKey[a.key].label), datasets: [{data: A.map(a => a.contrib), backgroundColor: A.map(a => T[PCOL[a.pillar]]), borderWidth: 0, barPercentage: 0.72}]}, options: o};
}, false);
function drawHeat(hoverIdx){
  const cv = $("cHeat"), wrap = $("heatWrap"); const dpr = window.devicePixelRatio || 1;
  const W = wrap.clientWidth, H = wrap.clientHeight; if (!W) return;
  if (cv.width !== Math.round(W*dpr) || cv.height !== Math.round(H*dpr)){ cv.width = Math.round(W*dpr); cv.height = Math.round(H*dpr); }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const i0 = startIndex(); const labels = S.dates.slice(i0); const n = labels.length;
  const padL = 132, padB = 22, rows = pOrder.length, rh = (H - padB - 4) / rows, cw = (W - padL) / n;
  const hex2 = (hex) => { const h = hex.replace("#",""); const v = parseInt(h, 16); return [(v>>16)&255, (v>>8)&255, v&255]; };
  const hi = hex2(T.orange), lo = hex2(T.slate), base = hex2(T.card);
  const mix = (c, a) => `rgb(${Math.round(base[0]+(c[0]-base[0])*a)},${Math.round(base[1]+(c[1]-base[1])*a)},${Math.round(base[2]+(c[2]-base[2])*a)})`;
  ctx.font = "12px 'IBM Plex Sans', sans-serif"; ctx.textBaseline = "middle";
  pOrder.forEach((p, r) => {
    const y = 2 + r*rh; const arr = S.pillars[p].slice(i0);
    ctx.fillStyle = T.ink; ctx.fillText(M.pillar_names[p], 0, y + rh/2);
    for (let i = 0; i < n; i++){
      const v = arr[i]; if (v == null) continue;
      const a = Math.min(1, Math.abs(v)/8); ctx.fillStyle = mix(v >= 0 ? hi : lo, a);
      ctx.fillRect(padL + i*cw, y, Math.max(cw, 1) + 0.6, rh - 3);
    }
  });
  ctx.fillStyle = T.muted; ctx.font = "11px 'IBM Plex Mono', monospace"; ctx.textBaseline = "top";
  const span = n/52;
  boundaryTicks(labels).forEach(i => { const x = padL + i*cw; const s = labels[i];
    ctx.fillRect(x, H - padB, 1, 4);
    const t = span <= 2.6 ? `${MONTHS[+s.slice(5,7)-1]} ${s.slice(2,4)}` : s.slice(0,4);
    const tw = ctx.measureText(t).width; if (x - tw/2 > padL - 4 && x + tw/2 < W) ctx.fillText(t, x - tw/2, H - padB + 6); });
  const k = hoverIdx == null ? n - 1 : hoverIdx;
  if (hoverIdx != null){ ctx.fillStyle = T.ink; ctx.fillRect(padL + k*cw, 0, Math.max(1, cw), H - padB); }
  const parts = pOrder.map(p => `${M.pillar_names[p]} ${fR(S.pillars[p][i0 + k], 1)}`);
  $("heatRead").textContent = `${hoverIdx == null ? "Latest, " : ""}${dLong(labels[k])}: ${parts.join(", ")}.`;
  cv._geo = {padL, cw, n};
}
$("cHeat").addEventListener("mousemove", e => { const g = $("cHeat")._geo; if (!g) return; const r = $("cHeat").getBoundingClientRect(); const i = Math.floor((e.clientX - r.left - g.padL) / g.cw); drawHeat(i >= 0 && i < g.n ? i : null); });
$("cHeat").addEventListener("mouseleave", () => drawHeat(null));
let rzT; window.addEventListener("resize", () => { clearTimeout(rzT); rzT = setTimeout(() => drawHeat(null), 120); });
drawHeat(null);

// ---------------------------------------------------------------- cards
const regMinIdx = r => { let m = 0; r.reg.forEach((v,i) => { if (r.sign*v < r.sign*r.reg[m]) m = i; }); return m; };
DATA.cards.forEach(c => {
  const r = byKey[c.key]; const inc = r.verdict === "Included";
  const card = el("article", {class: "card" + (inc ? "" : " excluded")});
  const chip = inc ? `<span class="chip in">In the composite, ${lc(M.pillar_names[r.pillar])}</span>` : `<span class="chip out">Left out</span>`;
  const mi = regMinIdx(r);
  const attr = DATA.attrib.find(a => a.key === c.key);
  const chg13 = attr ? attr.raw13 : null;
  let nowTxt = "–";
  if (attr){ nowTxt = (attr.z13 >= 0 ? "Higher" : "Lower"); }
  card.innerHTML = `<div class="top"><div><h3>${r.label}</h3><div class="legs">${r.legs}, from ${r.start.slice(0,4)}</div></div>${chip}</div>
    <div class="kv">
      <div><span class="v">${fR(r.rw)}</span><span class="k">Weekly ρ</span></div>
      <div><span class="v">${fR(r.r13)}</span><span class="k">13-week ρ</span></div>
      <div><span class="v">${fR(r.reg[mi])}</span><span class="k">Weakest regime, <span class="nb">${M.regimes[mi]}</span></span></div>
      <div><span class="v">${attr ? minus(fPct(chg13)) : fR(r.rbei)}</span><span class="k">${attr ? "Ratio, last 13 weeks (" + nowTxt.toLowerCase() + " yields)" : "ρ to breakeven"}</span></div>
    </div>
    ${r.reason && (!inc || /Marginal|Semi|drift/.test(r.reason)) ? `<p class="why">${r.reason.replace(/^[A-Za-z -]+ pillar\. /, "")}</p>` : ""}
    <div class="chart h200"><canvas aria-label="${r.label} against the 10-year yield"></canvas></div>
    <div class="strip-label">Trailing 52-week correlation with weekly 10-year changes</div>
    <div class="chart h90"><canvas aria-label="${r.label} rolling correlation"></canvas></div>`;
  $("cards").appendChild(card);
  const [cv1, cv2] = card.querySelectorAll("canvas");
  register(cv1, () => {
    const i0 = startIndex(); const labels = S.dates.slice(i0); const lv = c.level.slice(i0);
    const b = lv.find(v => v != null); const reb = lv.map(v => v == null || b == null ? null : 100 * Math.log(v / b));
    const o = baseOpts();
    const col = inc ? T.teal : T.slate;
    o.scales = {x: xAxis(labels), y: yAxis({position: "left", ticks: Object.assign(yAxis().ticks, {callback: v => v.toFixed(1)+"%", maxTicksLimit: 5})}),
      y1: yAxis({position: "right", reverse: r.sign < 0, grid: {display: false}, ticks: Object.assign(yAxis().ticks, {maxTicksLimit: 5, callback: v => minus((v > 0 ? "+" : "") + Math.round(v)) + "%"})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: x => x.datasetIndex === 0 ? ` 10-year ${x.parsed.y.toFixed(2)}%` : ` ${r.label} ${fR(x.parsed.y,1)}% log change${r.sign<0?", inverted axis":""}`};
    return {type: "line", data: {labels, datasets: [line("10-year", S.y10.slice(i0), T.ink, {yAxisID: "y", borderWidth: 1.3}), line(r.label, reb, col, {yAxisID: "y1", borderWidth: 1.5})]}, options: o};
  });
  register(cv2, () => {
    const i0 = startIndex(); const labels = S.dates.slice(i0); const rc = c.rc.slice(i0);
    const o = baseOpts();
    const good = r.sign > 0 ? T.teal : T.orange, bad = r.sign > 0 ? T.orange : T.teal;
    o.scales = {x: xAxis(labels, {display: false}), y: yAxis({min: -1, max: 1, ticks: Object.assign(yAxis().ticks, {stepSize: 1, callback: v => minus(String(v))})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: x => ` 52w ρ ${fR(x.parsed.y)}`};
    return {type: "line", data: {labels, datasets: [line("52w ρ", rc, T.ink, {borderWidth: 1.1, fill: {target: "origin", above: rgba(good, 0.28), below: rgba(bad, 0.28)}})]}, options: o};
  });
});

// ---------------------------------------------------------------- screen table
function screenTable(){
  const nInc = DATA.table.filter(r => r.verdict === "Included").length;
  $("screenBlurb").innerHTML = `${DATA.table.length} candidates, ${nInc} admitted. Rules, applied in order with requested internals first, then the ones you suggested, then the rest by strength: <b>R1</b> weekly |ρ| ≥ ${M.rules.r1.toFixed(2)} with the expected sign; <b>R2</b> expected sign in all five regimes (weakest below ${M.rules.r2min.toFixed(2)} is flagged marginal); <b>R3</b> trailing 52-week correlation right-signed at least ${Math.round(M.rules.r3*100)}% of the time; <b>R4</b> not mechanically tied to Treasury prices; <b>R5</b> |ρ| &lt; ${M.rules.r5.toFixed(2)} with every member already admitted. Regime cells are shaded teal when right-signed and orange when wrong.`;
  const tb = $("screenTable");
  const head = `<thead><tr><th>Internal</th><th class="n">Weekly ρ</th><th class="n">4w ρ</th><th class="n">13w ρ</th>${M.regimes.map(g => `<th class="n">${g}</th>`).join("")}<th class="n">52w sign held</th><th class="n">ρ real</th><th class="n">ρ breakeven</th><th>Verdict</th></tr></thead>`;
  const origins = [["requested","Requested"],["suggested","Suggested in the brief"],["tested","Also tested"]];
  const shade = (v, s) => { if (v == null) return ""; const a = Math.min(1, Math.abs(v)/0.7) * 0.42; const c = s*v > 0 ? T.teal : T.orange; return `background:${rgba(c, a)}`; };
  let body = "<tbody>";
  origins.forEach(([o, name]) => {
    body += `<tr class="grp"><td colspan="${9 + M.regimes.length}">${name}</td></tr>`;
    DATA.table.filter(r => r.origin === o).sort((a,b) => (a.verdict === b.verdict ? Math.abs(b.rw) - Math.abs(a.rw) : a.verdict === "Included" ? -1 : 1)).forEach(r => {
      body += `<tr><td><span class="lbl">${r.label}</span><span class="sub">${r.legs}, from ${r.start.slice(0,4)}</span></td>
        <td class="n">${fR(r.rw)}</td><td class="n">${fR(r.r4)}</td><td class="n">${fR(r.r13)}</td>
        ${r.reg.map(v => `<td class="n" style="${shade(v, r.sign)}">${fR(v)}</td>`).join("")}
        <td class="n">${Math.round(r.pct*100)}%</td><td class="n">${fR(r.rreal)}</td><td class="n">${fR(r.rbei)}</td>
        <td class="reason"><span class="lbl" style="color:${r.verdict === "Included" ? "var(--teal)" : "var(--muted)"}">${r.verdict}${r.fails.length && r.verdict !== "Included" ? " (" + r.fails.join(", ") + ")" : ""}</span><span class="sub">${r.reason}</span></td></tr>`;
    });
  });
  tb.innerHTML = head + body + "</tbody>";
}
screenTable();

// ---------------------------------------------------------------- tests
register($("cRegime"), () => {
  const o = baseOpts(); o.interaction = {mode: "index", intersect: false};
  o.scales = {x: {grid: {display: false}, border: {color: T.rule}, ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}}},
              y: yAxis({min: 0, max: 1, ticks: Object.assign(yAxis().ticks, {stepSize: 0.25, callback: v => v.toFixed(2)})})};
  o.plugins.tooltip.callbacks = {label: c => ` ${c.dataset.label} ${fR(c.parsed.y)}`};
  return {type: "bar", data: {labels: M.regimes, datasets: [
    {label: "Composite", data: DATA.comp_stats.reg, backgroundColor: T.teal, borderWidth: 0},
    {label: "Best single member", data: DATA.best_reg, backgroundColor: T.slate, borderWidth: 0},
    {label: "Median member", data: DATA.med_reg, backgroundColor: T.rule, borderWidth: 0}]}, options: o};
}, false);
(function regimeNote(){
  const ms = Object.keys(DATA.member_aligned);
  const lastI = M.regimes.length - 1;
  const bestKey = ms.reduce((a, k) => DATA.member_aligned[k].reg[lastI] > DATA.member_aligned[a].reg[lastI] ? k : a, ms[0]);
  const beats = DATA.comp_stats.reg.map((v,i) => v >= DATA.best_reg[i]).filter(Boolean).length;
  $("regimeNote").textContent = `The composite beats the median member in every regime and the best single member in ${beats} of ${M.regimes.length}. In ${M.regimes[lastI]} the best member is ${byKey[bestKey].label} (${fR(DATA.best_reg[lastI])}), because recent yield moves have been breakeven-led and that ratio is partly the breakeven itself.`;
})();
register($("cLead"), () => {
  const LL = DATA.leadlag; const o = baseOpts(); o.interaction = {mode: "nearest", intersect: false, axis: "x"};
  o.scales = {x: {grid: {display: false}, border: {color: T.rule}, title: {display: true, text: "k, weeks", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}, callback: v => minus(String(LL[v].k))}},
              y: yAxis({min: -0.1, max: 0.7, ticks: Object.assign(yAxis().ticks, {stepSize: 0.1, callback: v => minus(v.toFixed(1))})})};
  o.plugins.tooltip.callbacks = {title: it => { const k = LL[it[0].dataIndex].k; return k === 0 ? "Same week" : k > 0 ? `Internals ${k}w ahead of yields` : `Yields ${-k}w ahead of internals`; }, label: c => ` ρ ${fR(c.parsed.y, 3)}`};
  return {type: "bar", data: {labels: LL.map(d => d.k), datasets: [{data: LL.map(d => d.r), backgroundColor: LL.map(d => d.k === 0 ? T.teal : T.slate), borderWidth: 0, barPercentage: 0.7}]}, options: o};
}, false);
(function leadNote(){
  const ahead = DATA.leadlag.filter(d => d.k > 0).map(d => Math.abs(d.r));
  $("leadNote").textContent = `All of the relationship is contemporaneous: ρ ${fR(DATA.leadlag.find(d=>d.k===0).r)} in the same week, and no lead above |${Math.max(...ahead).toFixed(2)}| at 1 to 8 weeks. Treat the composite as a confirmation gauge, not a forecast.`;
})();
(function divTable(){
  const t = $("divTable");
  let h = `<thead><tr><th class="n">Horizon</th><th class="n">ρ gap z, fwd</th><th class="n">Non-overlap ρ</th><th class="n">Indep. obs</th><th class="n">Gap z &gt; +1</th><th class="n">Gap z &lt; −1</th><th class="n">All weeks</th></tr></thead><tbody>`;
  DATA.div.forEach(d => {
    h += `<tr><td class="n">${d.h}w</td><td class="n">${fR(d.r)}</td><td class="n">${fR(d.rno)}</td><td class="n">${d.n_eff}</td>
      <td class="n">${fBp(d.hi_mean)}<span class="sub">${Math.round(d.hi_down*100)}% fell, n ${d.hi_n}</span></td>
      <td class="n">${fBp(d.lo_mean)}<span class="sub">${Math.round(d.lo_up*100)}% rose, n ${d.lo_n}</span></td>
      <td class="n">${fBp(d.unc)}</td></tr>`;
  });
  t.innerHTML = h + "</tbody>";
  const d4 = DATA.div.find(d => d.h === 4);
  $("divNote").textContent = `Right direction, small size. Conditional means move by a few basis points against a ±1σ gap that is typically ${Math.round(Math.abs(L.gap / (L.gapz || 1)))}bp wide, and hit rates sit in the high 50s to low 60s on heavily overlapping windows (n counts weeks, not independent episodes). What predictive content exists lives in the internals' own momentum: the 13-week composite z-score correlates ${fR(d4.mom_r)} with the next 4 weeks of yield changes, against ${fR(d4.d13_r)} for the yield's own 13-week change.`;
})();
(function trendTable(){
  const t = $("trendTable");
  let h = `<thead><tr><th>Window</th><th class="n">10-year change</th><th class="n">Implied by internals</th><th class="n">Weekly R²</th></tr></thead><tbody>`;
  DATA.trend.forEach(d => { h += `<tr${d.label === "Full sample" ? ' style="font-weight:600"' : ""}><td>${d.label}</td><td class="n">${fBp(d.actual)}</td><td class="n">${fBp(d.implied)}</td><td class="n">${d.r2.toFixed(2)}</td></tr>`; });
  t.innerHTML = h + "</tbody>";
  const s22 = DATA.trend.find(d => d.label === "Since 2022");
  $("trendNote").textContent = `Internals explain about a third of weekly variance, and almost none of the level shifts. Since 2022 the 10-year is ${fBp(s22.actual)} while summed implied changes come to ${fBp(s22.implied)}. The hiking cycle and term premium moved yields without a matching rotation in the ratios. Read the composite for whether a move is being confirmed, not for where the yield should be.`;
  const cs = DATA.comp_stats, d13 = DATA.div.find(d => d.h === 13);
  $("testsBlurb").textContent = `Coincident fit is strong and stable (weekly ρ ${fR(cs.rw)}, never below ${fR(Math.min(...cs.reg))} by regime). There is no lead. Divergences close only weakly (13-week ρ ${fR(d13.rno)}). The level trend is mostly unexplained.`;
})();
(function robTable(){
  const t = $("robTable"); const c = DATA.comp_stats;
  let h = `<thead><tr><th>Variant</th><th class="n">Weekly ρ</th><th class="n">13w ρ</th><th class="n">Weakest regime</th></tr></thead><tbody>`;
  h += `<tr><td><span class="lbl">Composite as built</span></td><td class="n">${fR(c.rw)}</td><td class="n">${fR(c.r13)}</td><td class="n">${fR(Math.min(...c.reg))}</td></tr>`;
  DATA.robust.forEach(r => {
    const dlt = r.rw - c.rw;
    h += `<tr><td>${r.name}</td><td class="n">${fR(r.rw)} <span class="muted" style="font-size:11px">${fR(dlt,2)}</span></td><td class="n">${fR(r.r13)}</td><td class="n">${fR(r.minreg)}</td></tr>`;
  });
  t.innerHTML = h + "</tbody>";
  const sp = DATA.split;
  const without = DATA.robust.filter(r => r.name.startsWith("Without"));
  const worst = without.reduce((a,b) => b.rw < a.rw ? b : a), best = without.reduce((a,b) => b.rw > a.rw ? b : a);
  $("splitNote").textContent = `No single member carries the index: dropping ${worst.name.replace("Without ","")} costs the most (${fR(worst.rw - c.rw)}); dropping ${best.name.replace("Without ","")} would add ${fR(best.rw - c.rw)}, which is left alone rather than fitted in-sample. Split-sample: screening on 2004–13 only would have admitted ${sp.added.length ? sp.added.join(" and ") : "nothing extra"}${sp.dropped.length ? " and missed " + sp.dropped.join(", ") : ""}. On 2014–${M.asof.slice(2,4)} data that early-chosen composite scores ${fR(sp.early_oos.rw)} weekly and ${fR(sp.early_oos.r13)} at 13 weeks, against ${fR(sp.full_oos.rw)} and ${fR(sp.full_oos.r13)} for the full-sample selection. Member selection is the main in-sample element here, and it costs little.`;
})();
register($("cLegs"), () => {
  const rows = DATA.table.filter(r => r.key !== "HYG_SHY");
  const o = baseOpts(); o.interaction = {mode: "nearest", intersect: true};
  o.scales = {x: yAxis({type: "linear", min: -0.4, max: 0.5, title: {display: true, text: "ρ with Δ real yield", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: Object.assign(yAxis().ticks, {stepSize: 0.2, callback: v => minus(v.toFixed(1))})}),
              y: yAxis({min: -0.3, max: 1, title: {display: true, text: "ρ with Δ breakeven", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: Object.assign(yAxis().ticks, {stepSize: 0.25, callback: v => minus(v.toFixed(2))})})};
  o.plugins.tooltip.callbacks = {label: c => { const r = rows[c.dataIndex]; return ` ${r.label}: real ${fR(r.rreal)}, breakeven ${fR(r.rbei)}`; }};
  const lab = {id: "lab", afterDatasetsDraw(ch){
    const ctx = ch.ctx; const meta = ch.getDatasetMeta(0); ctx.save(); ctx.font = "11px 'IBM Plex Sans', sans-serif"; ctx.textBaseline = "middle";
    const xs = ch.scales.x, ys = ch.scales.y; ctx.strokeStyle = T.muted; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(xs.getPixelForValue(0), ys.top); ctx.lineTo(xs.getPixelForValue(0), ys.bottom); ctx.moveTo(xs.left, ys.getPixelForValue(0)); ctx.lineTo(xs.right, ys.getPixelForValue(0)); ctx.stroke(); ctx.setLineDash([]);
    const placed = meta.data.map(p => ({x0: p.x-5, y0: p.y-5, x1: p.x+5, y1: p.y+5}));
    const hit = bx => placed.some(q => !(bx.x1 < q.x0 || bx.x0 > q.x1 || bx.y1 < q.y0 || bx.y0 > q.y1));
    const order = meta.data.map((p,i) => i).sort((i,j) => (rows[i].verdict === "Included" ? 0 : 1) - (rows[j].verdict === "Included" ? 0 : 1));
    order.forEach(i => { const p = meta.data[i], r = rows[i];
      if (r.verdict !== "Included" && !["DXY","HYG_IEF","GOLD","VAL_GRO","SPY"].includes(r.key)) return;
      const t = r.label.replace(" stocks","").replace("Regional banks","Reg. banks").replace("High yield / 7-10y Treasuries","HYG / IEF").replace("US dollar index","DXY");
      const w = ctx.measureText(t).width, h = 12;
      const cand = [[7, 0], [-7 - w, 0], [7, -11], [7, 11], [-7 - w, -11], [-7 - w, 11], [7, -22], [7, 22], [-7-w, 22], [-7-w, -22]];
      for (const [dx, dy] of cand){
        const bx = {x0: p.x + dx, y0: p.y + dy - h/2, x1: p.x + dx + w, y1: p.y + dy + h/2};
        if (bx.x0 < ch.chartArea.left || bx.x1 > ch.chartArea.right || bx.y0 < ch.chartArea.top || bx.y1 > ch.chartArea.bottom) continue;
        if (hit(bx)) continue;
        placed.push(bx); ctx.fillStyle = r.key === "DXY" ? T.orange : (r.verdict === "Included" ? T.ink : T.muted);
        if (Math.abs(dy) > 1){ ctx.strokeStyle = T.rule; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(dx > 0 ? bx.x0 : bx.x1, p.y + dy); ctx.stroke(); }
        ctx.fillText(t, bx.x0, p.y + dy); break;
      }
    });
    ctx.restore(); }};
  return {type: "scatter", data: {datasets: [{data: rows.map(r => ({x: r.rreal, y: r.rbei})),
    pointBackgroundColor: rows.map(r => r.key === "DXY" ? T.orange : r.verdict === "Included" ? T.teal : "transparent"),
    pointBorderColor: rows.map(r => r.key === "DXY" ? T.orange : r.verdict === "Included" ? T.teal : T.muted), pointRadius: 4.5, pointHoverRadius: 6}]}, options: o, plugins: [lab]};
}, false);
(function legsNote(){
  const dxy = byKey.DXY, uj = byKey.USDJPY, tip = byKey.TIP_IEF;
  const dollarBit = dxy ? ` The dollar index loads ${fR(dxy.rreal)} on real yields but ${fR(dxy.rbei)} on breakevens, so on the nominal yield the two legs cancel; that is why it fails the screen despite a genuine rate-differential link.` : "";
  $("legsNote").textContent = `Most internals price the breakeven leg. USD/JPY is the main real-yield member (${fR(uj.rreal)} real, ${fR(uj.rbei)} breakeven).${dollarBit} TIPS / Treasuries at ${fR(tip.rbei)} is close to the breakeven itself.`;
})();
function cmat(){
  const K = DATA.cmat.keys, Mx = DATA.cmat.m; const t = $("cmatTable");
  const short = k => byKey[k].label.replace(" stocks","").replace("Regional banks","Reg. banks").replace("Cyclicals / Defensives","Cyc / Def").replace("Utilities / S&P 500","Utils / SPX (inv)").replace("TIPS / Treasuries","TIPS / UST").replace("Inflation / Deflation","Infl / Defl");
  let h = `<thead><tr><th></th>${K.map(k => `<th class="n" style="white-space:normal;min-width:62px">${short(k)}</th>`).join("")}</tr></thead><tbody>`;
  K.forEach((k,i) => {
    h += `<tr><td style="white-space:nowrap">${short(k)}</td>${K.map((j,jj) => { const v = Mx[i][jj]; const a = i===jj ? 0 : Math.min(1, Math.abs(v)/0.8)*0.5; const c = v >= 0 ? T.teal : T.orange;
      return `<td class="n" style="background:${i===jj ? T.soft : rgba(c, a)}">${i===jj ? "" : fR(v)}</td>`; }).join("")}</tr>`;
  });
  t.innerHTML = h + "</tbody>";
}
cmat();

// ---------------------------------------------------------------- method
(function method(){
  const ys = M.ysrc || {};
  const usedTiingo = Object.values(M.src || {}).includes("Tiingo");
  const proxyNames = {"HG=F":"copper via CPER", "GC=F":"gold via GLD", "CL=F":"crude via USO", "SI=F":"silver via SLV", "DX-Y.NYB":"the dollar via the Fed broad USD index"};
  const activeProxies = Object.keys(M.proxied || {}).filter(k => (M.src || {})[k] && (M.src || {})[k] !== "Yahoo").map(k => proxyNames[k] || k);
  const srcLine = usedTiingo
    ? `Equity and style ETFs come from Tiingo. The two yen crosses and the dollar come from FRED daily reference rates (one business-day lag; USD/JPY tracks the market close at a 0.86 weekly-change correlation, AUD/JPY at 0.91, and is rebuilt as USD/AUD times USD/JPY). Copper, gold, crude and silver use their liquid ETFs (CPER, GLD, USO, SLV), which track the front-month future at a 0.92 to 0.98 weekly-change correlation. The model is built on log-change ratios, where these proxies are faithful.`
    : `Prices are Yahoo Finance adjusted closes; FX and futures are dated to the exchange-local session.`;
  const src = ys.DGS10 === "FRED"
    ? `FRED DGS10, DFII10 and T10YIE (fallback: Treasury.gov was unreachable at build time)`
    : `Treasury.gov daily par and real yield curves, 10-year column. FRED republishes these as DGS10 and DFII10 a business day later, so Treasury is used first; the breakeven is nominal minus real, as in FRED's T10YIE`;
  const P = [
    `<b>Target.</b> Weekly change in the 10-year constant-maturity Treasury yield, in basis points, Friday to Friday (last available day in the week), ${M.start.slice(0,4)} to ${dLong(M.asof)}. Source: ${src}. The final week is truncated to the last date on which every member and the yield both printed.`,
    `<b>Internals.</b> Weekly log change of each ratio. Baskets are equal-weight averages of member ETF log changes, on total-return adjusted closes, so dividend gaps between utilities and the market do not leak into the ratios. ${srcLine}`,
    `<b>Signs are set before looking.</b> Each internal carries an economic prior: cyclicals, banks, breakevens, industrial commodities, and a weaker yen go with higher yields; utilities, homebuilders, tech and gold go the other way. The screen checks the data agrees; it does not choose signs.`,
    `<b>Composite.</b> Each member's weekly change is sign-aligned and divided by its trailing 52-week standard deviation, lagged one week, then clipped at ±4. Members are averaged within pillars and pillars averaged equally, so the three equity ratios do not outvote the two FX crosses. Weights and scaling are point-in-time; nothing is fitted to the target.`,
    `<b>Implied move and gap.</b> The 13-week composite sum is converted to basis points with a no-intercept beta estimated on the trailing 156 weeks of 13-week changes and lagged one week (latest ${L.beta.toFixed(1)}bp per unit). The gap is the actual 13-week change minus that implied change, and its z-score uses the trailing 156-week standard deviation.`,
    `<b>What is in-sample.</b> Membership comes from a full-sample screen, so the coincident fit on this page is partly in-sample. The split-sample check selects on 2004–13 only and scores on 2014 onward. Membership is frozen in the build script; each rebuild re-runs the screen and flags drift rather than silently changing the index.`,
    `<b>TIPS / Treasuries is semi-mechanical.</b> Nominal yield equals real yield plus breakeven, and the TIP/IEF ratio is close to a pure breakeven position (empirical duration to nominal yields ${M.dur.TIP.toFixed(1)} for TIP against ${M.dur.IEF.toFixed(1)} for IEF). It is kept because it was requested and it is market-priced, and the robustness table shows the composite without it.`,
    `<b>Credit was tested and rejected.</b> HYG's empirical rate duration is ${fR(M.dur.HYG,1)}: spread compression when yields rise offsets its carry duration. HYG/IEF therefore correlates strongly with yields purely through the IEF leg. Matched against 1–3 year Treasuries the signal falls to ${fR(byKey.HYG_SHY.rw)} and has been wrong-signed since 2020.`,
    `<b>Regimes.</b> 2004–08 (pre-crisis and crisis), 2009–13 (QE), 2014–19 (normalisation), 2020–22 (pandemic and the inflation shock, which flipped the stock–bond correlation), 2023 onward (restrictive policy and term premium).`,
    `<b>Data caveats.</b> ${usedTiingo ? "On the automated build, commodities are their liquid ETFs (CPER, GLD, USO, SLV): these track the front-month futures closely on weekly changes but CPER only begins in 2011 and USO in 2006, so the commodity ratios use less pre-crisis history than the rest. The dollar internal, which is excluded from the composite and shown for context, uses the Fed broad USD index rather than ICE DXY." : "Copper, gold, silver and crude are continuous front-month futures, so roll gaps add noise to weekly changes; weekly sampling avoids the negative WTI print of April 2020."} KRE starts June 2006 and HYG April 2007; regime statistics use whatever history exists in the window. The composite starts ${dMon(M.first_comp)} after a 26-week volatility warm-up, ${M.n_weeks} weeks in all.`,
  ];
  $("methodCols").innerHTML = P.map(p => `<p>${p}</p>`).join("");
  const srcCounts = {}; Object.values(M.src || {}).forEach(v => srcCounts[v] = (srcCounts[v]||0)+1);
  const srcSummary = Object.entries(srcCounts).map(([k,v]) => `${v}× ${k}`).join(", ");
  $("foot").innerHTML = `Acheron Insights. Built ${M.built} by rebuild.py${M.run_url ? `, <a href="${M.run_url}">build log</a>` : ""}. Sources: ${srcSummary}, yields from ${ys.DGS10}. Readings also at <a href="latest.json">latest.json</a>.`;
})();

// ---------------------------------------------------------------- theme changes
window.addEventListener("beforeprint", () => REG.forEach(r => { if (!r.visible){ r.visible = true; render(r); } }));
function onTheme(){ readTokens(); paintLegends(); screenTable(); cmat(); drawHeat(null); rerender(() => true); }
if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", onTheme);
new MutationObserver(onTheme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
})();
</script>
</body>
</html>
'''

def cli():
    global CACHE, TTL_HOURS
    ap = argparse.ArgumentParser(description="Rebuild the US 10y yield market internals dashboard")
    ap.add_argument("--out", default=os.path.join(HERE, "site"), help="output directory (index.html, latest.json)")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"), help="download cache directory")
    ap.add_argument("--ttl-hours", type=float, default=10, help="re-download cached files older than this")
    ap.add_argument("--max-age-days", type=int, default=7, help="exit 2 if the as-of date is older than this (calendar days, US/Eastern)")
    ap.add_argument("--template", default=None, help="optional external template.html (development)")
    args = ap.parse_args()
    CACHE, TTL_HOURS = os.path.abspath(args.cache), args.ttl_hours
    os.makedirs(CACHE, exist_ok=True); os.makedirs(args.out, exist_ok=True)

    payload = clean(main())
    tpl = open(args.template).read() if args.template else (TEMPLATE or open(os.path.join(HERE, "template.html")).read())
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html, n1 = re.subn(r"/\*__DATA__\*/null", lambda m: data, tpl)
    html, n2 = re.subn(r"/\*__FONTS__\*/", lambda m: font_css(), html)
    assert n1 == 1 and n2 == 1, (n1, n2)
    open(os.path.join(args.out, "index.html"), "w").write(html)
    j = latest_json(payload)
    json.dump(j, open(os.path.join(args.out, "latest.json"), "w"), indent=1)
    open(os.path.join(args.out, ".nojekyll"), "w").write("")
    log(f"wrote {args.out}/index.html ({len(html)/1024:.0f} KB) and latest.json")

    problems = sanity(payload)
    today_et = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    age = (today_et - pd.Timestamp(payload["meta"]["asof"])).days
    if problems:
        status = "sanity check failed"; code = 3
        for pr in problems: log("SANITY:", pr)
    elif age > args.max_age_days:
        status = f"stale: as-of is {age} days old"; code = 2
        log("STALE:", status)
    else:
        status = "ok" if not STALE else f"ok, {len(STALE)} ticker(s) served from cache"; code = 0
    if STALE: log("stale tickers:", STALE)
    step_summary(j, status)
    log("status:", status)
    return code

if __name__ == "__main__":
    sys.exit(cli())
