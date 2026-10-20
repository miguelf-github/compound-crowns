# /// script
# requires-python = ">=3.10"
# dependencies = ["openpyxl", "requests", "python-dotenv", "defusedxml"]
# ///
"""Compound Crowns Step 1: build the screening universe.

Reads the U.S. Dividend Champions spreadsheet, pulls each company's profile from
Financial Modeling Prep, applies the methodology's exclusions and size filter,
and writes data/universe.csv (kept) and data/universe_excluded.csv (dropped, with reason).

Run:  uv run build_universe.py
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
CHAMPIONS_FILE = ROOT / "data" / "raw" / "2026_Dividend_Champions.01.xlsx"
CHAMPIONS_SHEET = "Dividend Champions for 2026"
CHAMPIONS_SOURCE = "Dividend Growth Investor, 2026 Dividend Champions list (as of 2025-12-31)"
OUT_KEPT = ROOT / "data" / "universe.csv"
OUT_EXCLUDED = ROOT / "data" / "universe_excluded.csv"

MAX_UNIVERSE = 150
LISTED_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "NYSE AMERICAN"}
PARTNERSHIP_RE = re.compile(r"\b(L\.?P\.?|Partners)\b", re.IGNORECASE)

FMP_STABLE = "https://financialmodelingprep.com/stable/profile"
FMP_V3 = "https://financialmodelingprep.com/api/v3/profile/{symbol}"
PROFILE_CACHE = ROOT / "data" / "cache" / "fmp" / "profiles"
CACHE_DAYS = 7

# Companies added by the user on top of the Champions list. They go through the same exclusions and
# size filter. Dividend growth rates are computed from SEC per-share dividend history.
MANUAL_ADDITIONS = [
    {"symbol": "ABBV", "company": "AbbVie Inc.", "sector": "Health Care", "streak": 54,
     "source": "Manual addition: S&P Dividend Aristocrat; streak counts Abbott history before the 2013 spin-off"},
]


def load_champions():
    wb = openpyxl.load_workbook(CHAMPIONS_FILE, read_only=True, data_only=True)
    rows = list(wb[CHAMPIONS_SHEET].iter_rows(values_only=True))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Company Name")
    header = rows[header_idx]
    col = {name: header.index(name) for name in
           ("Company Name", "Symbol", "Sector", "Yrs", "DGR 5-yr", "DGR 10-yr", "Dividend", "Yield")}
    champions = []
    for r in rows[header_idx + 1:]:
        if not r or not r[col["Symbol"]]:
            continue
        champions.append({
            "symbol": str(r[col["Symbol"]]).strip(),
            "company": r[col["Company Name"]],
            "sector": r[col["Sector"]],
            "streak": r[col["Yrs"]],
            "dgr5": r[col["DGR 5-yr"]],
            "dgr10": r[col["DGR 10-yr"]],
            "annual_dividend": r[col["Dividend"]],
            "list_yield": r[col["Yield"]],
            "source": CHAMPIONS_SOURCE,
        })
    return champions


def load_additions(sec_user_agent):
    """MANUAL_ADDITIONS rows with 5- and 10-year dividend growth from SEC filings."""
    from compute_crown_scores import SEC_FACTS, SEC_TICKERS, dps_growth, sec_get

    if not MANUAL_ADDITIONS:
        return []
    with requests.Session() as sec:
        sec.headers["User-Agent"] = sec_user_agent
        tickers = sec_get(sec, SEC_TICKERS, "company_tickers.json")
        cik_by_ticker = {v["ticker"].upper(): v["cik_str"] for v in tickers.values()}
        rows = []
        for add in MANUAL_ADDITIONS:
            cik = cik_by_ticker[add["symbol"].upper()]
            facts = sec_get(sec, SEC_FACTS.format(cik=cik), f"CIK{cik}.json")
            rows.append({**add, "dgr5": dps_growth(facts, 5), "dgr10": dps_growth(facts, 10),
                         "annual_dividend": None, "list_yield": None})
    return rows


def fetch_profile(session, symbol, api_key):
    """Return the FMP profile dict (cached for CACHE_DAYS), trying the 'stable' API then the legacy v3 one."""
    path = PROFILE_CACHE / f"{symbol}.json"
    if path.exists() and datetime.fromtimestamp(path.stat().st_mtime) > datetime.now() - timedelta(days=CACHE_DAYS):
        return json.loads(path.read_text(encoding="utf-8"))
    profile = _fetch_profile(session, symbol, api_key)
    if profile is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile), encoding="utf-8")
    return profile


def _fetch_profile(session, symbol, api_key):
    for url, params in ((FMP_STABLE, {"symbol": symbol, "apikey": api_key}),
                        (FMP_V3.format(symbol=symbol), {"apikey": api_key})):
        resp = session.get(url, params=params, timeout=20)
        if resp.status_code in (401, 403):
            body = resp.text[:200]
            if "Invalid API KEY" in body or "Invalid API key" in body:
                sys.exit(f"FMP rejected the API key: {body}")
            continue  # endpoint not on this plan; try the next one
        if resp.status_code == 429:
            sys.exit("FMP rate limit hit (429). Wait and re-run, or check your plan's daily limit.")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            p = data[0]
            p["marketCap"] = p.get("marketCap") or p.get("mktCap")
            return p
    return None


def exclusion_reason(champ, p):
    if p is None:
        return "no FMP profile found"
    if p.get("isEtf") or p.get("isFund"):
        return "pooled product (ETF/fund)"
    # Any company trading on a US exchange is eligible, including ones based or primarily listed abroad.
    if champ["sector"] == "Real Estate" or p.get("sector") == "Real Estate" or "REIT" in (p.get("industry") or ""):
        return "REIT"
    if PARTNERSHIP_RE.search(champ["company"] or "") or PARTNERSHIP_RE.search(p.get("companyName") or ""):
        return "partnership/MLP (K-1 issuer)"
    if p.get("isActivelyTrading") is False:
        return "not actively trading"
    exchange = (p.get("exchangeShortName") or p.get("exchange") or "").upper()
    if exchange not in LISTED_EXCHANGES:
        return f"not exchange-listed ({exchange or 'unknown'})"
    if not p.get("marketCap"):
        return "market cap missing"
    return None


def previous_profiles():
    """Profiles rebuilt from the last run's outputs, for when FMP's daily limit is used up."""
    profiles = {}
    for path in (OUT_KEPT, OUT_EXCLUDED):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                profiles[r["symbol"]] = {
                    "companyName": r.get("company"), "sector": r.get("fmp_sector"), "industry": r.get("industry"),
                    "country": r.get("country"), "exchange": r.get("exchange"),
                    "price": float(r["price"]) if r.get("price") else None,
                    "marketCap": float(r["market_cap"]) if r.get("market_cap") else None,
                }
    return profiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-profiles", action="store_true",
                    help="reuse market cap / exchange / country from the last run's CSVs instead of calling FMP")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key and not args.reuse_profiles:
        sys.exit("FMP_API_KEY is empty. Add it to the .env file in the project folder.")
    reused = previous_profiles() if args.reuse_profiles else None

    champions = load_champions()
    print(f"Loaded {len(champions)} Dividend Champions from {CHAMPIONS_FILE.name}")
    additions = load_additions(os.getenv("SEC_USER_AGENT", "").strip())
    known = {c["symbol"] for c in champions}
    champions += [a for a in additions if a["symbol"] not in known]
    print(f"Added {len(additions)} manual additions: {', '.join(a['symbol'] for a in additions)}")

    kept, excluded = [], []
    with requests.Session() as session:
        for i, champ in enumerate(champions, 1):
            p = reused.get(champ["symbol"]) if reused is not None else fetch_profile(session, champ["symbol"], api_key)
            reason = exclusion_reason(champ, p)
            row = {
                **champ,
                "fmp_sector": (p or {}).get("sector"),
                "industry": (p or {}).get("industry"),
                "country": (p or {}).get("country"),
                "exchange": (p or {}).get("exchangeShortName") or (p or {}).get("exchange"),
                "price": (p or {}).get("price"),
                "market_cap": (p or {}).get("marketCap"),
            }
            if reason:
                excluded.append({**row, "reason": reason})
            else:
                kept.append(row)
            print(f"  [{i:3}/{len(champions)}] {champ['symbol']:6} {reason or 'ok'}")
            if reused is None:
                time.sleep(0.25)

    median_cap = statistics.median(r["market_cap"] for r in kept)
    below = [r for r in kept if r["market_cap"] < median_cap]
    kept = [r for r in kept if r["market_cap"] >= median_cap]
    excluded += [{**r, "reason": f"below median market cap (${median_cap / 1e9:.1f}B)"} for r in below]

    kept.sort(key=lambda r: r["market_cap"], reverse=True)
    excluded += [{**r, "reason": f"beyond top {MAX_UNIVERSE}"} for r in kept[MAX_UNIVERSE:]]
    kept = kept[:MAX_UNIVERSE]

    as_of = date.today().isoformat()
    fields = ["symbol", "company", "sector", "fmp_sector", "industry", "country", "exchange",
              "market_cap", "price", "streak", "dgr5", "dgr10", "annual_dividend", "list_yield"]
    OUT_KEPT.parent.mkdir(parents=True, exist_ok=True)
    with OUT_KEPT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields + ["as_of", "source"], extrasaction="ignore")
        w.writeheader()
        for r in kept:
            w.writerow({**r, "as_of": as_of})
    with OUT_EXCLUDED.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "company", "reason", "market_cap", "country", "exchange", "industry"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(excluded)

    print(f"\nMedian market cap of eligible list: ${median_cap / 1e9:.1f}B")
    print(f"Kept {len(kept)} -> {OUT_KEPT.relative_to(ROOT)}")
    print(f"Excluded {len(excluded)} -> {OUT_EXCLUDED.relative_to(ROOT)}")
    counts = {}
    for r in excluded:
        key = r["reason"].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    for reason, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:3}  {reason}")
    sectors = {}
    for r in kept:
        sectors[r["sector"]] = sectors.get(r["sector"], 0) + 1
    print("\nKept by sector:")
    for s, n in sorted(sectors.items(), key=lambda x: -x[1]):
        print(f"  {n:3}  {s}")


if __name__ == "__main__":
    main()
