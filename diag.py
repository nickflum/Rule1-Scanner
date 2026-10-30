"""One-off diagnostics: why companies have no Sticker Price. Writes diag/ for review."""
import json, os, sys, requests
sys.path.insert(0, ".")
import screener
os.makedirs("diag", exist_ok=True)
ua = {"User-Agent": f"Rule1Screener {os.environ['SEC_CONTACT']}"}
d = requests.get("https://nickflum.github.io/Rule1-Scanner/data.json", timeout=120).json()
cs = d["companies"]
def growth(c):
    vals = [c["g"][k][w] for k in ("eq", "eps") for w in ("10", "5") if c["g"][k][w] is not None]
    return min(vals) if vals else None
rows = []
for c in cs:
    g = growth(c)
    why = ("no price" if not c.get("px") else "EPS<=0" if (c.get("eps") is None or c["eps"] <= 0)
           else "no growth history" if g is None else "growth<=0" if g <= 0 else "growth<5%" if g < 0.05 else "ok")
    rows.append({"t": c["t"], "big5": c["big5"], "why": why, "g": g, "eps": c.get("eps"),
                 "eq": c["g"]["eq"], "epsg": c["g"]["eps"], "flags": c["flags"]})
summary = {}
for r in rows:
    summary.setdefault(r["why"], [0, 0]); summary[r["why"]][0] += 1
    if r["big5"] >= 50: summary[r["why"]][1] += 1
json.dump({"n": len(cs), "summary_all_vs_big5ge50": summary,
           "big5ge50_problem": [r for r in rows if r["big5"] >= 50 and r["why"] not in ("ok",)][:400]},
          open("diag/summary.json", "w"), indent=1)
keep = {"GOOGL", "RL", "AAPL", "NVDA", "AMZN", "WMT", "COST", "MSFT", "AVGO", "CMG"}
tm, _ = screener.sec_ticker_map(screener.make_session(ua["User-Agent"]))
for t in keep:
    cik = tm[t][0]
    f = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=ua, timeout=60).json()["facts"]
    out = {}
    for tag in ("EarningsPerShareDiluted", "WeightedAverageNumberOfDilutedSharesOutstanding", "StockholdersEquity", "NetIncomeLoss"):
        node = f.get("us-gaap", {}).get(tag)
        if node:
            out[tag] = [e for u in node["units"].values() for e in u if e.get("form", "").startswith("10-K")]
    json.dump(out, open(f"diag/{t}.json", "w"))
    res, why = screener.analyse(f)
    json.dump({"why": why, "res": res}, open(f"diag/{t}_analysis.json", "w"), indent=1)
print(json.dumps(summary))
