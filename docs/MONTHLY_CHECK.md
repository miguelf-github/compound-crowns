# Monthly check

Compound Crowns trades once a month, on the **first trading day of the month at about 10:30 ET**. The monthly check is a dry run: it reports what happened since last month and what the bot would do, and it places nothing until you approve.

## What the report covers

Each run writes `data/reports/run_<date>_dryrun.md` and prints the same report:

1. **Account check:** confirms a paper account (Alpaca paper account numbers start with `PA`) and whether the market is open.
2. **Dividend check:** dividends received since the last run (by stock, with the total) and dividends declared but not yet paid. There is no DRIP: dividends are pooled with cash and sent to the most underweight sector first.
3. **Sector weights:** each sector's weight vs. its target, most underweight first.
4. **Orders planned and skipped:** each buy or sell with quantity, limit price, value and reason, plus what was skipped and why.
5. **Decisions needed:** holdings a sell rule flagged (dividend cut or suspension, freeze, corporate event). They get no new money until you choose `--approve-sell SYM` (sell and rotate) or `--keep SYM` (keep it Dethroned and appoint a Regent).
6. **Dethroned holdings and Court Picks**, when you have any.
7. **Alerts:** overweight positions (flagged, never trimmed), dividend-reserve warnings and data flags.
8. **Performance:** live-equivalent return vs. SPY and NOBL, including dividends.

## Approving the orders

Read the report. If you agree with the plan, place the orders:

```bash
uv run run_monthly.py --execute
```

The bot allows one executed run per calendar month. `--allow-second-run` exists only for re-running after a failed run.

## Scheduling it

Schedulers can't express "first trading day", so schedule the check on **days 1–7 of every month** and pass `--first-trading-day-only`. On any other day it checks Alpaca's market calendar, prints one line and exits.

```bash
uv run run_monthly.py --first-trading-day-only
```

Pick whichever scheduler you use. All of them run the **dry run only**; you approve orders yourself.

### macOS / Linux (cron)

`crontab -e`, then add (cron uses your machine's local time; `CRON_TZ` pins it to New York where supported):

```
CRON_TZ=America/New_York
33 10 1-7 * * cd /path/to/compound-crowns && uv run run_monthly.py --first-trading-day-only >> data/reports/scheduler.log 2>&1
```

### Windows (Task Scheduler)

Task Scheduler uses local time, so convert 10:33 ET to your time zone (07:33 on Pacific time, for example):

```bat
schtasks /Create /TN "Compound Crowns monthly check" /SC MONTHLY /D 1,2,3,4,5,6,7 /ST 07:33 /TR "cmd /c cd /d C:\path\to\compound-crowns && uv run run_monthly.py --first-trading-day-only >> data\reports\scheduler.log 2>&1"
```

### AI agents (Claude, Grok, Muse, Dots and others)

Create a scheduled task in your agent for 10:33 ET on days 1–7 of each month using the prompt in [AGENT_PROMPT.md](AGENT_PROMPT.md). It covers both brokers. A shorter Alpaca-only version:

> You run the monthly check for Compound Crowns, a buy-and-hold dividend growth bot on an Alpaca paper account, in `<path to compound-crowns>`.
> 1. Run `uv run run_monthly.py --first-trading-day-only`. If it says it isn't the first trading day, stop.
> 2. In January, if `data/crown_scores.csv` is from last year, say the annual re-screen is due (`uv run build_universe.py`, then `uv run compute_crown_scores.py`) and ask before running it.
> 3. Summarize the report: account check, dividend check, sector weights, orders planned and skipped, alerts, performance.
> 4. Ask me before placing anything. Only after I confirm, run `uv run run_monthly.py --execute` and report each order's result. Never pass `--allow-second-run` or `--deploy-all` unless I ask.

## Once a year

Every January, refresh the universe and scores before the monthly run:

```bash
uv run build_universe.py
uv run compute_crown_scores.py
```

The January re-screen never sells a holding just because it dropped out of the top 30; new names only fill open slots.
