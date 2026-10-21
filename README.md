# Compound Crowns

**A dividend growth trading bot strategy.** Compound Crowns owns about 30 large companies that have raised their dividend for 25+ years in a row, chosen by a dividend-quality score and spread across sectors. Once a month it invests new cash and dividends into the most underweight sectors, and it sells only when a company cuts or freezes its dividend.

What it's built for:

- **Dollar-cost averaging you can forget about:** one run a month, new money and dividends go where the portfolio is lightest.
- **No expense ratio:** you own the stocks directly instead of a dividend fund.
- **Rising income:** companies with long records of growing their dividends, with no DRIP so income is steered deliberately.
- **Few taxable events:** buy-and-hold; no rebalancing sales, no trimming, sells only on dividend cuts, freezes or corporate events.

It aims for steadily rising income, not market-beating returns. Expect it to lag the S&P 500 in tech-led rallies and hold up better in drawdowns.

## Two ways to use it

| | Alpaca paper trading | Robinhood |
|---|---|---|
| Money | Simulated ($100k paper account) | Real |
| Who places orders | The bot, through Alpaca's API | You, or an AI agent you connect to Robinhood and approve |
| What the bot produces | Orders placed + monthly report | Monthly report + an order ticket (`data/orders/robinhood_ticket_<date>.csv`) |
| Dividends | Simulated by the bot's dividend ledger (Alpaca paper pays none) | Real; keep DRIP **off** in the Robinhood app |
| Set in `.env` | `BROKER=alpaca` | `BROKER=robinhood` |

Robinhood has no official stock-trading API for scripts, so in Robinhood mode the bot plans and writes the ticket, and the orders are placed through the Robinhood app or Robinhood's Agentic Trading connection for AI agents. Both modes use Alpaca's free market data, so a free Alpaca paper account is needed either way.

## Use it with any AI agent

[AGENTS.md](AGENTS.md) is the operating guide for agents: setup, the monthly routine, and the rules they must follow (never trade outside the plan, no rebalancing sales, no DRIP, keys stay in `.env`). Claude reads it through `CLAUDE.md`; for agents that don't read repo files (Grok, Muse, Dots or others), paste [docs/AGENT_PROMPT.md](docs/AGENT_PROMPT.md) into a monthly scheduled task.

## How it works

| Step | Script | What it does |
|---|---|---|
| 1. Universe | `build_universe.py` | Starts from the U.S. Dividend Champions list (25+ years of increases), plus manual additions. Any company trading on a US exchange is eligible, wherever it's based. Always drops MLPs (K-1s) and OTC stocks; drops REITs and BDCs in taxable accounts; then drops everything below the median market cap. |
| 2. Score | `compute_crown_scores.py` | Scores each company up to 15 on streak length, size, international sales, 5- and 10-year dividend growth, yield, and payout ratio (dividends ÷ free cash flow). |
| 3–8. Run | `run_monthly.py` | Picks the 30 (best name in every sector first, max 5 per sector), sizes buys, checks sell rules, checks dividends, and writes a run report. Alpaca: places limit orders. Robinhood: writes the order ticket. |

The full rule set is in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Crown-score

| Factor | Points |
|---|---|
| Dividend streak | 25–34 yrs = 1 · 35–49 = 2 · 50+ = 3 |
| Market cap (within universe) | top third = 2 · middle = 1 · bottom = 0 |
| International sales (share of revenue outside the US) | ≥40% = 2 · 20–39% = 1 · 1–19% = 0 · US-only = −1 |
| 5-yr dividend growth | ≥10% = 2 · 5–10% = 1 · 3–5% = 0 · <3% = −1 |
| 10-yr dividend growth | ≥8% = 3 · 4–8% = 2 · 2–4% = 0 · <2% = −1 |
| Yield | ≥3.5% = 2 · 1.5–3.5% = 1 · <1.5% = −1 |
| Payout (dividends ÷ FCF) | <60% = 1 · 60–89% = 0 · ≥90% or negative FCF = −2 |

**US companies that sell worldwide.** The universe is US-listed companies, but a business that sells only in the US is concentrated in a single market, so US-only revenue costs a point and broad international sales earn up to two. The share comes from the geographic revenue table tagged in each company's latest 10-K (or geographic business segments, such as Aflac Japan). A company with no geographic disclosure is treated as US-only and flagged.

**Yield rule.** A yield under 1.5% takes too long to compound into meaningful income, so such a company can only be selected if its 10-year dividend growth is 10% or more (the −1 yield point still applies).

### Monthly rules

This is a buy-and-hold bot, not a trader. It runs once a month and refuses a second executed run in the same month.

- **Buying:** each stock targets an equal weight (99% of equity ÷ 30). Normal runs buy one sixth of the target per month, scaled by a value check: yield vs. the stock's own 5-year average yield (cheap = 2×, fair = 1×, slightly rich = ½×, expensive = skip; a 6-month time stop buys anyway).
- **Dividends (no DRIP):** dividends are never reinvested automatically into the stock that paid them, and any broker DRIP should stay off. Each run checks for dividends received since the last run, reports them, and pools them with cash. The buy engine then sends that cash to the most underweight sector first, and within it to the most underweight stock.
- **Selling, only with your approval:** every sale is a taxable event. A dividend cut or suspension, a dividend freeze (no raise in 15+ months) or a corporate event only *flags* a holding; the bot pauses new money to it and asks you to decide (see below). Positions that grow overweight are never trimmed; the report flags them and new money goes elsewhere. The January re-screen never sells a holding just because it dropped out of the top 30.
- **Safety:** dry run by default. Alpaca: refuses to trade unless the account is paper and the market is open; never sends duplicate orders. Robinhood: never places orders; it writes a ticket for you or your approved agent.

### Dethroned, Regents and Court Picks

When a sell rule flags a holding, it appears under **Decisions needed** in the monthly report, gets no new money, and waits for your choice:

| Your choice | Command | What happens |
|---|---|---|
| Sell and rotate | `uv run run_monthly.py --execute --approve-sell SYM` | The bot sells it and the next-best company in the same sector takes the slot. |
| Keep it | `uv run run_monthly.py --execute --keep SYM` | The holding becomes **Dethroned**: you keep the shares, but it gets no new money. A **Regent**, the next-best company in the same sector, takes its slot and receives the new money. A Dethroned holding is only ever sold if you later approve it with `--approve-sell`. |
| Decide later | (nothing) | It stays paused and is listed again next month. |

**Court Picks** are companies you want to own even though the methodology cuts them. List up to **10** in `court_picks.txt`, one ticker per line. Each gets its own extra slot on top of the 30 at the same target weight, is bought with the monthly cash (without the value check), and is never sold by a rule. Delete the line to stop buying it.

### Dividend ledger (Alpaca paper)

Alpaca paper accounts don't pay dividends. Each run rebuilds a ledger from Alpaca's dividend history and the account's own fills (shares held before each ex-date × dividend per share). A $5,000 cash reserve is released as dividends are credited, so reinvested dividend money really exists in the account. Performance is reported as live-equivalent equity, and the SPY and NOBL benchmarks include their dividends.

## Sectors

The strategy aims to hold every GICS sector except Real Estate, listed in `SECTORS_TO_HOLD` in `compute_crown_scores.py`:

Communication Services · Consumer Discretionary · Consumer Staples · Energy · Financials · Health Care · Industrials · Information Technology · Materials · Utilities

Selection takes the best-scoring company in each sector that has an eligible Champion, then fills the remaining slots by Crown-score, at most 5 per sector. Which companies qualify changes over time, so the bot rebuilds the picture every January. A sector with no eligible company stays empty rather than relaxing the rules. Remove a sector from the list to keep it out entirely.

## Taxable or tax-advantaged account

Set `ACCOUNT_TYPE` in `.env`. It decides which kinds of dividend payers are eligible:

| | `ACCOUNT_TYPE=taxable` (default) | `ACCOUNT_TYPE=tax_advantaged` (IRA, 401(k) and similar) |
|---|---|---|
| REITs (Real Estate sector) | Excluded | Eligible; Real Estate is added to the sectors to hold |
| BDCs (business development companies) | Excluded | Eligible |
| MLPs and other partnerships | Excluded | Excluded |

REITs and BDCs pay mostly **ordinary** (non-qualified) dividends, which are taxed at your income-tax rate in a taxable account instead of the lower qualified-dividend rate. Inside a tax-advantaged account that difference doesn't apply, so they're allowed. MLPs are always excluded because they issue K-1 tax forms. After changing `ACCOUNT_TYPE`, re-run `build_universe.py` and `compute_crown_scores.py`. This is general information, not tax advice.

## Data sources

- **Dividend Champions list:** the U.S. Dividend Champions spreadsheet published by [Dividend Growth Investor](https://www.dividendgrowthinvestor.com/). Download it yourself into `data/raw/` (it isn't redistributed here).
- **Company profiles:** [Financial Modeling Prep](https://financialmodelingprep.com/) (free plan is enough for profiles).
- **Payout ratio:** SEC EDGAR XBRL company facts (free; needs a contact in `SEC_USER_AGENT`).
- **Dividends and prices:** [Alpaca](https://alpaca.markets/) free market data (both modes).
- **Trading:** Alpaca paper trading API, or a Robinhood order ticket you place yourself.

## Setup

1. Install [uv](https://docs.astral.sh/uv/). Each script declares its own dependencies, so there's nothing else to install.
2. Copy `.env.example` to `.env`. Set `BROKER`, and fill in a free FMP key, an SEC contact, and free Alpaca **paper** keys.
3. Download the current U.S. Dividend Champions spreadsheet into `data/raw/`, and update `CHAMPIONS_FILE` / `CHAMPIONS_SHEET` in `build_universe.py` if the file or sheet name changed.
4. Robinhood only: copy `examples/robinhood_holdings.example.csv` to `data/robinhood_holdings.csv` and list your holdings plus a `CASH` row with the amount to invest. Update it before each monthly run (an agent connected to Robinhood can do this for you). Turn dividend reinvestment off in the Robinhood app.
5. Check everything:

```bash
uv run check_setup.py
```

## Usage

```bash
uv run build_universe.py                           # step 1: data/universe.csv (+ excluded list with reasons)
uv run compute_crown_scores.py                     # step 2: data/crown_scores.csv and a 30-stock selection preview
uv run run_monthly.py                              # monthly check (dry run): report in data/reports/
uv run run_monthly.py --execute                    # alpaca: place the orders; robinhood: record the month after placing the ticket
uv run run_monthly.py --first-trading-day-only     # for schedulers: runs the check only on the month's first trading day
```

`--broker alpaca|robinhood` overrides `BROKER` for one run. `--deploy-all` (with `--execute`) is a one-time option that buys every selected stock up to its full target at once, instead of building positions over six months.

Re-run steps 1–2 every January (the full re-screen).

### Monthly check

The bot is built to run once a month, on the first trading day at about 10:30 ET. The monthly check is a dry run that reports dividends received, sector weights, the order plan and any alerts, and places nothing until you run `--execute`. [docs/MONTHLY_CHECK.md](docs/MONTHLY_CHECK.md) explains the report and how to schedule it with cron, Windows Task Scheduler or an AI agent.

## Choices in this version

- AbbVie is added manually (its streak counts Abbott's history before the 2013 spin-off).
- XOM and CVX always take the high-payout penalty; the "best name in every sector" rule still keeps Energy represented.
- Payout uses free cash flow for every sector, so capital-heavy utilities often score −2.

## Disclaimer

Compound Crowns is an independent project inspired by the publicly described rules of Nanalyze's Quantigence. It is not affiliated with or endorsed by Nanalyze, and its scoring weights are an approximation. Paper results overstate real results (idealized fills, simulated dividends). Nothing here is investment advice.
