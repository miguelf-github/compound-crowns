# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv", "tzdata", "defusedxml"]
# ///
"""Compound Crowns monthly run: Steps 3-8 of the methodology on the Alpaca PAPER account.

  uv run run_monthly.py            # dry run: builds the plan and report, places nothing
  uv run run_monthly.py --execute  # places the planned orders (market must be open)

Inputs:  data/crown_scores.csv (from compute_crown_scores.py) and data/crown_score_state.json (created on the first --execute).
Outputs: data/reports/run_<date>.md, and on --execute an updated data/crown_score_state.json.

Dividend ledger: Alpaca paper accounts never pay dividends, so each run rebuilds a ledger from Alpaca's
dividend history and the account's own fills (shares held before each ex-date x dividend per share).
A DIVIDEND_RESERVE of the paper cash is held back and released as dividends are credited, so the buy
engine reinvests dividend money that really exists in the account. Performance is reported as
account equity - unreleased reserve, i.e. what a live account with dividends would show.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from compute_crown_scores import MAX_PER_SECTOR, SECTORS_TO_HOLD, TARGET_POSITIONS, dividend_flags, select_portfolio

ROOT = Path(__file__).parent
CSCORES = ROOT / "data" / "crown_scores.csv"
STATE = ROOT / "data" / "crown_score_state.json"
REPORTS = ROOT / "data" / "reports"

TRADING = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets"

# ---- CONFIG (methodology) ----
DCA_MONTHS = 6
VALUE_OVERLAY = True
VALUE_TIME_STOP_MONTHS = 6
POSITION_HARD_CAP = 0.08
TRIM_TO = 0.06
CASH_BUFFER = 0.01
MAX_ORDERS_PER_RUN = 35
MIN_ORDER_USD = 25
BENCHMARKS = ("SPY", "NOBL")
# ---- additions agreed after the methodology review ----
DIVIDEND_RESERVE = 5_000.0      # paper cash held back and released as dividends are credited
# No DRIP: dividends are never reinvested automatically into the stock that paid them (keep any broker DRIP
# off too). Each run reports the dividends received since the last run, and the buy engine deploys them,
# with any other spendable cash, into the most underweight sectors first.
DRIP_ENABLED = False
# Low turnover: this is a buy-and-hold bot, not a trader. Every sale is a taxable event, so the only sells are
# rule exits (dividend cut, dividend freeze, corporate event). Overweight positions are not trimmed; new money
# goes to the underweight ones instead. Set TRIM_OVERWEIGHTS = True to trim above POSITION_HARD_CAP.
TRIM_OVERWEIGHTS = False
ONE_EXECUTED_RUN_PER_MONTH = True  # override with --allow-second-run
MAX_SPEND_PCT_PER_RUN = 0.40    # emergency stop: refuse --execute if buys exceed this share of equity
CSCORES_MAX_AGE_DAYS = 45       # warn when the screen is older than this
LIMIT_OFFSET = 0.001            # buy at ask + 0.10%, sell at bid - 0.10%
ORDER_PREFIX = "crown"


# ---------- Alpaca ----------

class Alpaca:
    def __init__(self, key, secret):
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def get(self, url, **params):
        r = self.s.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def paged(self, url, key, **params):
        """Collect a paginated data-API response; `key` is the top-level field holding results."""
        out, token = None, None
        while True:
            body = self.get(url, **params, **({"page_token": token} if token else {}))
            chunk = body.get(key) or {}
            if out is None:
                out = chunk
            elif isinstance(chunk, dict):
                for k, v in chunk.items():
                    if isinstance(out.get(k), list):
                        out[k] += v
                    else:
                        out[k] = v
            token = body.get("next_page_token")
            if not token:
                return out or {}

    def account(self):        return self.get(f"{TRADING}/account")
    def clock(self):          return self.get(f"{TRADING}/clock")
    def positions(self):      return {p["symbol"]: p for p in self.get(f"{TRADING}/positions")}
    def asset(self, sym):     return self.get(f"{TRADING}/assets/{sym.replace('-', '.')}")

    def orders(self, status="all", after=None):
        out, after = [], after or "2000-01-01T00:00:00Z"
        while True:
            batch = self.get(f"{TRADING}/orders", status=status, after=after, direction="asc", limit=500)
            out += batch
            if len(batch) < 500:
                return out
            after = batch[-1]["submitted_at"]

    def cancel(self, order_id):
        self.s.delete(f"{TRADING}/orders/{order_id}", timeout=30).raise_for_status()

    def submit(self, order):
        r = self.s.post(f"{TRADING}/orders", json=order, timeout=30)
        if r.status_code >= 400:
            return {"error": f"{r.status_code} {r.text[:200]}"}
        return r.json()

    def quotes(self, symbols):
        q = self.get(f"{DATA}/v2/stocks/quotes/latest", symbols=",".join(symbols), feed="iex").get("quotes", {})
        t = self.get(f"{DATA}/v2/stocks/trades/latest", symbols=",".join(symbols), feed="iex").get("trades", {})
        return {s: {"ask": (q.get(s) or {}).get("ap") or 0, "bid": (q.get(s) or {}).get("bp") or 0,
                    "last": (t.get(s) or {}).get("p") or 0} for s in symbols}

    def monthly_closes(self, symbols, start):
        bars = self.paged(f"{DATA}/v2/stocks/bars", "bars", symbols=",".join(symbols), timeframe="1Month",
                          start=start.isoformat(), end=(date.today() - timedelta(days=1)).isoformat(),
                          adjustment="split", feed="sip", limit=10000)
        return {s: [(date.fromisoformat(b["t"][:10]), b["c"]) for b in v] for s, v in bars.items()}

    def corporate_actions(self, symbols, start, end):
        return self.paged(f"{DATA}/v1/corporate-actions", "corporate_actions", symbols=",".join(symbols),
                          types="cash_dividend,forward_split,reverse_split",
                          start=start.isoformat(), end=end.isoformat(), limit=1000)

    def portfolio_history(self):
        return self.get(f"{TRADING}/account/portfolio/history", period="1A", timeframe="1D")


def alpaca_sym(s):  return s.replace("-", ".")
def our_sym(s):     return s.replace(".", "-")


# ---------- dividends ----------

def dividend_events(ca, today):
    """{symbol: [event, ...]} with split-restated rates, newest first. Events keep pay date and special flag."""
    splits = ca.get("forward_splits", []) + ca.get("reverse_splits", [])
    out = {}
    for d in ca.get("cash_dividends", []):
        ex = date.fromisoformat(d["ex_date"])
        factor = 1.0  # restate to today's share count
        for s in splits:
            if s["symbol"] == d["symbol"] and ex < date.fromisoformat(s["ex_date"]) <= today:
                factor *= s["old_rate"] / s["new_rate"]
        out.setdefault(our_sym(d["symbol"]), []).append({
            "ex_date": ex, "pay_date": date.fromisoformat(d.get("payable_date") or d["ex_date"]),
            "rate": d["rate"], "rate_today_shares": d["rate"] * factor, "special": bool(d.get("special")),
        })
    for v in out.values():
        v.sort(key=lambda e: e["ex_date"], reverse=True)
    return out, splits


def shares_before(fills, symbol, ex_date, splits):
    """Shares of `symbol` held at the close before `ex_date`, from the account's own fills (split-aware)."""
    qty = 0.0
    for f in fills:
        if f["symbol"] != symbol or f["date"] >= ex_date:
            continue
        q = f["qty"]
        for s in splits:  # a split between the fill and the ex-date changes the share count
            if our_sym(s["symbol"]) == symbol and f["date"] < date.fromisoformat(s["ex_date"]) <= ex_date:
                q *= s["new_rate"] / s["old_rate"]
        qty += q
    return qty


def build_ledger(events, fills, splits, today):
    """Every dividend the account would have earned since its first fill."""
    ledger = []
    for sym, evs in events.items():
        for e in evs:
            shares = shares_before(fills, sym, e["ex_date"], splits)
            if shares <= 0:
                continue
            ledger.append({
                "symbol": sym, "ex_date": e["ex_date"].isoformat(), "pay_date": e["pay_date"].isoformat(),
                "rate": e["rate"], "shares": round(shares, 6), "amount": round(shares * e["rate"], 2),
                "special": e["special"], "status": "credited" if e["pay_date"] <= today else "pending",
            })
    return sorted(ledger, key=lambda x: (x["pay_date"], x["symbol"]))


def ttm_dps(evs, as_of):
    return sum(e["rate_today_shares"] for e in evs if not e["special"] and as_of - timedelta(days=365) < e["ex_date"] <= as_of)


def yield_ratio(evs, closes, price, today):
    """Y = current yield / average of month-end yields over the last 60 months (None if < 36 months)."""
    cur = ttm_dps(evs, today) / price if price else 0
    hist = []
    for month_start, close in closes[-61:]:
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if month_end >= today.replace(day=1):  # skip the current, unfinished month
            continue
        d = ttm_dps(evs, month_end)
        if d and close:
            hist.append(d / close)
    hist = hist[-60:]
    if len(hist) < 36 or not cur:
        return None, cur, None
    avg = sum(hist) / len(hist)
    return cur / avg, cur, avg


def overlay_multiplier(y):
    if y is None:   return None
    if y >= 1.10:   return 2.0
    if y >= 1.00:   return 1.0
    if y >= 0.90:   return 0.5
    return 0.0


# ---------- selection (Step 3 and Step 6 replacement) ----------

def load_scores():
    with CSCORES.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["rank"], r["crown_score"] = int(r["rank"]), int(r["crown_score"])
    return sorted(rows, key=lambda r: r["rank"])


def replacement(scores, sector, selected_syms, sold_syms):
    per_sector = {}
    for r in scores:
        if r["symbol"] in selected_syms:
            per_sector[r["sector"]] = per_sector.get(r["sector"], 0) + 1
    pool = [r for r in scores if r["symbol"] not in selected_syms and r["symbol"] not in sold_syms
            and r["sector"] in SECTORS_TO_HOLD and not r.get("blocked")]
    same = next((r for r in pool if r["sector"] == sector), None)
    return same or next((r for r in pool if per_sector.get(r["sector"], 0) < MAX_PER_SECTOR), None)


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


# ---------- the run ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="place orders (default is a dry run)")
    ap.add_argument("--deploy-all", action="store_true",
                    help="one-time: buy every selected stock up to its full target now, skipping the DCA spread, "
                         "the value overlay and the per-run spending cap (user-approved initial deployment)")
    ap.add_argument("--allow-second-run", action="store_true",
                    help="allow a second executed run in the same calendar month")
    args = ap.parse_args()
    if DRIP_ENABLED:
        sys.exit("DRIP_ENABLED is not supported: dividends are pooled and sent to underweight sectors.")
    sys.stdout.reconfigure(encoding="utf-8")  # the report uses ✅/❌; Windows consoles default to cp1252
    load_dotenv(ROOT / ".env")
    key, secret = os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
    api = Alpaca(key, secret)
    now_utc = datetime.now(timezone.utc)
    today = date.today()
    notes, skipped, alerts, data_flags = [], [], [], []

    # --- hard safety rules 1 and 4 ---
    acct = api.account()
    is_paper = str(acct.get("account_number", "")).startswith("PA")
    if not is_paper:
        sys.exit(f"STOP: account {acct.get('account_number')} is not a paper account. No orders placed.")
    clock = api.clock()
    market_open = bool(clock.get("is_open"))
    cash, equity = float(acct["cash"]), float(acct["equity"])

    # --- inputs ---
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    first_run = state is None
    scores = load_scores()
    score_by = {r["symbol"]: r for r in scores}
    qs_age = (today - date.fromisoformat(scores[0]["as_of"])).days
    if qs_age > CSCORES_MAX_AGE_DAYS:
        data_flags.append(f"crown_scores.csv is {qs_age} days old; re-run build_universe.py and compute_crown_scores.py")
    if today.month == 1 and state and not state.get("last_full_screen", "").startswith(str(today.year)):
        notes.append("January: full re-screen due (build_universe.py, compute_crown_scores.py) before this run")

    positions = api.positions()
    orders = api.orders()
    fills = [{"symbol": our_sym(o["symbol"]), "date": date.fromisoformat(o["filled_at"][:10]),
              "qty": float(o["filled_qty"]) * (1 if o["side"] == "buy" else -1)}
             for o in orders if o.get("filled_at") and float(o.get("filled_qty") or 0) > 0]
    open_orders = [o for o in orders if o["status"] in ("new", "accepted", "partially_filled", "pending_new")]

    # --- selection ---
    if first_run:
        sel_rows = select_portfolio(scores)
        selected = [{"symbol": r["symbol"], "sector": r["sector"], "crown_score": r["crown_score"],
                     "selected_date": today.isoformat(), "first_buy_date": None} for r in sel_rows]
        sold_log = []
    else:
        selected, sold_log = state["selected"], state.get("sold_log", [])
    held_syms = {our_sym(s) for s in positions}
    universe_syms = sorted({s["symbol"] for s in selected} | held_syms)

    # --- market data ---
    # Alpaca filters on payment date, so look 60 days ahead for dividends already ex but not yet paid.
    ca = api.corporate_actions([alpaca_sym(s) for s in universe_syms + list(BENCHMARKS)],
                               today - timedelta(days=6 * 365 + 60), today + timedelta(days=60))
    events, splits = dividend_events(ca, today)
    closes = {our_sym(k): v for k, v in api.monthly_closes([alpaca_sym(s) for s in universe_syms],
                                                           today - timedelta(days=5 * 365 + 62)).items()}
    quotes = {our_sym(k): v for k, v in api.quotes([alpaca_sym(s) for s in universe_syms + list(BENCHMARKS)]).items()}

    # --- dividend ledger and synthetic (live-equivalent) equity ---
    ledger = build_ledger({s: events.get(s, []) for s in held_syms | {f["symbol"] for f in fills}}, fills, splits, today)
    credited = sum(x["amount"] for x in ledger if x["status"] == "credited")
    pending = sum(x["amount"] for x in ledger if x["status"] == "pending")
    reserve = state.get("dividend_reserve", DIVIDEND_RESERVE) if state else DIVIDEND_RESERVE
    reserve_left = max(0.0, reserve - credited)
    if credited > reserve:
        alerts.append(f"Dividend reserve exhausted: ${credited - reserve:,.2f} of credited dividends can't be "
                      f"reinvested from paper cash. Raise DIVIDEND_RESERVE or reset the account.")
    syn_equity = equity - reserve + credited
    spendable = max(0.0, cash - reserve_left - CASH_BUFFER * syn_equity)

    # --- Step 6: sell rules ---
    sells = []
    selected_syms = {s["symbol"] for s in selected}
    for sym in sorted(held_syms):
        pos = positions[alpaca_sym(sym)]
        qty, mv = float(pos["qty"]), float(pos["market_value"])
        regular = [(e["ex_date"], e["rate_today_shares"]) for e in events.get(sym, [])
                   if not e["special"] and e["ex_date"] <= today]
        _, flags = dividend_flags(regular, today)
        asset = api.asset(sym)
        reason = None
        if any(f.startswith("possible CUT") for f in flags):
            reason = "cut-sell: " + "; ".join(flags)
        elif any(f.startswith("possible FREEZE") for f in flags):
            reason = "freeze-sell: " + "; ".join(flags)
        elif not asset.get("tradable") or asset.get("status") != "active":
            reason = f"corporate event: asset status {asset.get('status')}, tradable={asset.get('tradable')}"
        elif sym not in selected_syms:
            alerts.append(f"{sym} is held but not in the selection (left alone)")
        if reason:
            sells.append({"symbol": sym, "qty": qty, "value": mv, "reason": reason, "full": True})
            alerts.append(f"SELL {sym}: {reason}")
        elif mv > POSITION_HARD_CAP * syn_equity and TRIM_OVERWEIGHTS:
            trim_value = mv - TRIM_TO * syn_equity
            sells.append({"symbol": sym, "qty": None, "value": trim_value, "full": False,
                          "reason": f"trim: weight {mv / syn_equity:.1%} > cap {POSITION_HARD_CAP:.0%}"})
        elif mv > POSITION_HARD_CAP * syn_equity:
            alerts.append(f"{sym} is {mv / syn_equity:.1%} of equity, above the {POSITION_HARD_CAP:.0%} cap. "
                          f"Not trimmed (TRIM_OVERWEIGHTS is off); new money goes to other holdings.")

    for s in sells:
        if s["full"] and s["symbol"] in selected_syms:
            sold_log.append({"symbol": s["symbol"], "date": today.isoformat(), "reason": s["reason"]})
            gone = next(x for x in selected if x["symbol"] == s["symbol"])
            selected.remove(gone)
            rep = replacement(scores, gone["sector"], {x["symbol"] for x in selected}, {x["symbol"] for x in sold_log})
            if rep:
                selected.append({"symbol": rep["symbol"], "sector": rep["sector"], "crown_score": rep["crown_score"],
                                 "selected_date": today.isoformat(), "first_buy_date": None})
                notes.append(f"{rep['symbol']} replaces {gone['symbol']} ({rep['sector']})")

    # --- Steps 4-5: buy engine ---
    target_weight = (1 - CASH_BUFFER) / len(selected)
    tranche = target_weight * syn_equity / DCA_MONTHS
    candidates, holdings = [], []
    for s in selected:
        sym = s["symbol"]
        pos = positions.get(alpaca_sym(sym))
        mv = float(pos["market_value"]) if pos else 0.0
        shares = float(pos["qty"]) if pos else 0.0
        target = target_weight * syn_equity
        gap = target - mv
        q = quotes.get(sym, {})
        price = q.get("last") or q.get("ask") or 0
        y, cur_yield, avg_yield = yield_ratio(events.get(sym, []), closes.get(sym, []), price, today)
        mult = overlay_multiplier(y) if VALUE_OVERLAY else 1.0
        waited = months_between(date.fromisoformat(s["selected_date"]), today)
        why = "DCA tranche" if mult == 1.0 else ("overlay 2x" if mult == 2.0 else "overlay 0.5x")
        if shares == 0 and waited >= VALUE_TIME_STOP_MONTHS and (mult is None or mult < 1):
            mult, why = 1.0, "time stop"
        regular = [(e["ex_date"], e["rate_today_shares"]) for e in events.get(sym, [])
                   if not e["special"] and e["ex_date"] <= today]
        last_inc, dflags = dividend_flags(regular, today)
        holdings.append({"symbol": sym, "sector": s["sector"], "crown_score": s["crown_score"], "y": y, "shares": shares,
                         "mv": mv, "weight": mv / syn_equity if syn_equity else 0, "target": target, "gap": gap,
                         "streak": "; ".join(dflags) or f"ok (last raise {last_inc})"})
        if gap <= 0:
            continue
        if args.deploy_all:
            mult, why = 1.0, "deploy-all: full target"
        if mult is None:
            skipped.append((sym, "data missing: under 36 months of yield history for the value overlay"))
            data_flags.append(f"{sym}: yield history too short for Y ratio")
            continue
        if mult == 0:
            skipped.append((sym, f"expensive: Y={y:.2f} < 0.90"))
            continue
        if any(o["symbol"] == alpaca_sym(sym) and o["side"] == "buy" for o in open_orders):
            skipped.append((sym, "open buy order already working"))
            continue
        candidates.append({"symbol": sym, "crown_score": s["crown_score"], "gap_pct": gap / target,
                           "value": gap if args.deploy_all else min(gap, tranche * mult), "reason": why, "quote": q})

    # Cash (including dividends) goes to the most underweight sector first, then the most underweight stock in it.
    sector_target, sector_mv = {}, {}
    for h in holdings:
        sector_target[h["sector"]] = sector_target.get(h["sector"], 0) + h["target"]
        sector_mv[h["sector"]] = sector_mv.get(h["sector"], 0) + h["mv"]
    sector_gap = {s: 1 - sector_mv[s] / t for s, t in sector_target.items() if t}
    sector_of = {h["symbol"]: h["sector"] for h in holdings}
    candidates.sort(key=lambda c: (-sector_gap.get(sector_of[c["symbol"]], 0), -c["gap_pct"], -c["crown_score"]))
    planned, budget = [], spendable
    for c in candidates:
        value = min(c["value"], budget)
        if value < MIN_ORDER_USD:
            skipped.append((c["symbol"], "insufficient cash" if budget < c["value"] else f"under ${MIN_ORDER_USD}"))
            continue
        ask = c["quote"].get("ask") or c["quote"].get("last")
        if not ask:
            skipped.append((c["symbol"], "data missing: no quote"))
            continue
        limit = round(ask * (1 + LIMIT_OFFSET), 2)
        if args.deploy_all and api.asset(c["symbol"]).get("fractionable"):
            qty = math.floor(value / limit * 1e4) / 1e4  # fractional, so the full target is invested
        elif value >= limit:
            qty = math.floor(value / limit)
        else:
            asset = api.asset(c["symbol"])
            if not asset.get("fractionable"):
                skipped.append((c["symbol"], "not fractionable and one share exceeds the order value"))
                continue
            qty = round(value / limit, 4)
        cost = qty * limit
        budget -= cost
        planned.append({"symbol": c["symbol"], "side": "buy", "qty": qty, "limit": limit, "value": cost,
                        "reason": c["reason"]})

    for s in sells:
        q = quotes.get(s["symbol"], {})
        bid = q.get("bid") or q.get("last")
        if not bid:
            skipped.append((s["symbol"], "data missing: no quote for sell"))
            continue
        limit = round(bid * (1 - LIMIT_OFFSET), 2)
        qty = s["qty"] if s["full"] else round(s["value"] / limit, 4)
        planned.insert(0, {"symbol": s["symbol"], "side": "sell", "qty": qty, "limit": limit,
                           "value": qty * limit, "reason": s["reason"]})

    if len(planned) > MAX_ORDERS_PER_RUN:
        for p in planned[MAX_ORDERS_PER_RUN:]:
            skipped.append((p["symbol"], f"over MAX_ORDERS_PER_RUN ({MAX_ORDERS_PER_RUN})"))
        planned = planned[:MAX_ORDERS_PER_RUN]
    buy_total = sum(p["value"] for p in planned if p["side"] == "buy")

    # --- execute ---
    placed = []
    if args.execute:
        if not market_open:
            sys.exit(f"Market is closed (next open {clock.get('next_open')}). Run without --execute for the plan.")
        if (ONE_EXECUTED_RUN_PER_MONTH and state and state.get("last_run", "")[:7] == today.isoformat()[:7]
                and not args.allow_second_run):
            sys.exit(f"Already executed a run this month ({state['last_run']}). This bot trades monthly; "
                     f"use --allow-second-run only if that run failed.")
        if buy_total > MAX_SPEND_PCT_PER_RUN * syn_equity and not args.deploy_all:
            sys.exit(f"Emergency stop: buys ${buy_total:,.0f} exceed {MAX_SPEND_PCT_PER_RUN:.0%} of equity.")
        for o in open_orders:  # rule 5: cancel stale orders from earlier runs
            if o["client_order_id"].startswith(ORDER_PREFIX) and o["submitted_at"][:10] < today.isoformat():
                api.cancel(o["id"])
                notes.append(f"cancelled stale order {o['symbol']} from {o['submitted_at'][:10]}")
        for p in planned:
            res = api.submit({
                "symbol": alpaca_sym(p["symbol"]), "qty": str(p["qty"]), "side": p["side"], "type": "limit",
                "time_in_force": "day", "limit_price": f"{p['limit']:.2f}",
                "client_order_id": f"{ORDER_PREFIX}-{today.isoformat()}-{p['symbol']}-{p['side']}",
            })
            p["result"] = res.get("error") or res.get("status")
            placed.append(p)
            if p["side"] == "buy" and not res.get("error"):
                s = next(x for x in selected if x["symbol"] == p["symbol"])
                s["first_buy_date"] = s["first_buy_date"] or today.isoformat()

    # --- performance ---
    bench_now = {b: quotes.get(b, {}).get("last") for b in BENCHMARKS}
    if state and state.get("bench_start"):
        bench_start = state["bench_start"]
    else:
        bench_start = {"date": today.isoformat(), "equity": syn_equity, **bench_now}

    def bench_return(b):
        start = bench_start.get(b)
        if not start or not bench_now.get(b):
            return None
        divs = sum(e["rate_today_shares"] for e in events.get(b, [])
                   if date.fromisoformat(bench_start["date"]) < e["ex_date"] <= today)
        return (bench_now[b] + divs) / start - 1

    port_return = syn_equity / bench_start["equity"] - 1
    hist = api.portfolio_history()
    max_dd, peak = 0.0, None
    credited_by_day = {}
    for x in ledger:
        if x["status"] == "credited":
            credited_by_day[x["pay_date"]] = credited_by_day.get(x["pay_date"], 0) + x["amount"]
    for ts, eq in zip(hist.get("timestamp") or [], hist.get("equity") or []):
        if eq is None or datetime.fromtimestamp(ts, timezone.utc).date() < date.fromisoformat(bench_start["date"]):
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        syn = eq - reserve + sum(v for k, v in credited_by_day.items() if k <= d)
        peak = max(peak or syn, syn)
        max_dd = min(max_dd, syn / peak - 1)
    divs_ttm = sum(x["amount"] for x in ledger if x["status"] == "credited"
                   and date.fromisoformat(x["pay_date"]) > today - timedelta(days=365))
    fwd_income, cost_basis = 0.0, 0.0
    for sym in held_syms:
        pos = positions[alpaca_sym(sym)]
        regular = [e for e in events.get(sym, []) if not e["special"]]
        per_year = sum(1 for e in regular if e["ex_date"] > today - timedelta(days=365)) or 4
        if regular:
            fwd_income += float(pos["qty"]) * regular[0]["rate_today_shares"] * per_year
        cost_basis += float(pos["cost_basis"])
    invested = sum(float(p["market_value"]) for p in positions.values())

    # --- report (Step 7) ---
    def pct(v):  return "-" if v is None else f"{v:+.2%}"
    def ratio(v): return "-" if v is None else f"{v:.2f}"
    lines = [
        f"RUN DATE / TIME (ET): {datetime.now(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M}"
        f"  ({'EXECUTE' if args.execute else 'DRY RUN - nothing placed'})",
        f"ACCOUNT CHECK: paper {'✅' if is_paper else '❌'} ({acct['account_number']}) | cash ${cash:,.2f} | "
        f"equity ${equity:,.2f} | buying power ${float(acct['buying_power']):,.2f}",
        f"DIVIDEND LEDGER: reserve ${reserve:,.2f} | credited ${credited:,.2f} | pending ${pending:,.2f} | "
        f"reserve left ${reserve_left:,.2f} | live-equivalent equity ${syn_equity:,.2f} | spendable ${spendable:,.2f}",
        f"MARKET STATUS: {'open' if market_open else 'closed'} (next open {clock.get('next_open', '')[:16]})",
        f"SCREEN USED: {scores[0]['as_of']} crown_scores.csv ({len(scores)} scored) | "
        f"{'first run: selection built now' if first_run else 'selection from state'} | re-screened this run? N",
        "",
        f"DIVIDEND CHECK (DRIP off: dividends are pooled with cash and sent to the most underweight sectors):",
    ]
    last_run = (state or {}).get("last_run")
    received = [x for x in ledger if x["status"] == "credited" and (not last_run or x["pay_date"] > last_run)]
    upcoming = [x for x in ledger if x["status"] == "pending"]
    lines.append(f"received since {last_run or 'inception'}: {len(received)} payments, "
                 f"${sum(x['amount'] for x in received):,.2f} | declared, not yet paid: {len(upcoming)} payments, "
                 f"${sum(x['amount'] for x in upcoming):,.2f}")
    for x in received:
        lines.append(f"- {x['symbol']}: ${x['amount']:,.2f} paid {x['pay_date']}{' (special)' if x['special'] else ''}")
    lines += ["", "SECTOR WEIGHTS (buys go to the most underweight sector first):",
              "| sector | weight | target | gap |", "|---|---|---|---|"]
    for sec in sorted(sector_target, key=lambda s: -sector_gap.get(s, 0)):
        lines.append(f"| {sec} | {sector_mv[sec] / syn_equity:.1%} | {sector_target[sec] / syn_equity:.1%} | "
                     f"{sector_gap.get(sec, 0):.0%} |")
    lines += [
        "",
        "HOLDINGS TABLE:",
        "| symbol | sector | Crown | Y ratio | shares | mkt value | weight | target | gap | streak status |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for h in sorted(holdings, key=lambda h: -h["gap"]):
        lines.append(f"| {h['symbol']} | {h['sector']} | {h['crown_score']} | {ratio(h['y'])} | "
                     f"{h['shares']:g} | ${h['mv']:,.0f} | {h['weight']:.1%} | ${h['target']:,.0f} | ${h['gap']:,.0f} | {h['streak']} |")
    lines += ["", f"ORDERS {'PLACED' if args.execute else 'PLANNED'} ({len(planned)}, buys ${buy_total:,.2f}):",
              "| symbol | side | qty | type | limit | value | reason | result |", "|---|---|---|---|---|---|---|---|"]
    for p in placed or planned:
        lines.append(f"| {p['symbol']} | {p['side']} | {p['qty']:g} | limit day | ${p['limit']:.2f} | ${p['value']:,.2f} | "
                     f"{p['reason']} | {p.get('result', '-')} |")
    lines += ["", "ORDERS SKIPPED:"] + ([f"- {s}: {r}" for s, r in skipped] or ["- none"])
    lines += ["", "ALERTS (sell rules, overweights, reserve):"] + ([f"- {a}" for a in alerts] or ["- none"])
    lines += ["", "DATA FLAGS:"] + ([f"- {d}" for d in data_flags] or ["- none"])
    if notes:
        lines += ["", "NOTES:"] + [f"- {n}" for n in notes]
    lines += ["", "PERFORMANCE (since inception, live-equivalent incl. credited dividends):",
              f"portfolio {pct(port_return)} | SPY {pct(bench_return('SPY'))} | NOBL {pct(bench_return('NOBL'))} | "
              f"max drawdown {max_dd:.2%} | dividends received TTM ${divs_ttm:,.2f} | "
              f"forward annual dividend income ${fwd_income:,.2f} | yield on cost "
              f"{(fwd_income / cost_basis if cost_basis else 0):.2%}",
              f"DEPLOYMENT: {invested / syn_equity:.1%} invested | {1 - invested / syn_equity:.1%} cash",
              "", "DIVIDEND LEDGER (last 12 entries):",
              "| symbol | ex-date | pay date | rate | shares | amount | status |", "|---|---|---|---|---|---|---|"]
    for x in ledger[-12:]:
        lines.append(f"| {x['symbol']} | {x['ex_date']} | {x['pay_date']} | ${x['rate']:.4f} | {x['shares']:g} | "
                     f"${x['amount']:,.2f} | {x['status']}{' (special)' if x['special'] else ''} |")
    if not ledger:
        lines.append("| - | - | - | - | - | - | no dividends earned yet |")
    report = "\n".join(lines)
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"run_{today.isoformat()}{'' if args.execute else '_dryrun'}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport saved to {path.relative_to(ROOT)}")

    # --- state (Step 8): only an executed run changes it ---
    if args.execute:
        new_state = {
            "last_run": today.isoformat(),
            "last_full_screen": (state or {}).get("last_full_screen") or scores[0]["as_of"],
            "screen_source": "data/crown_scores.csv",
            "selected": [{**s, "target_weight": round(target_weight, 5),
                          "runs_unstarted": 0 if s["first_buy_date"] else months_between(
                              date.fromisoformat(s["selected_date"]), today)} for s in selected],
            "bench_start": bench_start,
            "sold_log": sold_log,
            "dividend_reserve": reserve,
            "dividend_ledger": ledger,
        }
        STATE.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
        print(f"State saved to {STATE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
