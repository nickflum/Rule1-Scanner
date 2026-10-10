# Rule #1 Screener

A free, self-updating dashboard that screens US companies using Phil Town's *Rule #1* method:

- **Big Five** growth test (ROIC, sales, EPS, equity, free cash flow) over 10, 5 and 1 years
- **Debt test** (long-term debt payable from ~3 years of free cash flow)
- **Sticker Price** and **margin-of-safety price**, with adjustable assumptions
- **Circle of competence** filter by industry
- A ranked **priority list** of companies to research
- Town's three timing **tools** (10-day MA, MACD, stochastic) and a **macro context** panel

Every weekday after the US close, GitHub runs `screener.py`, which pulls free data
(SEC EDGAR financials, Yahoo Finance prices, FRED macro series) and republishes the dashboard.

## Files

| File | What it is |
|---|---|
| `screener.py` | Downloads the data and calculates everything |
| `dashboard.html` | The dashboard page |
| `watchlist.txt` | Extra tickers to include and star |
| `.github/workflows/daily-screen.yml` | The daily schedule |
| `requirements.txt` | Python packages the script needs |

## Settings

- **Secret `SEC_CONTACT`** (required): your email. The SEC asks every user of its free data to identify themselves.
- **Variable `UNIVERSE`** (optional): `sp500` (default) or `sp1500` to add the S&P 400 mid caps and S&P 600 small caps.

This is a research shortlist, not investment advice. Check numbers against the actual filings before acting.
