# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv"]
# ///
"""Compound Crowns setup check: tells you what's missing before the first run.

Run:  uv run check_setup.py
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
ok_count = problems = 0


def check(passed, label, fix=""):
    global ok_count, problems
    if passed:
        ok_count += 1
        print(f"  [ok]   {label}")
    else:
        problems += 1
        print(f"  [fix]  {label}" + (f"\n         -> {fix}" if fix else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("Compound Crowns setup check\n")
    env = ROOT / ".env"
    check(env.exists(), ".env file exists", "copy .env.example to .env and fill it in")
    load_dotenv(env)

    broker = (os.getenv("BROKER", "") or "alpaca").strip().lower()
    check(broker in ("alpaca", "robinhood"), f"BROKER is alpaca or robinhood (now: {broker})",
          "set BROKER=alpaca (paper trading) or BROKER=robinhood (ticket mode) in .env")
    account = (os.getenv("ACCOUNT_TYPE", "") or "taxable").strip().lower()
    check(account in ("taxable", "tax_advantaged"), f"ACCOUNT_TYPE is taxable or tax_advantaged (now: {account})",
          "set ACCOUNT_TYPE=taxable (REITs/BDCs excluded) or tax_advantaged (IRA/401(k): REITs/BDCs allowed)")
    check(bool(os.getenv("FMP_API_KEY", "").strip()), "FMP_API_KEY is set",
          "free key at https://financialmodelingprep.com/ (used for company profiles)")
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    check("@" in ua and " " in ua, "SEC_USER_AGENT has a name and email",
          "the SEC requires a contact on every request, e.g. SEC_USER_AGENT=Jane Doe jane@example.com")

    key, secret = os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
    check(bool(key and secret), "ALPACA_API_KEY / ALPACA_SECRET_KEY are set",
          "free paper account at https://alpaca.markets/ (needed in both modes: prices and dividend history)")
    if key and secret:
        h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        try:
            acct = requests.get("https://paper-api.alpaca.markets/v2/account", headers=h, timeout=20)
            check(acct.status_code == 200, "Alpaca paper keys work",
                  f"Alpaca answered {acct.status_code}; use keys from the PAPER trading dashboard")
            if acct.status_code == 200 and broker == "alpaca":
                number = acct.json().get("account_number", "")
                check(number.startswith("PA"), f"Alpaca account is a paper account ({number})",
                      "this bot only trades Alpaca paper accounts")
            data = requests.get("https://data.alpaca.markets/v2/stocks/trades/latest",
                                params={"symbols": "KO", "feed": "iex"}, headers=h, timeout=20)
            check(data.status_code == 200, "Alpaca market data works", f"Alpaca data answered {data.status_code}")
        except requests.RequestException as e:
            check(False, "Alpaca is reachable", str(e))

    sheets = sorted(RAW.glob("*Dividend_Champions*.xlsx")) if RAW.exists() else []
    check(bool(sheets), "Dividend Champions spreadsheet is in data/raw/",
          "download the current U.S. Dividend Champions spreadsheet (Dividend Growth Investor) into data/raw/, "
          "then set CHAMPIONS_FILE / CHAMPIONS_SHEET in build_universe.py")

    if broker == "robinhood":
        holdings = ROOT / "data" / "robinhood_holdings.csv"
        check(holdings.exists(), "data/robinhood_holdings.csv exists",
              "copy examples/robinhood_holdings.example.csv there; list your holdings and a CASH row")
        print("\n  Robinhood reminders: turn dividend reinvestment (DRIP) OFF in the Robinhood app; this bot\n"
              "  never places Robinhood orders itself. It writes a ticket that you or your approved agent place.")

    scores = ROOT / "data" / "crown_scores.csv"
    if not scores.exists():
        print("\n  Next: uv run build_universe.py, then uv run compute_crown_scores.py")
    print(f"\n{ok_count} ok, {problems} to fix.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
