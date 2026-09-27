# CryptoTrade — AI daily crypto paper-trading agent

> **This is a simulation.** It never places real orders, never holds exchange API keys,
> and refuses to start unless `mode: paper`. Backtest results do not predict future returns.

A daily (1d candles, UTC) crypto trading agent that **paper-trades only**: a rigorous
event-driven backtester, a once-a-day runtime that simulates trades, a SQLite trade
journal, and a local monitoring dashboard.

## Status

Built in phases; each phase ends with a validation report.

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, config, CLI, installer + `doctor`, DB schema/migrations, data fetch/cache/validation, indicators | ✅ done |
| 2 | SimBroker, RiskManager, S1, BTC backtest + look-ahead proof | ✅ done |
| 3 | S2, S3, benchmarks, walk-forward, sensitivity, Monte Carlo | pending |
| 4 | `trader serve`, daily job, catch-up, crash recovery, start/stop scripts | pending |
| 5 | Dashboard + email alerts | pending |
| 6 | Hardening, clean-install test, service install, full README | pending |
| 7 | Optional LLM analyst layer | pending |

## Install (macOS / Linux)

Requires Python 3.11–3.14 (`brew install python@3.12` on macOS). Works on Apple Silicon and Intel Macs:
every pinned package has a prebuilt wheel for both, so no compiler is needed.

```bash
./install.sh          # or double-click install.command in Finder
```

The installer is idempotent (safe to re-run). It creates `.venv`, installs the pinned
dependencies, creates `data/ logs/ run/ reports/`, copies `config.example.yaml → config.yaml`
and `.env.example → .env` (only if missing, `.env` set to owner-only), runs DB migrations,
downloads price history, and finishes with `trader doctor`.

> macOS: if Finder refuses to open a downloaded `.command` file, right-click → Open once,
> or run `xattr -d com.apple.quarantine *.command`.

## Useful commands (Phase 1)

There is no app to start yet: `start.sh` and the dashboard arrive in Phases 4–5. Run these
from the project folder (`cd ~/CryptoTrade` if you cloned it into your home folder):

```bash
.venv/bin/trader doctor            # ✅/❌ health checklist
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/trader doctor --full   # + dependency CVE audit
.venv/bin/trader data fetch        # download/refresh + validate daily candles
.venv/bin/trader data status       # what is cached, date ranges, freshness
.venv/bin/trader db status         # schema version and row counts
.venv/bin/trader db backup         # online backup to data/backups (keeps 14)
.venv/bin/python -m pytest         # run the test suite
```

## Backtesting (Phase 2)

```bash
.venv/bin/trader backtest -s S1 -a BTC            # event-driven backtest -> HTML report in reports/
.venv/bin/trader backtest -s S1 -a BTC --zero-costs --no-save   # sanity check: must beat the run with costs
.venv/bin/trader verify lookahead -s S1 -a BTC    # proof: decisions at day t never use data after t
```

Open the report file it prints (e.g. `open reports/backtest_S1_BTC_*.html` on macOS). Every
report shows red flags at the top (fewer than 30 trades, profit factor > 3, Sharpe > 3), a
SYNTHETIC banner if the prices were generated rather than downloaded, the equity curve vs
Buy & Hold, drawdowns, every metric, and every trade.

How the simulation trades: a decision made at the close of day t fills at the open of day
t+1 plus slippage, and pays a 0.10% fee per side. Stops are resting orders; if the price
opens below the stop, the fill is at that (worse) open price. The risk engine sizes every
trade to risk 1% of equity, caps each position at 25% of equity and total open risk at 4%,
allows at most 4 positions, and blocks new entries in a BTC bear market, after a 3% losing
day, during a 15% drawdown, and after 4 losing trades in a row.

## Market data

* Default exchange: **Bitstamp** (reachable from the US, USD pairs, 1000 daily candles per request).
  `trader data status` shows the actual first date retrieved for each coin.
  Coinbase Exchange is the fallback, used only for an asset Bitstamp does not list, so each
  price series always comes from a single exchange.
* Public endpoints only, through a read-only wrapper that refuses to hold credentials.
* Every fetch is validated: no duplicate timestamps, `high ≥ max(open, close)`,
  `low ≤ min(open, close)`, `volume ≥ 0`, and the still-open candle for today is never used.
  A malformed new batch never overwrites good cached data, and candles the exchange revises
  after the fact are reported.
* Gaps: up to 3 missing days are filled flat at the previous close and flagged (never traded
  on). A longer gap trims the history before it only if at least 400 days remain after it;
  a more recent long gap makes that coin **not tradable** (shown by `data status` and the
  doctor) until the exchange backfills it and you run `.venv/bin/trader data fetch --full-refresh`.
* The cache keeps the raw candles, so gap handling never destroys downloaded history.
* If the exchange is unreachable, each coin reports the failure and keeps its cached data;
  nothing trades on stale prices.
* `data.source: synthetic` generates clearly-labelled **synthetic** prices for offline testing only.

## Configuration

* `config.yaml`: all settings, validated and bounded (e.g. risk per trade must be 0.1%–2%;
  unknown keys are rejected; the dashboard can never bind to `0.0.0.0`).
* `.env`: secrets only (SMTP credentials for email alerts, optional LLM key). Gitignored.
