# Compound Crowns

**A dividend growth trading bot strategy.** Compound Crowns holds about 30 large U.S. companies that have raised their dividend for 25+ years in a row, scores them on dividend quality, and runs a rules-based monthly process on an **Alpaca paper trading account**: buy the best-scoring names across sectors, reinvest dividends into the most underweight holdings, and sell only when a company cuts or freezes its dividend.

It aims for steadily rising income, not market-beating returns. Expect it to lag the S&P 500 in tech-led rallies and hold up better in drawdowns.

> Paper trading only. This is a personal research project, not investment advice.

## How it works

| Step | Script | What it does |
|---|---|---|
| 1. Universe | `build_universe.py` | Starts from the U.S. Dividend Champions list (25+ years of increases), plus manual additions. Drops ADRs, Canadian primary listings, REITs, MLPs, OTC stocks and everything below the median market cap. |
| 2. Score | `compute_cscores.py` | Scores each company up to 15 on streak length, size, international sales, 5- and 10-year dividend growth, yield, and payout ratio (dividends ÷ free cash flow). |
| 3–8. Run | `run_monthly.py` | Picks the 30 (best name in every sector first, max 5 per sector), sizes buys, checks sell rules, places limit orders, tracks dividends, and writes a run report. |

The full rule set is in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### C-score

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

### Monthly rules

- **Buying:** each stock targets an equal weight (99% of equity ÷ 30). Normal runs buy one sixth of the target per month, scaled by a value check: yield vs. the stock's own 5-year average yield (cheap = 2×, fair = 1×, slightly rich = ½×, expensive = skip; a 6-month time stop buys anyway).
- **Selling:** only for a dividend cut, a dividend freeze (no raise in 15+ months), a corporate event, or a trim if a position grows past 8%.
- **Safety:** refuses to trade unless the account is paper and the market is open; never sends duplicate orders; dry run by default.

### Dividend ledger

Alpaca paper accounts don't pay dividends. Each run rebuilds a ledger from Alpaca's dividend history and the account's own fills (shares held before each ex-date × dividend per share). A $5,000 cash reserve is released as dividends are credited, so reinvested dividend money really exists in the account. Performance is reported as live-equivalent equity, and the SPY and NOBL benchmarks include their dividends.

## Data sources

- **Dividend Champions list:** the U.S. Dividend Champions spreadsheet published by [Dividend Growth Investor](https://www.dividendgrowthinvestor.com/). Download it yourself into `data/raw/` (it isn't redistributed here).
- **Company profiles:** [Financial Modeling Prep](https://financialmodelingprep.com/) (free plan is enough for profiles).
- **Payout ratio:** SEC EDGAR XBRL company facts (free; needs a contact in `SEC_USER_AGENT`).
- **Dividends, prices, trading:** [Alpaca](https://alpaca.markets/) paper trading API.

## Setup

1. Install [uv](https://docs.astral.sh/uv/). Each script declares its own dependencies, so there's nothing else to install.
2. Copy `.env.example` to `.env` and fill in your FMP key, SEC contact, and Alpaca **paper** keys.
3. Download the current Dividend Champions spreadsheet into `data/raw/` and update `CHAMPIONS_FILE` / `CHAMPIONS_SHEET` in `build_universe.py` if the file name or sheet name changed.

## Usage

```bash
uv run build_universe.py          # step 1: data/universe.csv (+ excluded list with reasons)
uv run compute_cscores.py         # step 2: data/cscores.csv and a 30-stock selection preview
uv run run_monthly.py             # dry run: report in data/reports/, places nothing
uv run run_monthly.py --execute   # place the planned orders (market must be open)
```

`--deploy-all` (with `--execute`) is a one-time option that buys every selected stock up to its full target at once, instead of building positions over six months.

Re-run steps 1–2 every January (the full re-screen); run step 3 on the first trading day of each month.

## Choices in this version

- AbbVie is added manually (its streak counts Abbott's history before the 2013 spin-off).
- XOM and CVX always take the high-payout penalty; the "best name in every sector" rule still keeps Energy represented.
- Payout uses free cash flow for every sector, so capital-heavy utilities often score −2.

## Disclaimer

Compound Crowns is an independent project inspired by the publicly described rules of Nanalyze's Quantigence. It is not affiliated with or endorsed by Nanalyze, and its scoring weights are an approximation. Paper results overstate real results (idealized fills, simulated dividends). Nothing here is investment advice.
