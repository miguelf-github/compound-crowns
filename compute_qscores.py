# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv"]
# ///
"""Compound Crown Step 2: Q-score the universe.

Reads data/universe.csv and scores each company per the methodology, using free data:
  - payout  = TTM common dividends paid / TTM free cash flow, from SEC EDGAR XBRL filings
              (falls back to dividends / net income when cash flow can't be built)
  - yield   = TTM dividends per share / current price. Dividend history comes from Alpaca when
              ALPACA_API_KEY / ALPACA_SECRET_KEY are in .env (split-adjusted, specials excluded);
              otherwise from per-share dividends declared in SEC filings.
  - streak, dgr5, dgr10 come from the Dividend Champions spreadsheet (via universe.csv).
International sales is skipped while INTL_SALES_FACTOR is False.

Writes data/qscores.csv and prints a preview of the Step 3 selection.
SEC responses are cached in data/cache/sec for CACHE_DAYS.

Run:  uv run compute_qscores.py
"""
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
UNIVERSE = ROOT / "data" / "universe.csv"
OUT = ROOT / "data" / "qscores.csv"
CACHE = ROOT / "data" / "cache" / "sec"
CACHE_DAYS = 7

INTL_SALES_FACTOR = False
TARGET_POSITIONS = 30
MAX_PER_SECTOR = 5
FREEZE_MONTHS = 15
MAX_DPS_AGE_DAYS = 270  # older SEC per-share dividend data counts as missing
# User rule: these always take the high-payout penalty (-2), whatever the computed ratio.
PAYOUT_PENALTY_OVERRIDES = {
    "XOM": "known high payout ratio (user rule)",
    "CVX": "known high payout ratio (user rule)",
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


def durations(facts, concepts, unit):
    """Duration facts as {(start, end): value}, merged across equivalent concepts.

    Companies rename concepts over time (e.g. operating cash flow moving to a 'continuing operations'
    tag), so all candidates are merged; for the same period the earlier concept in the list wins,
    and within a concept the latest filing wins.
    """
    out = {}
    for concept in reversed(concepts):
        node = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit, [])
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
    div_periods = durations(facts, DIVIDENDS_PAID, "USD")
    divs, end = ttm(div_periods)
    if divs is None:
        return None, "dividends paid not found in SEC data", []
    flags = []
    ocf, _ = ttm(durations(facts, OPERATING_CF, "USD"), end)
    if ocf is not None:
        capex, _ = ttm(durations(facts, CAPEX, "USD"), end)
        if capex is None:
            flags.append("no capex line in SEC data; payout uses operating cash flow")
        fcf = ocf - (capex or 0)
        basis = "FCF" if capex is not None else "OCF only"
        return (abs(divs) / fcf if fcf > 0 else -1.0), f"{basis}, TTM to {end}", flags
    ni, _ = ttm(durations(facts, NET_INCOME, "USD"), end)
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


# ---------- scoring (methodology Step 2) ----------

def pts_streak(y):   return 3 if y >= 50 else 2 if y >= 35 else 1
def pts_dgr5(g):     return 2 if g >= .10 else 1 if g >= .05 else 0 if g >= .03 else -1
def pts_dgr10(g):    return 3 if g >= .08 else 2 if g >= .04 else 0 if g >= .02 else -1
def pts_yield(y):    return 2 if y >= .035 else 1 if y >= .015 else -1
def pts_payout(p):   return -2 if p < 0 or p >= .90 else 0 if p >= .60 else 1


def main():
    load_dotenv(ROOT / ".env")
    today = date.today()
    sec = requests.Session()
    sec.headers["User-Agent"] = os.getenv("SEC_USER_AGENT", "").strip() or "Compound Crown research script (personal use)"

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
    yield_source = "Alpaca dividend history" if alpaca_hist is not None else "SEC dividends per share declared"
    print(f"Yield source: {yield_source}")

    rows = []
    for i, r in enumerate(universe, 1):
        sym = r["symbol"]
        print(f"  [{i:2}/{n}] {sym}")
        flags = []
        cik = cik_by_ticker.get(sym.upper()) or cik_by_ticker.get(sym.upper().replace("-", "."))
        facts = sec_get(sec, SEC_FACTS.format(cik=cik), f"CIK{cik}.json") if cik else None
        if facts is None:
            flags.append("no SEC XBRL data")
            payout, payout_basis = None, "no SEC data"
        else:
            payout, payout_basis, payout_flags = payout_from_sec(facts)
            flags += payout_flags
            if payout is None:
                flags.append(f"payout missing ({payout_basis})")

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
        yld = ttm_dps / r["price"] if ttm_dps else None
        if yld is None:
            flags.append(f"yield missing ({dps_basis})")

        pts = {
            "pts_streak": pts_streak(r["streak"]),
            "pts_mcap": r["pts_mcap"],
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
            "qscore": sum(pts.values()), **pts,
            "streak": r["streak"], "market_cap": r["market_cap"],
            "dgr5": round(r["dgr5"], 4), "dgr10": round(r["dgr10"], 4),
            "yield": None if yld is None else round(yld, 4),
            "ttm_dps": None if ttm_dps is None else round(ttm_dps, 4), "dps_basis": dps_basis,
            "price": r["price"],
            "payout": None if payout is None else round(payout, 4), "payout_basis": payout_basis,
            "last_increase": last_inc, "flags": "; ".join(flags), "as_of": today.isoformat(),
        })

    # Rank: Q-score, then tie-breakers (higher dgr10, lower payout, longer streak).
    rows.sort(key=lambda x: (-x["qscore"], -x["dgr10"],
                             x["payout"] if x["payout"] is not None and x["payout"] >= 0 else 9, -x["streak"]))
    for rank, x in enumerate(rows, 1):
        x["rank"] = rank

    fields = ["rank", "symbol", "company", "sector", "qscore", "pts_streak", "pts_mcap", "pts_dgr5", "pts_dgr10",
              "pts_yield", "pts_payout", "streak", "market_cap", "dgr5", "dgr10", "yield", "ttm_dps", "dps_basis",
              "price", "payout", "payout_basis", "last_increase", "flags", "as_of"]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} scored companies -> {OUT.relative_to(ROOT)}"
          f"  (INTL_SALES_FACTOR={'ON' if INTL_SALES_FACTOR else 'OFF'}, max score 13)")

    # Step 3 preview: best per sector, then fill by rank respecting the sector cap.
    selected, per_sector = [], {}
    for x in rows:
        if x["sector"] not in per_sector:
            selected.append(x)
            per_sector[x["sector"]] = 1
    for x in rows:
        if len(selected) >= TARGET_POSITIONS:
            break
        if x in selected or per_sector[x["sector"]] >= MAX_PER_SECTOR:
            continue
        selected.append(x)
        per_sector[x["sector"]] += 1
    selected.sort(key=lambda x: x["rank"])

    print(f"\nSelection preview ({len(selected)} names):")
    print(f"{'rank':>4} {'sym':6} {'Q':>3}  {'sector':24} {'yield':>6} {'payout':>7}  flags")
    for x in selected:
        y = f"{x['yield']:.1%}" if x["yield"] is not None else "-"
        p = f"{x['payout']:.0%}" if x["payout"] is not None else "-"
        print(f"{x['rank']:>4} {x['symbol']:6} {x['qscore']:>3}  {x['sector'][:24]:24} {y:>6} {p:>7}  {x['flags']}")
    print("\nPer sector:", dict(sorted(per_sector.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
