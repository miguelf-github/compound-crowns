# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv", "defusedxml"]
# ///
"""Compound Crowns Step 2: Crown-score the universe.

Reads data/universe.csv and scores each company per the methodology, using free data:
  - payout  = TTM common dividends paid / TTM free cash flow, from SEC EDGAR XBRL filings
              (falls back to dividends / net income when cash flow can't be built)
  - yield   = TTM dividends per share / current price. Dividend history comes from Alpaca when
              ALPACA_API_KEY / ALPACA_SECRET_KEY are in .env (split-adjusted, specials excluded);
              otherwise from per-share dividends declared in SEC filings.
  - streak, dgr5, dgr10 come from the Dividend Champions spreadsheet (via universe.csv).
  - intl    = share of revenue from outside the US, from the geographic revenue table tagged in the latest
              10-K (or geographic business segments). No geographic disclosure is treated as US-only (-1).

Writes data/crown_scores.csv and prints a preview of the Step 3 selection.
SEC responses are cached in data/cache/sec for CACHE_DAYS.

Run:  uv run compute_crown_scores.py
"""
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import defusedxml.ElementTree as ET
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
UNIVERSE = ROOT / "data" / "universe.csv"
OUT = ROOT / "data" / "crown_scores.csv"
CACHE = ROOT / "data" / "cache" / "sec"
CACHE_DAYS = 7

# Prefer US companies that sell worldwide; US-only revenue concentrates the business in one market.
INTL_SALES_FACTOR = True
TARGET_POSITIONS = 30
MAX_PER_SECTOR = 5
# The GICS sectors the strategy wants to hold. Selection takes the best-scoring name in each listed sector
# that has an eligible company, then fills the rest by score. Remove a sector here to keep it out entirely.
# Real Estate is added only for tax-advantaged accounts (see ACCOUNT_TYPE below).
SECTORS_TO_HOLD = [
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Utilities",
]


def tax_advantaged():
    """ACCOUNT_TYPE in .env: 'taxable' (default) or 'tax_advantaged' (IRA, 401(k) and similar).

    REITs and BDCs pay mostly ordinary (non-qualified) dividends, taxed at higher rates in a taxable account,
    so they are excluded by default. In a tax-advantaged account that difference doesn't apply, so they become
    eligible and Real Estate joins the sectors to hold. MLPs and other K-1 issuers stay excluded either way.
    """
    return os.getenv("ACCOUNT_TYPE", "taxable").strip().lower() == "tax_advantaged"


def sectors_to_hold():
    return SECTORS_TO_HOLD + (["Real Estate"] if tax_advantaged() else [])
FREEZE_MONTHS = 15
MAX_DPS_AGE_DAYS = 270  # older SEC per-share dividend data counts as missing
# Yield rule (very low yields take too long to compound into meaningful income). A company yielding
# under YIELD_FLOOR can only be selected if its 10-yr dividend growth is at least LOW_YIELD_MIN_DGR10.
# It still takes the -1 yield point in the Crown-score either way.
YIELD_FLOOR = 0.015
LOW_YIELD_MIN_DGR10 = 0.10
# User rule: these always take the high-payout penalty (-2), whatever the computed ratio.
PAYOUT_PENALTY_OVERRIDES = {
    "XOM": "known high payout ratio (user rule)",
    "CVX": "known high payout ratio (user rule)",
}

# Companies whose SEC filer ID changed (e.g. a holding-company reorganization): history lives under the old ID.
PREDECESSOR_CIKS = {
    "XOM": 34088,  # Exxon Mobil Corp, before the 2026 ExxonMobil Holdings reorganization
}

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ALPACA_CA = "https://data.alpaca.markets/v1/corporate-actions"

# XBRL concepts, in priority order. The concept with the most recent data wins.
DIVIDENDS_PAID = ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "PaymentsOfOrdinaryDividends",
                  "PaymentsOfDividendsAndDividendEquivalentsOnCommonStockAndRestrictedStockUnits"]
OPERATING_CF = ["NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
         "PaymentsForCapitalImprovements", "PaymentsToAcquireOtherPropertyPlantAndEquipment"]
NET_INCOME = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
DPS = ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]
# IFRS equivalents, for foreign filers (40-F / 20-F). They file annual XBRL only, so these TTMs are fiscal years.
IFRS_DIVIDENDS_PAID = ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid",
                       "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities", "DividendsPaidOrdinaryShares"]
IFRS_OPERATING_CF = ["CashFlowsFromUsedInOperatingActivities"]
IFRS_CAPEX = ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"]
IFRS_NET_INCOME = ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"]


# ---------- SEC EDGAR ----------

def sec_get(session, url, cache_name):
    path = CACHE / cache_name
    if path.exists() and datetime.fromtimestamp(path.stat().st_mtime) > datetime.now() - timedelta(days=CACHE_DAYS):
        return json.loads(path.read_text(encoding="utf-8"))
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        sys.exit("SEC refused the request (403). Set SEC_USER_AGENT in .env to 'Your Name your@email'.")
    resp.raise_for_status()
    data = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(0.15)  # SEC allows 10 requests/second
    return data


def durations(facts, concepts, unit, taxonomy="us-gaap"):
    """Duration facts as {(start, end): value}, merged across equivalent concepts.

    Companies rename concepts over time (e.g. operating cash flow moving to a 'continuing operations'
    tag), so all candidates are merged; for the same period the earlier concept in the list wins,
    and within a concept the latest filing wins.
    """
    out = {}
    for concept in reversed(concepts):
        node = facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit, [])
        for f in sorted(node, key=lambda f: f.get("filed", "")):
            if f.get("start") and f.get("end"):
                out[(date.fromisoformat(f["start"]), date.fromisoformat(f["end"]))] = f["val"]
    return out


def near(a, b, days):
    return abs((a - b).days) <= days


def quarters(periods):
    """Three-month values, both as reported and derived from year-to-date differences (e.g. Q4 = FY - 9M)."""
    q = {(s, e): v for (s, e), v in periods.items() if 80 <= (e - s).days <= 100}
    by_start = {}
    for (s, e), v in periods.items():
        by_start.setdefault(s, []).append((e, v))
    for s, items in by_start.items():
        items.sort()
        for (e1, v1), (e2, v2) in zip(items, items[1:]):
            if 80 <= (e2 - e1).days <= 100:
                q.setdefault((e1 + timedelta(days=1), e2), v2 - v1)
    return q


def ttm(periods, end=None):
    """Trailing twelve months ending at `end` (default: latest period) from SEC flow facts.

    Tries, in order: a fiscal year ending there; last FY + current YTD - prior-year YTD (10-Q cash flows
    are year-to-date); the sum of four chained quarters. Returns (value, period_end) or (None, None).
    """
    if not periods:
        return None, None
    end = end or max(e for _, e in periods)
    annual = {(s, e): v for (s, e), v in periods.items() if 350 <= (e - s).days <= 380}
    if (cur := [(s, e) for (s, e) in annual if near(e, end, 3)]):
        return annual[cur[0]], end

    ytd = [(s, e) for (s, e) in periods if near(e, end, 3) and (e - s).days < 350]
    if ytd:
        s, e = max(ytd, key=lambda p: (p[1] - p[0]).days)
        fy = next(((fs, fe) for (fs, fe) in annual if near(s - timedelta(days=1), fe, 7)), None)
        prior = next(((ps, pe) for (ps, pe) in periods
                      if near(pe, e - timedelta(days=365), 10) and abs((pe - ps).days - (e - s).days) <= 10), None)
        if fy and prior:
            return annual[fy] + periods[(s, e)] - periods[prior], end

    q = quarters(periods)
    total, cursor = 0.0, end
    for _ in range(4):
        hit = next(((s, e) for (s, e) in q if near(e, cursor, 5)), None)
        if not hit:
            return None, None
        total += q[hit]
        cursor = hit[0] - timedelta(days=1)
    return total, end


def dps_growth(facts, years):
    """Annualized growth of TTM dividends per share over `years`, from SEC filings."""
    periods = durations(facts, DPS, "USD/shares")
    now, end = ttm(periods)
    if not now:
        return None
    target = end - timedelta(days=round(365.25 * years))
    for e in sorted({e for _, e in periods}, key=lambda e: abs((e - target).days)):
        if abs((e - target).days) > 45:
            break
        then, _ = ttm(periods, e)
        if then:
            return (now / then) ** (1 / years) - 1
    # Fall back to fiscal year vs the fiscal year `years` earlier when quarterly history has gaps.
    annual = {e: v for (s, e), v in periods.items() if 350 <= (e - s).days <= 380}
    if annual:
        last = max(annual)
        then = next((v for e, v in annual.items() if near(e, last - timedelta(days=round(365.25 * years)), 10)), None)
        if then:
            return (annual[last] / then) ** (1 / years) - 1
    return None


def payout_from_sec(facts):
    gaap = facts.get("facts", {}).get("us-gaap", {})
    units = {u for c in DIVIDENDS_PAID if c in gaap for u in gaap[c]["units"]}
    unit = "USD" if "USD" in units or not units else sorted(units)[0]  # e.g. CAD for Canadian US-GAAP filers
    divs, end = ttm(durations(facts, DIVIDENDS_PAID, unit))
    if divs is None:
        return payout_from_ifrs(facts)
    payout, basis, flags = _payout_rest(facts, divs, end, OPERATING_CF, CAPEX, NET_INCOME, unit, "us-gaap")
    return payout, basis + ("" if unit == "USD" else f" ({unit})"), flags


def payout_from_ifrs(facts):
    """Payout for IFRS filers (e.g. Canadian 40-F filers): annual figures, in the reporting currency."""
    ifrs = facts.get("facts", {}).get("ifrs-full", {})
    units = next((list(ifrs[c]["units"]) for c in IFRS_DIVIDENDS_PAID if c in ifrs), None)
    if not units:
        return None, "dividends paid not found in SEC data", []
    unit = units[0]
    divs, end = ttm(durations(facts, IFRS_DIVIDENDS_PAID, unit, "ifrs-full"))
    if divs is None:
        return None, "dividends paid not found in SEC data", []
    payout, basis, flags = _payout_rest(facts, divs, end, IFRS_OPERATING_CF, IFRS_CAPEX, IFRS_NET_INCOME,
                                        unit, "ifrs-full")
    return payout, f"{basis} (IFRS, {unit})", flags


def _payout_rest(facts, divs, end, ocf_concepts, capex_concepts, ni_concepts, unit, taxonomy):
    flags = []
    ocf, _ = ttm(durations(facts, ocf_concepts, unit, taxonomy), end)
    if ocf is not None:
        capex, _ = ttm(durations(facts, capex_concepts, unit, taxonomy), end)
        if capex is None:
            flags.append("no capex line in SEC data; payout uses operating cash flow")
        fcf = ocf - abs(capex or 0)
        basis = "FCF" if capex is not None else "OCF only"
        return (abs(divs) / fcf if fcf > 0 else -1.0), f"{basis}, TTM to {end}", flags
    ni, _ = ttm(durations(facts, ni_concepts, unit, taxonomy), end)
    if ni is not None:
        return (abs(divs) / ni if ni > 0 else -1.0), f"EPS fallback, TTM to {end}", flags
    return None, f"no cash flow or earnings for TTM to {end}", flags


# ---------- dividend history ----------

def alpaca_dividends(session, symbols, start, end):
    """{symbol: [(ex_date, split-adjusted rate), ...] newest first}, regular dividends only."""
    divs, splits, token = [], [], None
    to_ours = {s.replace("-", "."): s for s in symbols}  # Alpaca writes share classes as BF.B
    while True:
        params = {"symbols": ",".join(to_ours), "types": "cash_dividend,forward_split,reverse_split",
                  "start": start.isoformat(), "end": end.isoformat(), "limit": 1000}
        if token:
            params["page_token"] = token
        resp = session.get(ALPACA_CA, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        ca = body.get("corporate_actions", {})
        divs += ca.get("cash_dividends", [])
        splits += ca.get("forward_splits", []) + ca.get("reverse_splits", [])
        token = body.get("next_page_token")
        if not token:
            break
    out = {}
    for d in divs:
        if d.get("special"):
            continue
        ex = date.fromisoformat(d["ex_date"])
        rate = d["rate"]
        for s in splits:  # restate pre-split dividends in today's share count
            if s["symbol"] == d["symbol"] and date.fromisoformat(s["ex_date"]) > ex:
                rate *= s["old_rate"] / s["new_rate"]
        out.setdefault(to_ours.get(d["symbol"], d["symbol"]), []).append((ex, rate))
    for v in out.values():
        v.sort(reverse=True)
    return out


def dividend_flags(history, today):
    """Cut/freeze flags and last increase date from a newest-first [(date, amount)] list."""
    flags = []
    if len(history) >= 2 and history[0][1] < history[1][1] * 0.98:
        flags.append(f"possible CUT: {history[1][1]:.4f} -> {history[0][1]:.4f} on {history[0][0]}")
    last_increase = next((history[i][0] for i in range(len(history) - 1)
                          if history[i][1] > history[i + 1][1] * 1.001), None)
    if last_increase is None and len(history) >= 4:
        flags.append("possible FREEZE: no increase in the available history")
    elif last_increase and (today - last_increase).days / 30.44 > FREEZE_MONTHS:
        flags.append(f"possible FREEZE: last increase {last_increase}")
    return last_increase, flags


# ---------- international sales (latest 10-K, SEC XBRL instance) ----------

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
# 10-K for US filers; 40-F (Canada) and 20-F (other foreign) filers report in IFRS.
ANNUAL_FORMS = ("10-K", "40-F", "20-F")
REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
                    # insurers report geography on premiums rather than revenue
                    "PremiumsEarnedNet", "PremiumsWrittenGross", "PremiumsWrittenNet",
                    "Revenue", "RevenueFromContractsWithCustomers")  # last two: IFRS filers
NON_US_RE = re.compile(r"(non|outside|otherthan|excluding|ex)(us|unitedstates|usa)|international|foreign|nondomestic")
FOREIGN_WORDS = ("Japan", "Europe", "EMEA", "Asia", "Pacific", "Latin", "Canada", "China", "International",
                 "Foreign", "Africa", "MiddleEast", "UnitedKingdom", "Germany", "Mexico", "Brazil", "Australia",
                 "Overseas")


def geo_class(member):
    """'US', 'NONUS' (an aggregate of everything outside the US), 'NA' (North America), 'FOREIGN', or None."""
    ns, _, local = member.partition(":")
    if ns == "country":
        return "US" if local == "US" else "FOREIGN"
    name = local.removesuffix("Member")
    low = name.lower()
    if NON_US_RE.search(low):
        return "NONUS"
    tokens = set(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", name))
    if tokens & {"US", "USA", "Domestic"} or "UnitedStates" in name:
        return "US"
    if "NorthAmerica" in name or ("Americas" in name and "Latin" not in name):
        return "NA"
    if any(w in name for w in FOREIGN_WORDS):
        return "FOREIGN"
    return None


def sec_fetch(session, url, tries=4):
    """GET with backoff: SEC answers bursts with 403/429 'request rate threshold exceeded'."""
    for attempt in range(tries):
        resp = session.get(url, timeout=60)
        if resp.status_code not in (403, 429, 503):
            return resp
        time.sleep(2 * (attempt + 1))
    return resp


def latest_10k_instance(session, cik):
    """(report_date, XBRL instance bytes) for the latest 10-K, or (None, None)."""
    sub = sec_get(session, f"https://data.sec.gov/submissions/CIK{cik:010d}.json", f"submissions_{cik}.json")
    recent = (sub or {}).get("filings", {}).get("recent", {})
    idx = next((i for i, f in enumerate(recent.get("form", [])) if f in ANNUAL_FORMS), None)
    if idx is None:
        return None, None
    acc = recent["accessionNumber"][idx].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
    resp = sec_fetch(session, base + recent["primaryDocument"][idx].rsplit(".", 1)[0] + "_htm.xml")
    if resp.status_code != 200:  # older filings: find the instance in the filing index
        time.sleep(1)
        index = sec_fetch(session, base + "index.json").json()
        names = [x["name"] for x in index["directory"]["item"] if x["name"].endswith(".xml")]
        # the instance is <doc>_htm.xml (inline XBRL) or the .xml that isn't a linkbase (_cal/_def/_lab/_pre)
        name = next((n for n in names if n.endswith("_htm.xml")), None) or next(
            (n for n in names if not n.startswith(("FilingSummary", "R"))
             and not n.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml"))), None)
        if not name:
            return recent["reportDate"][idx], None
        resp = sec_fetch(session, base + name)
    time.sleep(0.15)
    return recent["reportDate"][idx], resp.content if resp.status_code == 200 else None


def intl_from_instance(xml_bytes):
    """Share of revenue from outside the US for the latest fiscal year, with a note on how it was derived."""
    tree = ET.fromstring(xml_bytes)
    contexts = {}
    for cx in tree.iter(XBRLI + "context"):
        period = cx.find(XBRLI + "period")
        start, end = period.findtext(XBRLI + "startDate"), period.findtext(XBRLI + "endDate")
        dims = [(m.get("dimension", ""), (m.text or "").strip()) for m in cx.iter(XBRLDI + "explicitMember")]
        if start and end and 350 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 380:
            contexts[cx.get("id")] = (end, dims)
    totals, splits = {}, {}  # (concept, end) -> value ; (concept, end, axis kind) -> {member key: value}
    for el in tree:
        concept = el.tag.split("}")[-1]
        if concept not in REVENUE_CONCEPTS or el.get("contextRef") not in contexts or not (el.text or "").strip():
            continue
        end, dims = contexts[el.get("contextRef")]
        # "Operating segments" on the consolidation axis is just a label; eliminations and corporate rows are skipped.
        dims = [d for d in dims if not (d[0].endswith("ConsolidationItemsAxis") and d[1].endswith("OperatingSegmentsMember"))]
        if any(d[0].endswith("ConsolidationItemsAxis") for d in dims):
            continue
        value = float(el.text)
        geo = [m for a, m in dims if a.endswith(("StatementGeographicalAxis", "GeographicalAreasAxis"))]
        seg = [m for a, m in dims if a.endswith("SegmentsAxis")]
        if not dims:
            totals[(concept, end)] = value
        elif len(dims) == 1:
            splits.setdefault((concept, end, "geo" if geo else "segment"), {})[dims[0][1]] = value
        elif len(dims) == 2 and geo and seg:  # geography x business segment: summed per geography below
            splits.setdefault((concept, end, "geo_x_segment"), {})[(geo[0], seg[0])] = value
    for key in [k for k in splits if k[2] == "geo_x_segment"]:
        by_geo = {}
        for (geo_member, _), v in splits[key].items():
            by_geo[geo_member] = by_geo.get(geo_member, 0) + v
        splits[key] = by_geo

    def compute(members, total):
        by = {}
        for m, v in members.items():
            c = geo_class(m)
            if c:
                by[c] = by.get(c, 0) + v
        if "US" in by:
            return 1 - by["US"] / total, "US revenue reported"
        if "NONUS" in by:
            return by["NONUS"] / total, "non-US revenue reported"
        if "NA" in by:
            return 1 - by["NA"] / total, "North America used as proxy for US"
        if "FOREIGN" in by:
            return by["FOREIGN"] / total, "only foreign regions reported (lower bound)"
        return None, None

    for axis in ("geo", "geo_x_segment", "segment"):
        keys = sorted((k for k in splits if k[2] == axis), key=lambda k: (k[1], k[0] == "Revenues"), reverse=True)
        for concept, end, _ in keys:
            total = totals.get((concept, end)) or max((v for (c, e), v in totals.items() if e == end), default=None)
            if not total:
                continue
            share, how = compute(splits[(concept, end, axis)], total)
            if share is not None:
                note = {"segment": " (business segments)", "geo_x_segment": " (summed across business segments)"}
                return max(0.0, min(1.0, share)), f"{how}, FY ending {end}{note.get(axis, '')}"
    if not totals:  # e.g. a 40-F whose XBRL covers only the cover page: unknown, not US-only
        return None, "no revenue data in the annual filing's XBRL"
    return 0.0, "no geographic revenue disclosure (treated as US-only)"


def intl_sales(session, cik):
    """Cached wrapper: (intl share or None, basis)."""
    path = CACHE / f"intl_{cik}.json"
    if path.exists() and datetime.fromtimestamp(path.stat().st_mtime) > datetime.now() - timedelta(days=CACHE_DAYS):
        return tuple(json.loads(path.read_text(encoding="utf-8")))
    report_date, xml_bytes = latest_10k_instance(session, cik)
    if not xml_bytes:
        return None, "no 10-K XBRL instance found"  # not cached, so the next run retries
    result = intl_from_instance(xml_bytes)
    path.write_text(json.dumps(result), encoding="utf-8")
    return result


# ---------- selection (methodology Step 3) ----------

def yield_block(yld, dgr10):
    """Reason a company can't be selected on yield, or '' if it can.

    A very low yield takes too long to compound into meaningful income, so it's only acceptable when the
    dividend is growing at double digits over 10 years (the consistency measure).
    """
    if yld is not None and yld < YIELD_FLOOR and dgr10 < LOW_YIELD_MIN_DGR10:
        return f"yield {yld:.1%} < {YIELD_FLOOR:.1%} without {LOW_YIELD_MIN_DGR10:.0%}+ 10-yr dividend growth"
    return ""


def select_portfolio(ranked):
    """Best name in every sector we hold (pass 1), then fill by rank under the sector cap (pass 2).

    Companies blocked by the yield rule are skipped.
    """
    pool = [r for r in ranked if r["sector"] in sectors_to_hold() and not r.get("blocked")]
    selected, per_sector = [], {}
    for r in pool:
        if r["sector"] not in per_sector:
            selected.append(r)
            per_sector[r["sector"]] = 1
    for r in pool:
        if len(selected) >= TARGET_POSITIONS:
            break
        if r in selected or per_sector[r["sector"]] >= MAX_PER_SECTOR:
            continue
        selected.append(r)
        per_sector[r["sector"]] += 1
    return sorted(selected, key=lambda r: r["rank"])


# ---------- scoring (methodology Step 2) ----------

def pts_intl(s):
    """>=40% = 2, 20-39% = 1, 1-19% = 0, US-only (<1%) = -1, unknown = 0."""
    if s is None:
        return 0
    return 2 if s >= .40 else 1 if s >= .20 else 0 if s >= .01 else -1


def pts_streak(y):   return 3 if y >= 50 else 2 if y >= 35 else 1
def pts_dgr5(g):     return 2 if g >= .10 else 1 if g >= .05 else 0 if g >= .03 else -1
def pts_dgr10(g):    return 3 if g >= .08 else 2 if g >= .04 else 0 if g >= .02 else -1
def pts_yield(y):    return 2 if y >= .035 else 1 if y >= .015 else -1
def pts_payout(p):   return -2 if p < 0 or p >= .90 else 0 if p >= .60 else 1


def main():
    load_dotenv(ROOT / ".env")
    today = date.today()
    sec = requests.Session()
    sec.headers["User-Agent"] = os.getenv("SEC_USER_AGENT", "").strip() or "Compound Crowns research script (personal use)"

    with UNIVERSE.open(encoding="utf-8") as f:
        universe = list(csv.DictReader(f))
    for r in universe:
        for k in ("market_cap", "price", "dgr5", "dgr10", "annual_dividend"):
            r[k] = float(r[k] or 0)
        r["streak"] = int(r["streak"])
    n = len(universe)
    for i, r in enumerate(sorted(universe, key=lambda r: -r["market_cap"])):
        r["pts_mcap"] = 2 if i < n / 3 else 1 if i < 2 * n / 3 else 0

    tickers = sec_get(sec, SEC_TICKERS, "company_tickers.json")
    cik_by_ticker = {v["ticker"].upper(): v["cik_str"] for v in tickers.values()}

    alpaca_key, alpaca_secret = os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
    alpaca_hist = None
    if alpaca_key and alpaca_secret:
        with requests.Session() as a:
            a.headers.update({"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret})
            # Alpaca filters on process (payment) date, so look ahead to catch dividends already ex but not yet paid.
            alpaca_hist = alpaca_dividends(a, [r["symbol"] for r in universe],
                                           today - timedelta(days=800), today + timedelta(days=60))
            alpaca_hist = {s: [(d, v) for d, v in h if d <= today] for s, h in alpaca_hist.items()}
            # Latest trade prices, fresher than the universe snapshot (and present for every symbol).
            syms = ",".join(r["symbol"].replace("-", ".") for r in universe)
            trades = a.get("https://data.alpaca.markets/v2/stocks/trades/latest",
                           params={"symbols": syms, "feed": "iex"}, timeout=30).json().get("trades", {})
            for r in universe:
                price = (trades.get(r["symbol"].replace("-", ".")) or {}).get("p")
                if price:
                    r["price"] = price
    yield_source = "Alpaca dividend history" if alpaca_hist is not None else "SEC dividends per share declared"
    print(f"Yield source: {yield_source}")

    rows = []
    for i, r in enumerate(universe, 1):
        sym = r["symbol"]
        print(f"  [{i:2}/{n}] {sym}")
        flags = []
        cik = cik_by_ticker.get(sym.upper()) or cik_by_ticker.get(sym.upper().replace("-", "."))
        facts = sec_get(sec, SEC_FACTS.format(cik=cik), f"CIK{cik}.json") if cik else None
        old_cik = PREDECESSOR_CIKS.get(sym)
        if old_cik:  # merge the predecessor's history in; the current filer's facts win for the same period
            old = sec_get(sec, SEC_FACTS.format(cik=old_cik), f"CIK{old_cik}.json") or {}
            merged = (facts or {"facts": {}}).setdefault("facts", {}).setdefault("us-gaap", {})
            for concept, node in old.get("facts", {}).get("us-gaap", {}).items():
                for unit, items in node.get("units", {}).items():
                    current = merged.setdefault(concept, {"units": {}})["units"].setdefault(unit, [])
                    current[:0] = items  # older filings first, so the current filer's values take precedence
            facts = facts or {"facts": {"us-gaap": merged}}
        if facts is None:
            flags.append("no SEC XBRL data")
            payout, payout_basis = None, "no SEC data"
        else:
            payout, payout_basis, payout_flags = payout_from_sec(facts)
            flags += payout_flags
            if payout is None:
                flags.append(f"payout missing ({payout_basis})")

        intl, intl_basis = intl_sales(sec, cik) if (INTL_SALES_FACTOR and cik) else (None, "factor off")
        if INTL_SALES_FACTOR and intl is None and old_cik:
            intl, intl_basis = intl_sales(sec, old_cik)
            intl_basis += " (predecessor filer)"
        if INTL_SALES_FACTOR and (intl is None or "treated as US-only" in intl_basis or "proxy" in intl_basis
                                  or "lower bound" in intl_basis):
            flags.append(f"intl sales: {intl_basis}")

        if alpaca_hist is not None:
            hist = alpaca_hist.get(sym, [])
            ttm_dps = sum(v for d, v in hist if d > today - timedelta(days=365))
            last_inc, div_flags = dividend_flags(hist, today)
            flags += div_flags
            dps_basis = "Alpaca TTM ex-dates"
        else:
            ttm_dps, dps_end = ttm(durations(facts, DPS, "USD/shares")) if facts else (None, None)
            last_inc = None
            dps_basis = f"SEC per-share, TTM to {dps_end}"
            if not ttm_dps or (today - dps_end).days > MAX_DPS_AGE_DAYS:
                # The Champions spreadsheet's Dividend column is misaligned across rows, so it is not a fallback.
                dps_basis = f"SEC per-share data ends {dps_end}" if ttm_dps else "no SEC per-share data"
                ttm_dps = None
        yld = ttm_dps / r["price"] if ttm_dps and r["price"] else None
        if yld is None:
            flags.append(f"yield missing ({dps_basis})")

        pts = {
            "pts_streak": pts_streak(r["streak"]),
            "pts_mcap": r["pts_mcap"],
            "pts_intl": pts_intl(intl) if INTL_SALES_FACTOR else 0,
            "pts_dgr5": pts_dgr5(r["dgr5"]),
            "pts_dgr10": pts_dgr10(r["dgr10"]),
            "pts_yield": pts_yield(yld) if yld is not None else 0,
            "pts_payout": pts_payout(payout) if payout is not None else 0,
        }
        if sym in PAYOUT_PENALTY_OVERRIDES:
            pts["pts_payout"] = -2
            flags.append(f"payout docked to -2: {PAYOUT_PENALTY_OVERRIDES[sym]}")
        rows.append({
            "symbol": sym, "company": r["company"], "sector": r["sector"],
            "crown_score": sum(pts.values()), **pts,
            "streak": r["streak"], "market_cap": r["market_cap"],
            "intl_pct": None if intl is None else round(intl, 4), "intl_basis": intl_basis,
            "dgr5": round(r["dgr5"], 4), "dgr10": round(r["dgr10"], 4),
            "yield": None if yld is None else round(yld, 4),
            "ttm_dps": None if ttm_dps is None else round(ttm_dps, 4), "dps_basis": dps_basis,
            "price": r["price"],
            "payout": None if payout is None else round(payout, 4), "payout_basis": payout_basis,
            "last_increase": last_inc, "blocked": yield_block(yld, r["dgr10"]),
            "flags": "; ".join(flags), "as_of": today.isoformat(),
        })

    # Rank: Crown-score, then tie-breakers (higher dgr10, lower payout, longer streak).
    rows.sort(key=lambda x: (-x["crown_score"], -x["dgr10"],
                             x["payout"] if x["payout"] is not None and x["payout"] >= 0 else 9, -x["streak"]))
    for rank, x in enumerate(rows, 1):
        x["rank"] = rank

    fields = ["rank", "symbol", "company", "sector", "crown_score", "pts_streak", "pts_mcap", "pts_intl", "pts_dgr5",
              "pts_dgr10", "pts_yield", "pts_payout", "streak", "market_cap", "intl_pct", "intl_basis", "dgr5", "dgr10",
              "yield", "ttm_dps", "dps_basis",
              "price", "payout", "payout_basis", "last_increase", "blocked", "flags", "as_of"]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} scored companies -> {OUT.relative_to(ROOT)}"
          f"  (INTL_SALES_FACTOR={'ON' if INTL_SALES_FACTOR else 'OFF'}, max score {15 if INTL_SALES_FACTOR else 13})")

    selected = select_portfolio(rows)
    per_sector = {}
    for x in selected:
        per_sector[x["sector"]] = per_sector.get(x["sector"], 0) + 1

    print(f"\nSelection preview ({len(selected)} names):")
    print(f"{'rank':>4} {'sym':6} {'Crown':>5}  {'sector':24} {'yield':>6} {'payout':>7}  flags")
    for x in selected:
        y = f"{x['yield']:.1%}" if x["yield"] is not None else "-"
        p = f"{x['payout']:.0%}" if x["payout"] is not None else "-"
        print(f"{x['rank']:>4} {x['symbol']:6} {x['crown_score']:>5}  {x['sector'][:24]:24} {y:>6} {p:>7}  {x['flags']}")
    print("\nPer sector:", dict(sorted(per_sector.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
