"""
Rule #1 screener — builds site/data.json for the dashboard.

What it does, in order:
  1. Gets the list of companies to screen (by default every company listed on a major
     US exchange — NYSE, Nasdaq, Cboe — plus the S&P 500 and watchlist.txt), and each
     company's SEC industry (SIC) code.
  2. Downloads up to ~10+ years of annual financials for each from SEC EDGAR (free).
  3. Works out Phil Town's "Big Five" (ROIC, sales, EPS, equity, free cash flow growth),
     the debt test, and a few management signals.
  4. Downloads daily prices (Yahoo Finance via yfinance, Stooq as backup) and
     Town's three timing "tools" (10-day MA, MACD 8-17-9, stochastic 14-5).
  5. Downloads macro context series from FRED (no key needed).
  6. Writes everything to site/data.json. Sticker Price is calculated in the page,
     so you can change the assumptions without re-running anything.

Settings come from environment variables (set in the GitHub workflow):
  SEC_CONTACT  your email address — the SEC requires one in every request (required)
  UNIVERSE     comma-separated list of: sp500, sp400, sp600, sp1500 (= all three),
               nasdaq, nyse, cboe, us (= nasdaq + nyse + cboe), otc (over-the-counter;
               thousands of tiny companies with patchy data, not recommended).
               Default "sp500,us".
"""

import datetime as dt
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import numpy as np
import pandas as pd
import requests

SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
UNIVERSE = os.environ.get("UNIVERSE", "").strip().lower() or "sp500,us"
OUT_DIR = os.environ.get("OUT_DIR", "site")
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
SEC_RATE = 8          # requests per second, kept under the SEC's limit of 10
SEC_WORKERS = 6       # parallel downloads
SIC_MAX_AGE_DAYS = 90 # re-check a company's industry code this often

WIKI = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# XBRL tags tried in order (first one that has a value for a year wins).
TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
                "InterestAndDividendIncomeOperating"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements", "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligationsNoncurrent", "LongTermNotesPayable",
                "SeniorLongTermNotes"],
    "op_income": ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
               "WeightedAverageNumberOfSharesOutstandingBasic"],
}
INSTANT = {"equity", "lt_debt"}          # balance-sheet values (a point in time)
UNITS = {"eps": ("USD/shares",), "shares": ("shares",)}

FRED_SERIES = {
    "DGS10": "10-year Treasury yield (%)",
    "FEDFUNDS": "Fed funds rate (%)",
    "T10Y2Y": "10y minus 2y Treasury spread (pts)",
    "CPIAUCSL": "CPI inflation, year on year (%)",
    "UNRATE": "Unemployment rate (%)",
    "BAMLH0A0HYM2": "High-yield credit spread (pts)",
}


# --------------------------------------------------------------------------- helpers

def log(*a):
    print(*a, flush=True)


def make_session(user_agent):
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
    return s


class RateLimiter:
    """Spaces out request starts across threads so we never exceed `per_sec`."""

    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


SEC_LIMIT = RateLimiter(SEC_RATE)


def get(session, url, tries=3, timeout=30, limiter=None):
    for i in range(tries):
        if limiter:
            limiter.wait()
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            log(f"  HTTP {r.status_code} for {url}")
            if r.status_code in (403, 429):
                time.sleep(10)  # SEC is asking us to slow down
        except requests.RequestException as e:
            log(f"  {type(e).__name__} for {url}")
        time.sleep(1.5 * (i + 1))
    return None


def norm_ticker(t):
    return str(t).strip().upper().replace(".", "-").replace("/", "-")


def num(x, nd=4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


# --------------------------------------------------------------------------- universe

def find_col(df, *needles):
    for n in needles:
        for c in df.columns:
            if n in str(c).lower():
                return c
    return None


def wiki_table(session, url):
    r = get(session, url)
    if r is None:
        return None
    for t in pd.read_html(StringIO(r.text)):
        sym = find_col(t, "symbol", "ticker")
        sec = find_col(t, "gics sector", "sector")
        if sym is not None and sec is not None and len(t) > 100:
            return t
    return None


def standardise(t, index_name):
    sym = find_col(t, "symbol", "ticker")
    name = find_col(t, "security", "company")
    sec = find_col(t, "gics sector", "sector")
    sub = find_col(t, "sub-industry", "sub industry", "industry")
    out = pd.DataFrame({
        "ticker": t[sym].map(norm_ticker),
        "name": t[name].astype(str) if name is not None else "",
        "sector": t[sec].astype(str),
        "sub": t[sub].astype(str) if sub is not None else "",
    })
    out["index"] = index_name
    return out


def universe_parts():
    toks = {t.strip() for t in UNIVERSE.split(",") if t.strip()}
    if "sp1500" in toks:
        toks |= {"sp500", "sp400", "sp600"}
    if "us" in toks:
        toks |= {"nasdaq", "nyse", "cboe"}
    return toks


def get_index_lists(wiki_session, toks):
    """S&P index members from Wikipedia (gives GICS sector / sub-industry)."""
    parts = []
    if "sp500" in toks:
        t = wiki_table(wiki_session, WIKI["sp500"])
        if t is None:
            log("Wikipedia S&P 500 list unavailable, using backup CSV")
            r = get(wiki_session, SP500_CSV)
            if r is not None:
                t = pd.read_csv(StringIO(r.text))
        if t is not None:
            parts.append(standardise(t, "S&P 500"))
        else:
            log("Could not load the S&P 500 list")
    for key, label in (("sp400", "S&P 400"), ("sp600", "S&P 600")):
        if key in toks:
            t = wiki_table(wiki_session, WIKI[key])
            if t is not None:
                parts.append(standardise(t, label))
            else:
                log(f"Could not load {label} list")
    return parts


SUFFIX_JUNK = ("W", "WS", "U", "R", "RT")  # warrants, units, rights


EXCHANGES = {"nasdaq": "Nasdaq", "nyse": "NYSE", "cboe": "Cboe", "otc": "OTC"}


def exchange_of(exch):
    e = (exch or "").strip().lower()
    for key in EXCHANGES:
        if e.startswith(key):  # e.g. "NYSE", "NYSE American", "NYSE Arca"
            return key
    return None


def exchange_list(exch_rows, key):
    """One row per company listed on an exchange, from the SEC's ticker/exchange file.
    Companies with several tickers (share classes, warrants, units) keep the simplest one."""
    label = EXCHANGES[key]
    by_cik = {}
    for cik, name, ticker, exch in exch_rows:
        if not ticker or exchange_of(exch) != key:
            continue
        t = norm_ticker(ticker)
        best = by_cik.get(cik)
        junk = len(t) == 5 and t.endswith(SUFFIX_JUNK)
        rank = (junk, "-" in t, len(t), t)
        if best is None or rank < best[0]:
            by_cik[cik] = (rank, t, name)
    rows = [{"ticker": t, "name": name, "sector": "", "sub": "", "index": label}
            for _, t, name in by_cik.values()]
    return pd.DataFrame(rows, columns=["ticker", "name", "sector", "sub", "index"])


def read_watchlist(path="watchlist.txt"):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                out.append(norm_ticker(line))
    return out


def sec_ticker_map(sec):
    """Ticker -> (CIK, name) for every SEC filer, plus the raw rows with each ticker's exchange."""
    rows = []
    r = get(sec, "https://www.sec.gov/files/company_tickers_exchange.json", limiter=SEC_LIMIT)
    if r is not None:
        j = r.json()
        f = j["fields"]
        ci, ni, ti, ei = f.index("cik"), f.index("name"), f.index("ticker"), f.index("exchange")
        rows = [(int(d[ci]), d[ni], d[ti], d[ei]) for d in j["data"]]
    else:
        log("SEC ticker/exchange file unavailable; exchange lists can't be built this run")
        r = get(sec, "https://www.sec.gov/files/company_tickers.json", limiter=SEC_LIMIT)
        if r is None:
            raise SystemExit("Could not download the SEC ticker list. Check SEC_CONTACT is a real email.")
        rows = [(int(v["cik_str"]), v["title"], v["ticker"], None) for v in r.json().values()]
    m = {}
    for cik, name, ticker, _ in rows:
        if ticker:
            m.setdefault(norm_ticker(ticker), (cik, name))
    return m, rows


# --------------------------------------------------------------------------- industry (SIC)

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def get_sic(sec, cik, cache):
    """SEC industry code + description for a company, cached between runs."""
    key = str(cik)
    hit = cache.get(key)
    today = dt.date.today()
    if hit and (today - dt.date.fromisoformat(hit[2])).days < SIC_MAX_AGE_DAYS:
        return hit[0], hit[1]
    r = get(sec, f"https://data.sec.gov/submissions/CIK{cik:010d}.json", limiter=SEC_LIMIT)
    if r is None:
        return (hit[0], hit[1]) if hit else (None, None)
    j = r.json()
    try:
        code = int(j.get("sic") or 0) or None
    except ValueError:
        code = None
    desc = j.get("sicDescription") or None
    cache[key] = [code, desc, today.isoformat()]
    return code, desc


SMALL = {"and", "of", "the", "for", "in", "on", "or", "to", "a", "with", "nec"}


def clean_sic(desc):
    if not desc:
        return "Unclassified"
    d = desc.strip()
    if d.upper().startswith("SERVICES-"):
        d = d[9:]
    words = []
    for i, w in enumerate(d.replace("-", " - ").split()):
        lw = w.lower()
        if lw == "nec":
            words.append("(NEC)")
        elif i and lw in SMALL:
            words.append(lw)
        else:
            words.append(lw[:1].upper() + lw[1:])
    out = " ".join(words).replace(" - ", " – ").replace("( ", "(")
    return out.replace("&amp;", "&")


def sic_sector(sic):
    """Map an SEC SIC code to the closest of the 11 GICS sectors."""
    if not sic:
        return "Other"
    s = int(sic)
    if s < 1000: return "Consumer Staples"
    if 1300 <= s <= 1399: return "Energy"
    if s < 1500: return "Materials"
    if s == 1531: return "Consumer Discretionary"
    if s < 1800: return "Industrials"
    if s < 2200: return "Consumer Staples"
    if s < 2400: return "Consumer Discretionary"
    if s < 2500: return "Materials"
    if s < 2600: return "Consumer Discretionary"
    if s < 2700: return "Materials"
    if s < 2800: return "Communication Services"
    if 2830 <= s <= 2839: return "Health Care"
    if 2840 <= s <= 2844: return "Consumer Staples"
    if s < 2900: return "Materials"
    if s < 3000: return "Energy"
    if s == 3021 or 3100 <= s < 3200: return "Consumer Discretionary"
    if s < 3400: return "Materials"
    if 3570 <= s <= 3579: return "Information Technology"
    if s < 3600: return "Industrials"
    if 3630 <= s <= 3639 or s == 3651: return "Consumer Discretionary"
    if 3660 <= s <= 3699: return "Information Technology"
    if s < 3700: return "Industrials"
    if 3711 <= s <= 3716 or s == 3751: return "Consumer Discretionary"
    if s < 3800: return "Industrials"
    if 3840 <= s <= 3851: return "Health Care"
    if s < 3900: return "Information Technology"
    if s < 4000: return "Consumer Discretionary"
    if s < 4800: return "Industrials"
    if s < 4900: return "Communication Services"
    if s < 5000: return "Utilities"
    if s == 5122: return "Health Care"
    if 5140 <= s <= 5149: return "Consumer Staples"
    if s < 5200: return "Industrials"
    if 5400 <= s <= 5499 or s in (5331, 5912): return "Consumer Staples"
    if s < 6000: return "Consumer Discretionary"
    if 6500 <= s <= 6599 or s == 6798: return "Real Estate"
    if s < 7000: return "Financials"
    if 7370 <= s <= 7379: return "Information Technology"
    if 7300 <= s < 7400: return "Industrials"
    if 7800 <= s < 7990: return "Communication Services"
    if s < 8000: return "Consumer Discretionary"
    if s < 8100: return "Health Care"
    if 8200 <= s < 8300: return "Consumer Discretionary"
    if s == 8731: return "Health Care"
    if s < 9000: return "Industrials"
    return "Other"


# --------------------------------------------------------------------------- SEC facts

def parse_date(s):
    return dt.date.fromisoformat(s)


def fy_label(end):
    """Fiscal year label from the period end date (Jan–Mar year ends count as the prior year)."""
    return end.year - 1 if end.month <= 3 else end.year


def annual_series(facts, key):
    """{fiscal_year: value} from 10-K style filings, trying each tag in order."""
    tags, instant = TAGS[key], key in INSTANT
    units_pref = UNITS.get(key, ("USD",))
    out, out_end = {}, {}
    for tag in tags:
        node = None
        for ns in ("us-gaap", "ifrs-full"):
            node = facts.get(ns, {}).get(tag)
            if node:
                break
        if not node:
            continue
        for u in units_pref:
            entries = node.get("units", {}).get(u)
            if not entries:
                continue
            best = {}
            for e in entries:
                form = e.get("form", "")
                if not form.startswith(("10-K", "20-F", "40-F", "10-KT")):
                    continue
                end_s = e.get("end")
                if not end_s or e.get("val") is None:
                    continue
                end = parse_date(end_s)
                if instant:
                    if e.get("start"):
                        continue
                else:
                    if not e.get("start"):
                        continue
                    days = (end - parse_date(e["start"])).days
                    if not 330 <= days <= 400:
                        continue
                fy = fy_label(end)
                filed = e.get("filed", "")
                prev = best.get(fy)
                # keep the latest-ending period for that FY, then the most recent filing
                if prev is None or (end, filed) > (prev[0], prev[1]):
                    best[fy] = (end, filed, e["val"])
            for fy, (end, _, v) in best.items():
                if fy not in out:
                    out[fy] = float(v)
                    out_end[fy] = end
    return out, out_end


def cagr(series, years, latest, n, min_span):
    """Compound growth from (latest - n) to latest, falling back to the earliest year
    if the full window isn't available but at least min_span years are."""
    if latest not in series:
        return None, 0
    start = latest - n
    if start not in series:
        avail = [y for y in years if y in series and latest - y >= min_span and y > latest - n]
        if not avail:
            return None, 0
        start = min(avail)
    a, b = series[start], series[latest]
    span = latest - start
    if span <= 0 or a is None or b is None or a <= 0 or b <= 0:
        return None, span
    return (b / a) ** (1.0 / span) - 1.0, span


def avg_window(series, latest, n):
    vals = [series[y] for y in range(latest - n + 1, latest + 1) if series.get(y) is not None]
    if not vals:
        return None
    return float(np.mean(vals))


def window_score(r):
    if r is None:
        return 0.0
    if r >= 0.10:
        return 1.0
    if r >= 0.05:
        return 0.5
    return 0.0


def analyse(facts):
    s, ends = {}, {}
    for key in TAGS:
        s[key], ends[key] = annual_series(facts, key)

    anchor = s["revenue"] or s["net_income"]
    if not anchor:
        return None, "no annual revenue or earnings found"
    latest = max(anchor)
    latest_end = (ends["revenue"] or ends["net_income"]).get(latest)
    if latest_end and (dt.date.today() - latest_end).days > 600:
        return None, "no annual report in the last ~18 months"

    years = list(range(latest - 10, latest + 1))

    # Book value per share (preferred) or total equity
    bvps = {}
    for y in years:
        e, sh = s["equity"].get(y), s["shares"].get(y)
        if e is not None and sh:
            bvps[y] = e / sh
    use_bvps = sum(1 for y in years if y in bvps) >= 3
    equity_g = bvps if use_bvps else s["equity"]

    # Free cash flow
    fcf = {}
    for y in years:
        o = s["ocf"].get(y)
        if o is not None:
            fcf[y] = o - abs(s["capex"].get(y, 0.0) or 0.0)

    # ROIC = after-tax operating profit / (equity + long-term debt)
    roic = {}
    for y in years:
        eq = s["equity"].get(y)
        if eq is None:
            continue
        ic = eq + (s["lt_debt"].get(y) or 0.0)
        if ic <= 0:
            continue
        op = s["op_income"].get(y)
        if op is not None:
            pt, tx = s["pretax"].get(y), s["tax"].get(y)
            rate = (tx / pt) if (pt and tx is not None and pt > 0) else 0.21
            rate = min(max(rate, 0.0), 0.35)
            nopat = op * (1 - rate)
        else:
            nopat = s["net_income"].get(y)
        if nopat is not None:
            roic[y] = nopat / ic

    growth = {}
    big5 = {}
    for key, series in (("rev", s["revenue"]), ("eps", s["eps"]), ("eq", equity_g), ("fcf", fcf)):
        g10, span10 = cagr(series, years, latest, 10, 5)
        g5, _ = cagr(series, years, latest, 5, 3)
        g1, _ = cagr(series, years, latest, 1, 1)
        yoy = [(series[y] / series[y - 1] - 1) for y in years
               if y in series and (y - 1) in series and series[y - 1] and series[y - 1] > 0]
        consistency = (sum(1 for v in yoy if v > 0) / len(yoy)) if yoy else None
        growth[key] = {"10": num(g10), "5": num(g5), "1": num(g1), "span": span10,
                       "consistency": num(consistency, 2)}
        big5[key] = 0.4 * window_score(g10) + 0.4 * window_score(g5) + 0.2 * window_score(g1)

    r10, r5, r1 = avg_window(roic, latest, 10), avg_window(roic, latest, 5), roic.get(latest)
    growth["roic"] = {"10": num(r10), "5": num(r5), "1": num(r1),
                      "span": sum(1 for y in years if y in roic), "consistency": None}
    big5["roic"] = 0.4 * window_score(r10) + 0.4 * window_score(r5) + 0.2 * window_score(r1)

    score = round(100 * float(np.mean(list(big5.values()))), 1)
    passes = {k: (growth[k]["10"] is not None and growth[k]["10"] >= 0.10 and
                  growth[k]["5"] is not None and growth[k]["5"] >= 0.10)
              for k in ("roic", "rev", "eps", "eq", "fcf")}

    # Debt test: years of current free cash flow to repay long-term debt
    lt = s["lt_debt"].get(latest)
    f_latest = fcf.get(latest)
    if lt is None:
        debt_years = None
    elif lt <= 0:
        debt_years = 0.0
    elif f_latest and f_latest > 0:
        debt_years = lt / f_latest
    else:
        debt_years = 99.0

    sh_now, sh_5 = s["shares"].get(latest), s["shares"].get(latest - 5)
    shares_chg5 = (sh_now / sh_5 - 1) if (sh_now and sh_5) else None

    flags = []
    if s["equity"].get(latest) is not None and s["equity"][latest] <= 0:
        flags.append("Negative equity (often heavy buybacks) — equity growth can't be measured")
    if not s["capex"]:
        flags.append("Capex not reported in a standard way — FCF may be overstated")
    if sum(1 for y in years if y in s["revenue"]) < 6:
        flags.append("Less than 6 years of data")
    if growth["rev"]["1"] is not None and growth["rev"]["1"] < 0:
        flags.append("Sales fell last year")

    def row(series, nd=4):
        return [num(series.get(y), nd) for y in years]

    result = {
        "fy": latest,
        "years": years,
        "series": {
            "rev": row(s["revenue"], 0),
            "eps": row(s["eps"], 3),
            "eq": row(equity_g, 3),
            "fcf": row(fcf, 0),
            "roic": row(roic, 4),
        },
        "eqBasis": "book value per share" if use_bvps else "total equity",
        "g": growth,
        "big5": score,
        "passes": passes,
        "eps": num(s["eps"].get(latest), 3),
        "ltDebt": num(lt, 0),
        "debtYears": num(debt_years, 2),
        "sharesChg5": num(shares_chg5),
        "shares": num(sh_now, 0),
        "flags": flags,
    }
    return result, None


# --------------------------------------------------------------------------- prices & tools

def tools_from_ohlc(df):
    """Phil Town's three tools on daily bars: 10-day MA, MACD(8,17,9), stochastic(14,5)."""
    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        return None
    c, h, l = df["Close"], df["High"].fillna(df["Close"]), df["Low"].fillna(df["Close"])
    ma10 = c.rolling(10).mean()
    macd = c.ewm(span=8, adjust=False).mean() - c.ewm(span=17, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    k = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)
    d = k.rolling(5).mean()
    last = df.index[-1]
    return {
        "price": num(c.iloc[-1], 2),
        "date": pd.Timestamp(last).strftime("%Y-%m-%d"),
        "ma": bool(c.iloc[-1] > ma10.iloc[-1]),
        "macd": bool(macd.iloc[-1] > sig.iloc[-1]),
        "stoch": bool(k.iloc[-1] > d.iloc[-1]) if not math.isnan(d.iloc[-1]) else None,
        "chg1y": num(c.iloc[-1] / c.iloc[0] - 1),
        "spark": [num(v, 2) for v in c.iloc[-130::5].tolist()],
    }


def prices_yahoo(tickers):
    out = {}
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance not installed")
        return out
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            data = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            log(f"  yfinance chunk failed: {e}")
            continue
        for t in chunk:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                res = tools_from_ohlc(df)
                if res:
                    out[t] = res
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1)
    return out


def prices_stooq(session, tickers, limit=600):
    out = {}
    if len(tickers) > limit:
        log(f"  only trying the first {limit} on Stooq")
    for t in tickers[:limit]:
        r = get(session, f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d", tries=2)
        if r is None or "Date" not in r.text[:50]:
            continue
        try:
            df = pd.read_csv(StringIO(r.text), parse_dates=["Date"]).set_index("Date").tail(260)
            res = tools_from_ohlc(df)
            if res:
                out[t] = res
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return out


# --------------------------------------------------------------------------- macro

def macro(session):
    out = []
    for sid, label in FRED_SERIES.items():
        r = get(session, f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", tries=2)
        if r is None:
            continue
        try:
            df = pd.read_csv(StringIO(r.text))
            df.columns = ["date", "v"]
            df["v"] = pd.to_numeric(df["v"], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df = df.dropna()
            if sid == "CPIAUCSL":
                df = df.set_index("date")["v"]
                df = (df / df.shift(12) - 1).dropna() * 100
                df = df.reset_index()
                df.columns = ["date", "v"]
            m = df.set_index("date")["v"].resample("ME").last().dropna()
            m = m[m.index >= m.index[-1] - pd.DateOffset(years=5)]
            last_date = df["date"].iloc[-1]
            yr_ago = df[df["date"] <= last_date - pd.DateOffset(years=1)]
            out.append({
                "id": sid, "label": label,
                "value": num(df["v"].iloc[-1], 2),
                "date": last_date.strftime("%Y-%m-%d"),
                "yearAgo": num(yr_ago["v"].iloc[-1], 2) if len(yr_ago) else None,
                "hist": [num(v, 2) for v in m.tolist()],
            })
        except Exception as e:  # noqa: BLE001
            log(f"  macro {sid} failed: {e}")
    return out


# --------------------------------------------------------------------------- main

def build_universe(web, sec_rows, tick_map):
    """One row per company (by SEC CIK), merging S&P index membership and exchange listings."""
    toks = universe_parts()
    parts = get_index_lists(web, toks)
    for key in EXCHANGES:
        if key in toks:
            lst = exchange_list(sec_rows, key)
            log(f"{EXCHANGES[key]}-listed companies: {len(lst)}")
            parts.append(lst)
    if not parts:
        raise SystemExit("Could not load any list of companies to screen.")
    uni = pd.concat(parts, ignore_index=True)

    companies, skipped = {}, []
    for row in uni.itertuples(index=False):
        t = row.ticker
        hit = tick_map.get(t) or tick_map.get(t.replace("-", ""))
        if not hit:
            skipped.append({"t": t, "why": "no SEC CIK for this ticker"})
            continue
        cik, title = hit
        c = companies.get(cik)
        if c is None:
            companies[cik] = {"t": t, "cik": cik, "name": row.name or title, "gicsSector": row.sector,
                              "gics": row.sub, "lists": [row.index]}
        else:
            if row.index not in c["lists"]:
                c["lists"].append(row.index)
            if not c["gicsSector"] and row.sector:  # prefer the S&P entry's details
                c.update(t=t, name=row.name or c["name"], gicsSector=row.sector, gics=row.sub)
    return companies, skipped


_local = threading.local()


def screen_one(c, sic_cache):
    sec = getattr(_local, "sec", None)
    if sec is None:
        sec = _local.sec = make_session(f"Rule1Screener {SEC_CONTACT}")
    cik = c["cik"]
    r = get(sec, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", timeout=60,
            limiter=SEC_LIMIT)
    if r is None:
        return None, "SEC data not available"
    try:
        res, why = analyse(r.json().get("facts", {}))
    except Exception as e:  # noqa: BLE001
        return None, f"could not parse ({type(e).__name__})"
    if res is None:
        return None, why
    sic, desc = get_sic(sec, cik, sic_cache)
    if sic == 6770:
        return None, "blank-cheque company (SPAC)"
    res.update({
        "t": c["t"], "name": c["name"], "cik": cik, "watch": c.get("watch", False),
        "sector": c["gicsSector"] or sic_sector(sic),
        "sub": clean_sic(desc), "sic": sic, "gics": c["gics"] or None,
        "lists": c["lists"], "index": " · ".join(c["lists"]),
    })
    return res, None


def main():
    if "@" not in SEC_CONTACT:
        raise SystemExit("SEC_CONTACT is missing. Add your email as a repository secret named SEC_CONTACT "
                         "(the SEC requires a contact email for free data access).")
    sec = make_session(f"Rule1Screener {SEC_CONTACT}")
    web = make_session("Mozilla/5.0 (Rule1Screener; personal research)")

    log(f"Universe: {UNIVERSE}")
    tick_map, sec_rows = sec_ticker_map(sec)
    uni, skipped = build_universe(web, sec_rows, tick_map)

    watch = read_watchlist()
    for t in watch:
        hit = tick_map.get(t) or tick_map.get(t.replace("-", ""))
        if not hit:
            skipped.append({"t": t, "why": "watchlist ticker has no SEC CIK"})
            continue
        c = uni.setdefault(hit[0], {"t": t, "cik": hit[0], "name": hit[1], "gicsSector": "",
                                    "gics": "", "lists": []})
        c["watch"] = True
        if "Watchlist" not in c["lists"]:
            c["lists"].append("Watchlist")
    log(f"{len(uni)} companies to screen")

    os.makedirs(CACHE_DIR, exist_ok=True)
    sic_path = os.path.join(CACHE_DIR, "sic.json")
    sic_cache = load_json(sic_path, {})

    companies = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=SEC_WORKERS) as pool:
        futs = {pool.submit(screen_one, c, sic_cache): c for c in uni.values()}
        for n, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                res, why = fut.result()
            except Exception as e:  # noqa: BLE001
                res, why = None, f"error ({type(e).__name__})"
            if res is None:
                skipped.append({"t": c["t"], "why": why})
            else:
                companies.append(res)
            if n % 250 == 0:
                log(f"  {n}/{len(uni)} done ({time.time() - t0:.0f}s)")

    with open(sic_path, "w") as f:
        json.dump(sic_cache, f)
    log(f"Analysed {len(companies)}, skipped {len(skipped)}")

    tickers = [c["t"] for c in companies]
    px = prices_yahoo(tickers)
    best_first = sorted(companies, key=lambda c: -c["big5"])
    missing = [c["t"] for c in best_first if c["t"] not in px]
    if missing:
        log(f"Yahoo missing {len(missing)} prices, trying Stooq")
        px.update(prices_stooq(web, missing))
    for c in companies:
        p = px.get(c["t"])
        c["px"] = p
    log(f"Prices for {sum(1 for c in companies if c['px'])}/{len(companies)}")

    mac = macro(web)

    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "universe": UNIVERSE,
        "companies": companies,
        "skipped": skipped,
        "macro": mac,
    }
    with open(os.path.join(OUT_DIR, "data.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    # copy the dashboard next to the data
    with open("dashboard.html") as src, open(os.path.join(OUT_DIR, "index.html"), "w") as dst:
        dst.write(src.read())
    log("Wrote site/data.json and site/index.html")


if __name__ == "__main__":
    sys.exit(main())
