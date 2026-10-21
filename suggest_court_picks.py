# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv", "defusedxml"]
# ///
"""Compound Crowns: suggest REIT and BDC Court Pick candidates. Suggestions only; nothing is added.

REITs and BDCs rarely make the Dividend Champions list (25+ years of raises), so the main screen seldom holds
them. This screen looks for exchange-listed REITs and BDCs with at least MIN_STREAK consecutive fiscal years of
higher dividends per share, using free SEC data:
  - REITs: SEC filers with industry code 6798 (real estate investment trusts)
  - BDCs:  companies that file 10-Ks *and* 40-17G fidelity-bond filings (investment companies that report
           like operating companies), found with EDGAR full-text search
  - streak: annual dividends per share from each company's XBRL filings (back to about 2009)
Prices come from Alpaca. Writes data/court_pick_suggestions.csv and prints the list.

To use a suggestion, add its ticker to court_picks.txt yourself (max 10 Court Picks). In a taxable account,
REIT and BDC dividends are mostly ordinary income.

Run:  uv run suggest_court_picks.py
"""
import csv
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import defusedxml.ElementTree as ET
import requests
from dotenv import load_dotenv

from compute_crown_scores import CACHE, SEC_FACTS, durations, sec_fetch, sec_get

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "court_pick_suggestions.csv"

MIN_STREAK = 10              # consecutive fiscal years of higher dividends per share
MIN_UNBROKEN_DATA = 7        # shorter runs that reach the start of the SEC data without a decrease are shown, flagged
MIN_MARKET_CAP = 1e9         # skip micro caps
SHOW = 25                    # rows printed per type
REIT_SIC = "6798"
LISTED = ("NYSE", "Nasdaq")
# Per-share distribution tags. BDC totals can include special/supplemental distributions.
DPS_CONCEPTS = ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid",
                "InvestmentCompanyDistributionToShareholdersPerShare", "DistributionsPerShare"]
ATOM = "{http://www.w3.org/2005/Atom}"


def listed_tickers(sec):
    """{cik: common-stock ticker} for companies on NYSE/Nasdaq."""
    data = sec_get(sec, "https://www.sec.gov/files/company_tickers_exchange.json", "company_tickers_exchange.json")
    out = {}
    for cik, _, ticker, exchange in data["data"]:
        if exchange in LISTED and ticker:
            current = out.get(cik)
            # prefer the plain common ticker over preferreds/units (e.g. ADC over ADC-PA)
            if current is None or ("-" in current and "-" not in ticker) or len(ticker) < len(current):
                out[cik] = ticker
    return out


def reit_ciks(sec):
    ciks, start = set(), 0
    while True:
        resp = sec_fetch(sec, "https://www.sec.gov/cgi-bin/browse-edgar?" + "&".join(
            f"{k}={v}" for k, v in {"action": "getcompany", "SIC": REIT_SIC, "type": "10-K",
                                    "count": 100, "start": start, "output": "atom"}.items()))
        entries = ET.fromstring(resp.content).findall(ATOM + "entry")
        for e in entries:
            m = re.search(r"cik>(\d+)</|CIK=(\d+)", ET.tostring(e, encoding="unicode"))  # tags carry ns prefixes
            if m:
                ciks.add(int(m.group(1) or m.group(2)))
        if len(entries) < 100:
            return ciks
        start += 100
        time.sleep(0.2)


def bdc_ciks(sec, today):
    """CIKs that filed a 40-17G in the past year (investment companies, including BDCs)."""
    ciks, offset = set(), 0
    while True:
        resp = sec.get("https://efts.sec.gov/LATEST/search-index", timeout=30, params={
            "forms": "40-17G", "startdt": (today - timedelta(days=365)).isoformat(), "enddt": today.isoformat(),
            "from": offset})
        hits = resp.json().get("hits", {}).get("hits", [])
        for h in hits:
            ciks.update(int(c) for c in h["_source"].get("ciks", []))
        if len(hits) < 100:
            return ciks
        offset += 100
        time.sleep(0.2)


def files_10k(sec, cik):
    sub = sec_get(sec, f"https://data.sec.gov/submissions/CIK{cik:010d}.json", f"submissions_{cik}.json") or {}
    forms = set(sub.get("filings", {}).get("recent", {}).get("form", []))
    return "10-K" in forms and "N-CSR" not in forms


def annual_dps(facts):
    """[(fiscal year end, dividends per share)] oldest first, one value per fiscal year."""
    periods = durations(facts, DPS_CONCEPTS, "USD/shares")
    years = {}
    for (start, end), value in periods.items():
        if 350 <= (end - start).days <= 380 and value:
            years[end] = value
    return sorted(years.items())


def streak(years):
    """(count, broken): consecutive fiscal years, counting back from the latest, with a higher dividend than the
    year before. broken=False means the run reached the start of the SEC data (or a gap in it) without a
    decrease, so the real streak may be longer than the data shows."""
    count = 0
    for (end_prev, prev), (end, cur) in zip(reversed(years[:-1]), reversed(years[1:])):
        if not (300 <= (end - end_prev).days <= 430):
            return count, False  # gap in the data, not a dividend decrease
        if cur <= prev * 1.0001:
            return count, True
        count += 1
    return count, False


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    key, secret = os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not ua or not key:
        sys.exit("Needs SEC_USER_AGENT and Alpaca keys in .env (run uv run check_setup.py).")
    today = date.today()
    sec = requests.Session()
    sec.headers["User-Agent"] = ua

    tickers = listed_tickers(sec)
    print("Finding REITs (SEC industry code 6798)...")
    reits = {c for c in reit_ciks(sec) if c in tickers}
    print("Finding BDCs (10-K filers that also file 40-17G)...")
    bdcs = {c for c in bdc_ciks(sec, today) if c in tickers and c not in reits}
    bdcs = {c for c in bdcs if files_10k(sec, c)}
    print(f"{len(reits)} listed REITs, {len(bdcs)} listed BDCs. Measuring dividend streaks...")

    rows = []
    for kind, ciks in (("REIT", reits), ("BDC", bdcs)):
        for i, cik in enumerate(sorted(ciks), 1):
            facts = sec_get(sec, SEC_FACTS.format(cik=cik), f"CIK{cik}.json")
            if not facts:
                continue
            years = annual_dps(facts)
            n, broken = streak(years)
            if n >= MIN_STREAK:
                note = "proven in SEC data"
            elif not broken and n >= MIN_UNBROKEN_DATA:
                note = f"no decrease since {years[-1 - n][0].year} (SEC data starts there); verify the full streak"
            else:
                continue
            shares = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}) \
                .get("units", {}).get("shares", [])
            latest_shares = max(shares, key=lambda f: f["end"])["val"] if shares else None
            dps_now, dps_5y = years[-1][1], (years[-6][1] if len(years) >= 6 else None)
            rows.append({"symbol": tickers[cik], "type": kind, "company": facts.get("entityName", ""),
                         "streak_years": n, "streak_note": note, "fiscal_year_end": years[-1][0].isoformat(),
                         "annual_dps": round(dps_now, 4),
                         "dps_growth_5y": round((dps_now / dps_5y) ** 0.2 - 1, 4) if dps_5y else None,
                         "shares": latest_shares})

    # Prices from Alpaca, for yield and market cap.
    alpaca = requests.Session()
    alpaca.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    prices = {}
    syms = [r["symbol"] for r in rows]
    for i in range(0, len(syms), 100):
        trades = alpaca.get("https://data.alpaca.markets/v2/stocks/trades/latest", timeout=30,
                            params={"symbols": ",".join(s.replace("-", ".") for s in syms[i:i + 100]),
                                    "feed": "iex"}).json().get("trades", {})
        prices.update({k.replace(".", "-"): v.get("p") for k, v in trades.items()})
    kept = []
    for r in rows:
        price = prices.get(r["symbol"])
        r["price"] = price
        r["yield"] = round(r["annual_dps"] / price, 4) if price else None
        r["market_cap"] = round(r["shares"] * price) if price and r["shares"] else None
        if r["market_cap"] is None or r["market_cap"] >= MIN_MARKET_CAP:
            kept.append(r)
    kept.sort(key=lambda r: (r["type"], -r["streak_years"], -(r["dps_growth_5y"] or 0)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "type", "company", "streak_years", "streak_note", "fiscal_year_end", "annual_dps", "dps_growth_5y",
              "yield", "price", "market_cap"]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    print(f"\nCourt Pick candidates: {MIN_STREAK}+ straight years of higher dividends, market cap >= "
          f"${MIN_MARKET_CAP / 1e9:.0f}B. Suggestions only; nothing was added.\n")
    for kind in ("REIT", "BDC"):
        group = [r for r in kept if r["type"] == kind]
        print(f"{kind}s ({len(group)}):")
        print(f"  {'symbol':7} {'streak (min)':>16} {'yield':>6} {'5y div growth':>13} {'mkt cap':>9}  company")
        for r in group[:SHOW]:
            y = f"{r['yield']:.1%}" if r["yield"] is not None else "-"
            g = f"{r['dps_growth_5y']:.1%}" if r["dps_growth_5y"] is not None else "-"
            cap = f"${r['market_cap'] / 1e9:.0f}B" if r["market_cap"] else "-"
            mark = "" if r["streak_note"] == "proven in SEC data" else "+ (verify)"
            print(f"  {r['symbol']:7} {str(r['streak_years']) + 'y' + mark:>16} {y:>6} {g:>13} {cap:>9}  {r['company'][:38]}")
        if not group:
            print("  none" + ("; BDCs rarely raise every year, since most pay variable or supplemental dividends"
                              if kind == "BDC" else ""))
        print()
    print(f"Full list: {OUT.relative_to(ROOT)}")
    print("Streaks count fiscal years of higher dividends per share in SEC filings, which start around 2009,")
    print("so they are minimums: a long-time raiser shows only the years in the data. '+ (verify)' means no")
    print("decrease as far back as the data goes; confirm the full history before relying on it.")
    print("Add any you want to court_picks.txt yourself (max 10). In a taxable account REIT and BDC dividends")
    print("are mostly ordinary income.")

if __name__ == "__main__":
    main()
