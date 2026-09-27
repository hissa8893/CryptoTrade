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
| 2 | SimBroker, RiskManager, S1, BTC backtest + look-ahead proof | pending |
| 3 | S2, S3, benchmarks, walk-forward, sensitivity, Monte Carlo | pending |
| 4 | `trader serve`, daily job, catch-up, crash recovery, start/stop scripts | pending |
| 5 | Dashboard + email alerts | pending |
| 6 | Hardening, clean-install test, service install, full README | pending |
| 7 | Optional LLM analyst layer | pending |

## Install (macOS / Linux)

Requires Python 3.11 or 3.12 (`brew install python@3.12` on macOS).

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
.venv/bin/trader doctor            # ✅/❌ health checklist  (--full adds pip-audit)
.venv/bin/trader data fetch        # download/refresh + validate daily candles
.venv/bin/trader data status       # what is cached, date ranges, freshness
.venv/bin/trader db status         # schema version and row counts
.venv/bin/trader db backup         # online backup to data/backups (keeps 14)
.venv/bin/python -m pytest         # run the test suite
```

## Market data

* Default exchange: **Bitstamp** (reachable from the US, USD pairs, BTC history back to 2011).
  Coinbase Exchange is the fallback, used only for an asset Bitstamp does not list, so each
  price series always comes from a single exchange.
* Public endpoints only, through a read-only wrapper that refuses to hold credentials.
* Every fetch is validated: no duplicate timestamps, no gaps (gaps ≤ 3 days are filled flat
  and flagged, longer gaps trim history and are reported), `high ≥ max(open, close)`,
  `low ≤ min(open, close)`, `volume ≥ 0`, and the still-open candle for today is never used.
* `data.source: synthetic` generates clearly-labelled **synthetic** prices for offline testing only.

## Configuration

* `config.yaml`: all settings, validated and bounded (e.g. risk per trade must be 0.1%–2%;
  unknown keys are rejected; the dashboard can never bind to `0.0.0.0`).
* `.env`: secrets only (SMTP credentials for email alerts, optional LLM key). Gitignored.
