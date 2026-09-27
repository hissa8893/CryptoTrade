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
| 3 | S2, S3, benchmarks, walk-forward, sensitivity, Monte Carlo | ✅ done |
| 4 | `trader serve`, daily job, catch-up, crash recovery, start/stop scripts | ✅ done |
| 5 | Dashboard + email alerts | ✅ done |
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

## Daily use (Phase 4)

Double-click in Finder, or run in Terminal from the CryptoTrade folder:

| | Terminal | Finder (macOS) |
|---|---|---|
| Start in the background | `./start.sh` | `start.command` |
| Is it running? | `./status.sh` | `status.command` |
| Stop | `./stop.sh` | `stop.command` |
| Restart | `./restart.sh` | `restart.command` |
| Watch the log | `./logs.sh -f` | `logs.command` |

`status` prints one line, for example:
`● running · PID 4242 · up 3h 12m · last successful run: day 2026-09-26 (finished 9 h ago) · next run: 2026-09-28 00:10 UTC`

What happens while it runs:

* Every day at **00:10 UTC** it processes the day that just closed, for four paper accounts:
  S1, S2 and S3 each with $10,000, plus a combined PORTFOLIO account running all three.
* **Asleep or switched off?** On start-up, and every 5 minutes, it checks for closed days it has
  not processed and runs them **in order**. Nothing is skipped and nothing runs twice.
* **Crash, kill or power cut mid-run?** Each day is saved in one all-or-nothing database
  transaction. An interrupted day leaves nothing behind and is simply re-run on the next start.
* **Exchange down or data bad?** It never trades on stale data. The day is marked skipped, an
  urgent alert is queued, and it retries automatically.
* A database backup is written to `data/backups/` after each successful run (last 14 kept).

`stop` asks the trader to finish any work in progress and exit cleanly. If it has not exited
within 30 seconds it is force-killed, and `stop` tells you so. `run-once` processes pending days in
the foreground without the background server (it refuses if the trader is already running).
The Windows `.bat` files are generated but **untested**.

## Dashboard (Phase 5)

Open **http://127.0.0.1:8765** while the trader is running (`start` opens it for you).
It refreshes itself every 60 seconds.

* **Status bar** (always visible): a health dot (✓ healthy · ! needs attention · ✕ problem;
  it turns to ✕ if the last successful run is more than 26 hours old), when the last run
  finished, a countdown to the next one, whether the price data is fresh, and the risk
  engine's state in plain words (for example "Circuit breaker on — new entries blocked").
* **Headline numbers:** equity, today's P&L, total return, drawdown from peak, open positions, open risk.
* **Equity vs Buy & Hold** (7D / 30D / 90D / ALL) with the drawdown underneath. Use the
  S1 / S2 / S3 / Portfolio buttons to switch account.
* **Open positions:** entry, price now, unrealized P&L, stop, and distance to the stop.
  Positions within 3% of their stop are highlighted.
* **Recent closed trades:** tap one to see its full decision trail
  (signal → risk check → LLM verdict → fill, for the entry and the exit).
* **Risk events** in plain English (newest first), a **strategy scoreboard**, and whether
  **alerts** are being delivered.
* **Research** (top right) lists every backtest and research report.

Gains are blue with ▲ and "+"; losses are vermillion with ▼ and "−". The colors are
colorblind-safe and never the only signal. The theme is dark by default; the button switches
to light, and your OS light/dark setting is respected.

The dashboard is **read-only** and makes **no requests to the internet** (everything is
served from this computer).

### Viewing it from another device (optional)

By default it only listens on this computer (`server.host: 127.0.0.1`), which is the safest
setting. To look from your phone later, either:

* **SSH tunnel** (keeps 127.0.0.1): `ssh -L 8765:127.0.0.1:8765 you@your-mac`, then open
  http://127.0.0.1:8765 on the device, or
* **Tailscale**: set `server.host` to your Mac's Tailscale IP (100.x.y.z) and restart. Other
  devices must sign in with the **dashboard login token** that the installer printed once.

Never use `0.0.0.0` (the config refuses it). Only the SHA-256 hash of the token is stored. If
you lost the token, delete `data/dashboard_token.sha256` and run `.venv/bin/trader init`
to get a new one.

### Email alerts

Put your SMTP details in `.env` (for Gmail: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
`SMTP_USER=you@gmail.com`, `SMTP_PASSWORD=<an App Password>`, `ALERT_EMAIL_FROM` and
`ALERT_EMAIL_TO`), then check it with `.venv/bin/trader doctor --send-test-alert`. You get:

* a one-line **daily summary** (equity, day P&L, open positions) for each account;
* an **urgent** email right away for: circuit breaker on, daily loss cap hit, a daily run
  failed or skipped, bad or missing price data, and the trader having been down for more
  than 26 hours (sent when it starts again).

Each alert is sent once, even if a day is re-run. `.venv/bin/trader check-heartbeat` sends
an urgent email if the trader is not running; Phase 6 schedules it for you.

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

## Research (Phase 3)

```bash
.venv/bin/trader research        # S1, S2, S3 + combined, on BTC, ETH, SOL, XRP  (~2 min)
.venv/bin/trader verify lookahead -s S1 -s S2 -s S3 -a BTC -a ETH -a SOL -a XRP
```

The research report's headline numbers are all **out-of-sample** (walk-forward: settings are
picked on 2 years of history, then scored untouched on the next 6 months, rolling forward).
It also shows parameter-sensitivity heatmaps, a Monte Carlo of drawdowns (1,000 reshuffles
of the trade order: plan for the "worst 5%" figure), results split by bull / bear / sideways
market, and the **fixed default settings** over the same span.

How to read it: if the fixed defaults beat the walk-forward columns, re-optimising is fitting
noise. Keep the defaults, and never change them to chase a better backtest; that is exactly
how backtests end up looking great and trading badly.

Strategies: **S1** Donchian breakout with a chandelier trailing stop; **S2** Supertrend flip
above the 200-day average; **S3** 30/90-day momentum above the 200-day average, sized so
each coin gets an equal share of a 40%-a-year volatility budget, rebalanced only when the
target moves more than 20%.

Circuit breaker note: the breaker caps each losing *episode* near 15%. If it trips while
everything is in cash, it re-arms after 30 days (`risk.drawdown_rearm_days`), because flat
equity can never recover on its own. So the all-time peak-to-trough drawdown can exceed 15%.

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
