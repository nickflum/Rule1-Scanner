"""
Rule #1 screener — builds site/data.json for the dashboard.

What it does, in order:
  1. Gets the list of companies to screen (S&P 500 by default, plus watchlist.txt).
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
  UNIVERSE     "sp500" (default) or "sp1500" (S&P 500 + 400 + 600)
"""

import datetime as dt
import json
import math
import os
import sys
import time
from io import StringIO

import numpy as np
import pandas as pd
import requests

SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
UNIVERSE = os.environ.get("UNIVERSE", "sp500").strip().lower() or "sp500"
OUT_DIR = os.environ.get("OUT_DIR", "site")
SEC_DELAY = 0.12  # SEC allows max 10 requests/second

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


def get(session, url, tries=3, timeout=30):
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            log(f"  HTTP {r.status_code} for {url}")
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


def get_universe(wiki_session):
    parts = []
    t = wiki_table(wiki_session, WIKI["sp500"])
    if t is None:
        log("Wikipedia S&P 500 list unavailable, using backup CSV")
        r = get(wiki_session, SP500_CSV)
        if r is not None:
            t = pd.read_csv(StringIO(r.text))
    if t is not None:
        parts.append(standardise(t, "S&P 500"))
    if UNIVERSE == "sp1500":
        for key, label in (("sp400", "S&P 400"), ("sp600", "S&P 600")):
            t = wiki_table(wiki_session, WIKI[key])
            if t is not None:
                parts.append(standardise(t, label))
            else:
                log(f"Could not load {label} list")
    if not parts:
        raise SystemExit("Could not load any index constituent list.")
    uni = pd.concat(parts).drop_duplicates("ticker").reset_index(drop=True)
    return uni


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
    r = get(sec, "https://www.sec.gov/files/company_tickers.json")
    if r is None:
        raise SystemExit("Could not download the SEC ticker list. Check SEC_CONTACT is a real email.")
    m = {}
    for row in r.json().values():
        m[norm_ticker(row["ticker"])] = (int(row["cik_str"]), row["title"])
    return m


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


def prices_stooq(session, tickers):
    out = {}
    for t in tickers:
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

def main():
    if "@" not in SEC_CONTACT:
        raise SystemExit("SEC_CONTACT is missing. Add your email as a repository secret named SEC_CONTACT "
                         "(the SEC requires a contact email for free data access).")
    sec = make_session(f"Rule1Screener {SEC_CONTACT}")
    web = make_session("Mozilla/5.0 (Rule1Screener; personal research)")

    log(f"Universe: {UNIVERSE}")
    uni = get_universe(web)
    tick_map = sec_ticker_map(sec)

    watch = read_watchlist()
    have = set(uni["ticker"])
    extra = [t for t in watch if t not in have]
    if extra:
        uni = pd.concat([uni, pd.DataFrame({
            "ticker": extra, "name": "", "sector": "Watchlist", "sub": "Watchlist", "index": "Watchlist"})],
            ignore_index=True)
    watch_set = set(watch)
    log(f"{len(uni)} companies to screen")

    companies, skipped = [], []
    for i, row in uni.iterrows():
        t = row["ticker"]
        hit = tick_map.get(t) or tick_map.get(t.replace("-", ""))
        if not hit:
            skipped.append({"t": t, "why": "no SEC CIK for this ticker"})
            continue
        cik, title = hit
        r = get(sec, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", timeout=60)
        time.sleep(SEC_DELAY)
        if r is None:
            skipped.append({"t": t, "why": "SEC data not available"})
            continue
        try:
            res, why = analyse(r.json().get("facts", {}))
        except Exception as e:  # noqa: BLE001
            res, why = None, f"could not parse ({type(e).__name__})"
        if res is None:
            skipped.append({"t": t, "why": why})
            continue
        res.update({
            "t": t, "name": row["name"] or title, "sector": row["sector"], "sub": row["sub"],
            "index": row["index"], "cik": cik, "watch": t in watch_set,
        })
        companies.append(res)
        if (i + 1) % 50 == 0:
            log(f"  {i + 1}/{len(uni)} done")

    log(f"Analysed {len(companies)}, skipped {len(skipped)}")

    tickers = [c["t"] for c in companies]
    px = prices_yahoo(tickers)
    missing = [t for t in tickers if t not in px]
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
