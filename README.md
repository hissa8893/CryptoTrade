# CryptoTrade: a daily crypto paper-trading agent

> ## ⚠️ This is a simulation
> * It **never places real orders**. There is no code that can: it only reads *public* price
>   data, holds **no exchange API keys**, and refuses to start unless `mode: paper`.
> * All money shown is **pretend money** (4 paper accounts of $10,000 each).
> * **Backtests do not predict future returns.** A strategy that looked good on past prices can
>   and often does lose money later. Nothing here is financial advice.

Once a day, just after the daily candle closes (00:10 UTC), it looks at BTC, ETH, SOL and XRP,
lets three rule-based strategies decide what to buy or sell, checks every decision against a
strict risk manager, and records simulated trades in a local database. A dashboard on your own
computer shows how it is doing. The same engine runs a rigorous backtester, so you can see how
the rules would have done in the past, measured honestly.

---

## Contents
1. [Quick start (Mac)](#1-quick-start-mac)
2. [Installing](#2-installing)
3. [Daily use](#3-daily-use)
4. [The dashboard](#4-the-dashboard)
5. [Every number, in plain language](#5-every-number-in-plain-language)
6. [Email alerts](#6-email-alerts)
7. [Viewing it from your phone (optional)](#7-viewing-it-from-your-phone-optional)
8. [Backtests and research](#8-backtests-and-research)
9. [How the simulation trades](#9-how-the-simulation-trades)
10. [Market data](#10-market-data)
11. [Settings](#11-settings)
12. [The optional AI analyst](#12-the-optional-ai-analyst)
13. [Safety and security](#13-safety-and-security)
14. [Uninstalling](#14-uninstalling)
15. [Troubleshooting](#15-troubleshooting)
16. [For developers](#16-for-developers)
17. [What has and has not been verified](#17-what-has-and-has-not-been-verified)

---

## 1. Quick start (Mac)

In **Terminal**, from the folder you downloaded (for example `cd ~/CryptoTrade`):

```bash
./install.sh      # once: sets everything up and ends with a ✅/❌ health check (2-5 minutes)
./start.sh        # starts the trader in the background and opens the dashboard
```

The dashboard is at **http://127.0.0.1:8765**. That is all you need to do: it now runs by itself
every day while your Mac is on. If the Mac was asleep or off, it catches up on the missed days,
in order, the next time it runs.

```bash
./status.sh       # is it running? when did it last run?
./stop.sh         # stop it
.venv/bin/trader service install    # optional: start it automatically at every login
```

Prefer double-clicking? Use `install.command`, `start.command`, `status.command`,
`stop.command` in Finder (see [Troubleshooting](#15-troubleshooting) if macOS blocks them).

---

## 2. Installing

**You need Python 3.11, 3.12, 3.13 or 3.14.** On a Mac: `brew install python@3.12`
([Homebrew](https://brew.sh)) or the installer from python.org. Apple Silicon and Intel Macs
both work: every pinned package has a prebuilt download for both, so no compiler is needed.

```bash
./install.sh
```

The installer is safe to run again at any time. It:

1. finds a suitable Python and creates a private environment in `.venv/`
   (nothing is installed system-wide);
2. installs the exact, pinned package versions from `requirements.txt`;
3. creates `data/ logs/ run/ reports/`, copies `config.example.yaml → config.yaml` and
   `.env.example → .env` (only if they do not exist yet; `.env` is made readable by you only);
4. prints a **dashboard login token once**. You only need it to view the dashboard from
   another device (section 7). Only a hash of it is stored;
5. sets up the database, downloads the price history, and runs `trader doctor`, a health
   checklist that must end with no ❌.

Do **not** run it with `sudo`: it refuses, because that leaves files you cannot update later.

| System | How | Status |
|---|---|---|
| macOS | `./install.sh` or double-click `install.command` | the supported target |
| Linux | `./install.sh` | tested (the whole test suite runs on Linux) |
| Windows | double-click `install.bat` (runs `install.ps1`), then the `.bat` scripts | written but **never run on Windows** |
| No Python | optional self-contained build, see [16](#optional-self-contained-build-pyinstaller) | works on Linux; **unsigned**, see notes |

> **The `trader` command.** It lives inside the project, so type `.venv/bin/trader …` from the
> project folder (or `.venv\Scripts\trader …` on Windows). The start/stop/status scripts do
> this for you.

---

## 3. Daily use

| What | Terminal (from the project folder) | Finder |
|---|---|---|
| Start in the background | `./start.sh` | `start.command` |
| Is it running? | `./status.sh` | `status.command` |
| Stop | `./stop.sh` | `stop.command` |
| Restart | `./restart.sh` | `restart.command` |
| Read the log | `./logs.sh` (`-f` to keep following) | `logs.command` |
| Health check | `.venv/bin/trader doctor` | |

`status` prints one line, for example:

```
● running · PID 4242 · up 3h 12m · last successful run: day 2026-09-26 (finished 9 h ago) · next run: 2026-09-28 00:10 UTC
```

Starting twice is harmless: the second start says "already running". `stop` asks the trader to
finish what it is doing and exit. If it has not exited after 30 seconds it is forced to stop,
and `stop` tells you so. No work is lost either way (see below).

### What happens every day

* At **00:10 UTC** (evening in the US) it processes the day that just closed for four
  paper accounts: **S1**, **S2** and **S3** with $10,000 each, plus **PORTFOLIO**, which runs all
  three strategies together from its own $10,000.
* **Asleep or switched off?** At start-up, and every 5 minutes, it looks for closed days it has
  not processed and runs them **in date order**. Nothing is skipped and nothing runs twice.
* **Crash, forced stop or power cut in the middle of a run?** Each day is saved as one
  all-or-nothing database transaction. A half-finished day leaves no trace and simply runs
  again next time.
* **Exchange down or bad data?** It never trades on stale or broken prices. The day is marked
  skipped, you get an urgent email, and it retries automatically.
* After each successful run it writes a database backup to `data/backups/` (the last 14 are kept).

### Start automatically at login

```bash
.venv/bin/trader service install     # set up
.venv/bin/trader service status      # check
.venv/bin/trader service uninstall   # remove
```

On a Mac this adds two small launchd agents to `~/Library/LaunchAgents/`. On Linux it adds
systemd user units. On Windows it adds Task Scheduler tasks (untested). They do two things:

1. **start the trader when you log in**, and restart it if it ever **crashes**. A normal
   `./stop.sh` still stops it; it is not restarted until your next login or `./start.sh`;
2. **every hour, run a heartbeat check.** If the trader has not been heard from for more than
   **26 hours**, you get one urgent email per day until it is back.

Linux only: to run even when you are not logged in, also run `loginctl enable-linger $USER` once.
The files it writes are shown, with example paths, in [`deploy/`](deploy/).

---

## 4. The dashboard

Open **http://127.0.0.1:8765** while the trader is running. It refreshes itself every 60
seconds and works on a phone-sized screen.

* **Status bar** (top): a health dot (✓ healthy · ! needs attention · ✕ problem, which also
  shows if the last successful run is more than 26 hours old), when the last run finished, a
  countdown to the next one, whether the price data is fresh, and the risk manager's state in
  plain words (for example "Circuit breaker on: new entries blocked").
* **Headline numbers** for the selected account (section 5 explains each one).
* **Equity vs Buy & Hold** over 7 days, 30 days, 90 days or all time, with the drawdown chart
  underneath. The **S1 / S2 / S3 / Portfolio** buttons switch account.
* **Open positions**, with coins within 3% of their stop highlighted.
* **Recent closed trades.** Tap one to see its full decision trail: the signal, the risk
  check, the (optional) AI review, and the fill, for both entry and exit.
* **Risk events** in plain English (newest first), the **strategy scoreboard**, and whether
  **alerts** are being delivered.
* **AI analyst** (only once it has been switched on, section 12): the AI-filtered account against
  its rules-only twin, the verdicts, what the AI has cost, and whether the difference means anything yet.
* **Research** (top right) lists every backtest and research report.

Gains are **blue with ▲ and "+"**; losses are **vermillion with ▼ and "−"**. These colours
are safe for colour-blind readers, and colour is never the only signal. The theme is dark by
default. The button switches to light, and your system's light/dark setting is respected.

The dashboard is **read-only**: nothing on it can place, change or cancel trades or settings.
It loads nothing from the internet.

---

## 5. Every number, in plain language

### On the dashboard

| Number | What it means |
|---|---|
| **Equity** | What the paper account is worth at the last daily close: cash plus open positions valued at that close. |
| **Today's P&L** | Profit or loss (in $ and %) of the most recent day compared with the day before. |
| **Total return** | How much the account has grown or shrunk since it started with $10,000. |
| **Drawdown from peak** | How far the account is below its highest-ever value right now. 0% means it is at a new high. |
| **Open positions** | How many coins are held now (at most 4 across all strategies). |
| **Open risk** | If every open position fell to its stop-loss right now, the share of equity that would be lost. No new trade may take it above 4%. |
| **Unrealized P&L** | The profit or loss on an open position if it were sold at the last close. |
| **Stop** | The price at which the position will be sold to limit the loss. It only ever moves up. |
| **Distance to stop** | How far the price can fall before the stop is hit. |
| **Buy & Hold** | The comparison line: the same starting money split equally across the coins and simply held. |

**Scoreboard** columns: *Return* (total return), *Max DD* (the worst fall from a peak so far),
*Sharpe* (return per unit of day-to-day ups and downs, explained below), *Win rate* and
*Trades* (closed trades).

### In backtest and research reports

| Metric | What it means | How to read it |
|---|---|---|
| **Total return** | Growth from the first day to the last. | Always compare with Buy & Hold over the same days. |
| **CAGR** | The steady yearly growth rate that would give the same total return. | Easier to compare periods of different lengths. |
| **Max drawdown** | The worst peak-to-bottom fall of the account. | The pain you would have had to sit through. Expect worse in the future. |
| **Longest drawdown (days)** | The longest time the account spent below a previous high. | How long you might wait to "get back to even". |
| **Sharpe (365d)** | Average daily return divided by how much returns swing, scaled to a year. | Above 1 is good, above 2 is rare. **Above 3 is a red flag** (probably a bug). |
| **Sortino (365d)** | Like Sharpe, but only counts the *down* swings. | Higher is better; usually higher than Sharpe. |
| **Calmar** | CAGR divided by max drawdown. | Return earned per unit of worst-case pain. |
| **Trades** | Number of completed round trips (buy then sell). | **Fewer than 30 is a red flag**: too few to judge. |
| **Win rate** | Share of trades that made money. | Trend-following often wins only 30–45% of the time and still profits, because winners are much bigger than losers. |
| **Profit factor** | Total won ÷ total lost. | Above 1 = profitable. **Above 3 is a red flag.** |
| **R / Average R** | *1R* is what a trade risked at entry (distance to the initial stop × size). A +2R trade made twice what it risked. | Average R above 0 means the rules have an edge after costs. |
| **Expectancy per trade** | Average profit or loss per trade, in dollars. | Must be positive after fees and slippage. |
| **Exposure** | Share of days with at least one position open. | Low exposure = mostly in cash. |
| **Fees paid / Slippage cost** | Simulated trading fees, and the cost of filling at a worse price than quoted. | Costs are always included. The `--zero-costs` run exists only as a sanity check. |
| **End equity** | The final account value. | |

### Research-only terms

| Term | Meaning |
|---|---|
| **Walk-forward / out-of-sample** | Settings are chosen on 2 years of history, then scored **untouched** on the next 6 months, rolling forward through time. Only these out-of-sample results are headline numbers. |
| **In-sample** | Results on the same data the settings were chosen on. Always look better than reality. **Red flag** if out-of-sample Sharpe is less than half of in-sample. |
| **Fixed defaults** | The standard settings run over the same span. If they do as well as the walk-forward, re-optimising only fits noise: keep the defaults. |
| **Parameter sensitivity** | A heat-map of results for nearby settings. A good strategy is a broad "plateau", not a single lucky spike. |
| **Monte Carlo (worst 5%)** | The trades are reshuffled 1,000 times to see how bad the drawdown could have been with a different order of luck. Plan for the worst-5% figure, not the backtest's own drawdown. |
| **Regimes** | Results split into bull, bear and sideways markets (by BTC's trend), to show where the strategy makes and loses money. |
| **SYNTHETIC** | Prices were generated for offline testing, not downloaded. Such results mean nothing about real markets. |

---

## 6. Email alerts

Put your email settings in `.env` (never in `config.yaml`). For Gmail, create an
[App Password](https://support.google.com/accounts/answer/185833), then:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-16-letter-app-password
ALERT_EMAIL_FROM=you@gmail.com
ALERT_EMAIL_TO=you@gmail.com
```

Port 465 (SSL) also works. Then test it: `.venv/bin/trader doctor --send-test-alert`.

You get:

* a short **daily summary** (equity, day P&L, open positions) for each account;
* an **urgent** email right away when the circuit breaker turns on, the daily loss cap is hit,
  a daily run fails or is skipped, price data is bad or missing, or the trader has been
  silent for more than 26 hours.

Each alert is sent once, even if a day is re-run. If your mail server is unreachable, a failed
alert is retried every 15 minutes for up to 24 hours. The dashboard shows whether alerts are
getting through.

---

## 7. Viewing it from your phone (optional)

By default the dashboard only listens on this computer (`server.host: 127.0.0.1`). That is the
safest setting, and it was the choice made at setup. If you later want to look from a phone:

* **SSH tunnel** (nothing changes on the Mac's side):
  `ssh -N -L 8765:127.0.0.1:8765 you@your-mac.local`, then open http://127.0.0.1:8765
  on the device doing the tunnelling. Needs *Remote Login* enabled in macOS settings.
* **Tailscale** (a private network between your own devices): install it on the Mac and the
  phone, set `server.host` to the Mac's Tailscale address (`100.x.y.z`) in `config.yaml`,
  then `./restart.sh`. Other devices must sign in with the **dashboard login token** from
  install.

Never use `0.0.0.0` or open the port to the internet: the config refuses `0.0.0.0`. Only
the SHA-256 hash of the login token is stored, and sign-in is rate-limited. **Lost the
token?** Delete `data/dashboard_token.sha256` and run `.venv/bin/trader init` to print a new one.

---

## 8. Backtests and research

```bash
.venv/bin/trader backtest -s S1 -a BTC              # one strategy on one coin -> HTML report
.venv/bin/trader research                           # all strategies, all coins, walk-forward (~2 min)
.venv/bin/trader verify lookahead -s S1 -a BTC      # proof that no decision used future data
```

Reports are saved in `reports/` and listed on the dashboard's **Research** page. On a Mac you
can also `open reports/<name>.html`. Every report starts with **red flags** (fewer than 30
trades, profit factor above 3, Sharpe above 3, out-of-sample much worse than in-sample). Each
one needs investigating before you believe anything else on the page.

**How to read the research report:** its headline numbers are all out-of-sample. If the fixed
defaults do as well as the walk-forward, keep the defaults. Never tweak settings to make a
backtest look better: that is exactly how strategies end up looking great on paper and
losing money live.

**The strategies:**
* **S1: Donchian breakout.** Buy when the price closes above the highest price of the previous
  20 days; sell when it closes below the lowest price of the previous 10 days, or below a
  "chandelier" trailing stop (3 × ATR under the highest price since buying).
* **S2: Supertrend.** Buy when the Supertrend indicator flips up while the price is above its
  200-day average; exit when it flips down.
* **S3: Momentum.** Hold coins whose 30- and 90-day returns are both positive and that are above
  their 200-day average. Size each so the coins share a 40%-a-year volatility budget; rebalance
  only when the target moves by more than 20%.

(*ATR*, average true range, is the typical daily price movement. *Volatility* is how much the
price swings, per year.)

---

## 9. How the simulation trades

* A decision made at the **close of day t** is filled at the **open of day t+1**, plus slippage
  (0.05% for BTC/ETH, 0.15% for others) and a **0.10% fee** each way.
* Stops rest as orders. If the price **opens below** a stop (a gap down), the fill is at that
  worse opening price, as it would be in real life.
* The **risk manager** checks every new trade:
  * risk at most **1% of equity** per trade;
  * no position above **25%** of equity;
  * total open risk of at most **4%**;
  * at most **4 positions** at once;
  * **no new buys** in any of these cases:
    * while BTC is below its 200-day average (a bear market);
    * the day after a loss of 3% or more;
    * while the account is **15% or more below its peak** (the *circuit breaker*, released
      back within 10%);
    * for 5 days after a strategy has 4 losing trades in a row.

  Exits are never blocked.
* Circuit-breaker note: if the breaker trips while everything is already in cash, it re-arms
  after 30 days, because flat equity can never recover on its own. So the all-time worst
  drawdown can be deeper than 15%.

---

## 10. Market data

* Daily candles (UTC) from **Bitstamp** public endpoints (available in the US, USD pairs).
  **Coinbase Exchange** is used only for a coin Bitstamp does not list, so each coin's history
  always comes from one exchange. No account or API key is involved.
* Every download is validated: no duplicate days, consistent high/low/open/close, no negative
  volume. **Today's unfinished candle is never used.** A bad batch never overwrites good data.
* Short gaps (up to 3 days) are filled flat and flagged, and never traded on. A longer recent
  gap makes that coin **not tradable** until the exchange fills it in
  (`.venv/bin/trader data fetch --full-refresh`).
* `.venv/bin/trader data status` shows what is stored for each coin and how fresh it is.
* `data.source: synthetic` generates clearly labelled fake prices for offline testing only.

---

## 11. Settings

* **`config.yaml`**: every setting, with comments. Values are checked on start-up and must be
  in safe ranges (for example risk per trade 0.1%–2%). Unknown keys and `0.0.0.0` are
  rejected. Check your edits with `.venv/bin/trader config check`, then `./restart.sh`.
* **`.env`**: secrets only (email password, optional AI key). Readable by you only, never
  committed, never logged.

The defaults are deliberately conservative. Changing them to improve a backtest is the most
common way to fool yourself (section 8).

---

## 12. The optional AI analyst

It is **off by default**. When it is on, an AI model reviews each **new entry that the risk engine
has already approved**, and may only **approve** it, **make it smaller**, or **veto** it.

It cannot create a trade, make one bigger, move a stop or touch an exit. The software enforces this,
not the prompt. If anything goes wrong (no key, timeout, API error, the model declining, or an
answer that is malformed or out of range), the rule-based decision stands, and the dashboard says so.

**It is measured, not trusted.** Switching it on creates two new accounts on the same day:
* **AI**: the AI-filtered portfolio.
* **Shadow**: the same strategies, the same money and the same signals, but rules only.

The dashboard's **AI analyst** card and `.venv/bin/trader llm report` compare the two, including
what the AI has cost, and say honestly whether the difference means anything yet. For a daily
system, expect "no evidence either way" for a long time: [docs/AI_SELF_IMPROVEMENT.md](docs/AI_SELF_IMPROVEMENT.md)
explains why, and what else AI could do here.

**To switch it on:**
1. Create an API key in the Claude Console (<https://console.anthropic.com>) and put
   `ANTHROPIC_API_KEY=...` in `.env`.
2. Run `.venv/bin/trader llm test`. It sends one sample review (a made-up BTC entry) and shows the
   answer, how long it took and what it cost. Nothing is traded.
3. Set `llm.enabled: true` in `config.yaml`, then run `./restart.sh`.

**Cost:** a review happens only when an entry passes every risk check. That is at most one per
strategy per coin per day, and usually far fewer. `trader llm test` shows the real cost of one
review, and the dashboard keeps the running total.

**Settings** (`llm:` in `config.yaml`, each explained there):
* the model (pinned, and recorded with every review);
* how hard it thinks (`effort`);
* the timeout;
* how old a catch-up day may be and still get a review (`max_age_days`; older days follow the
  rules);
* whether a declined request may be retried on the API's recommended fallback model.

Note that a `config.yaml` created before this version keeps the model it already names.

**What is sent:**
* recent daily prices;
* indicator values;
* your *paper* positions and risk state.

No personal data and no keys are sent. The exact request and the answer for every review are
stored locally in `data/trader.db`.

**Never in backtests:** a model may remember what prices did after any day it was trained on, so a
backtest with it in the loop would be meaningless. It is only judged going forward.

---

## 13. Safety and security

* **Simulation only.** It uses public market-data endpoints only. The data layer refuses to
  hold any credentials, and every entry point checks `mode == "paper"` and refuses to run
  otherwise.
* **Local only.** It listens on 127.0.0.1. Anything that is not this computer must sign in with
  the login token (only its hash is stored), and sign-in is rate-limited. The dashboard is
  read-only and has a strict Content-Security-Policy. It loads nothing from other sites.
* **Secrets** live only in `.env` (owner-only permissions, git-ignored) and are scrubbed from
  logs and error messages. `trader stop` uses a private token in `run/`.
* **The optional AI analyst** can only keep, shrink or veto an approved entry, and this is enforced
  in code. Its key lives only in `.env`, and any failure falls back to the rules (section 12).
* **Dependencies** are pinned. `.venv/bin/pip install -r requirements-dev.txt`, then
  `.venv/bin/trader doctor --full` checks them for known vulnerabilities (pip-audit).

---

## 14. Uninstalling

```bash
./uninstall.sh        # or double-click uninstall.command  (Windows: uninstall.bat, untested)
```

It removes the auto-start service, stops the trader, **asks before deleting `data/`** (your
paper-trading history and its backups), and removes `.venv/`. It leaves `config.yaml`, `.env`,
`logs/` and `reports/` in place. Delete the folder yourself if you want everything gone. Use
`--keep-data` or `--delete-data` to skip the question.

---

## 15. Troubleshooting

| Problem | Fix |
|---|---|
| `Lacking write permission` / files owned by root (from an earlier `sudo`) | `sudo chown -R "$(id -un)" ~/CryptoTrade`, then `./install.sh` again (the installer detects this and prints the exact command) |
| macOS: *"cannot be opened because it is from an unidentified developer"* on a `.command` file | Right-click → Open once, or `xattr -d com.apple.quarantine *.command` |
| `permission denied: ./install.sh` | `chmod +x *.sh *.command` |
| `trader: command not found` | Use `.venv/bin/trader …` from the project folder |
| *Port 8765 is already in use* | Another program uses it: set `server.port` in `config.yaml` |
| Status shows **!** or **✕**, or a day was skipped | `.venv/bin/trader doctor` explains what is wrong; `./logs.sh` shows details |
| No emails | `.venv/bin/trader doctor --send-test-alert` (Gmail needs an App Password) |
| Anything else | `.venv/bin/trader doctor`, then `./logs.sh -n 200` |

---

## 16. For developers

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                     # the full test suite (~4 min)
.venv/bin/trader doctor --full                 # + dependency vulnerability audit
.venv/bin/trader verify lookahead -s S1 -s S2 -s S3 -a BTC -a ETH -a SOL -a XRP
.venv/bin/trader run-once                      # process pending days in the foreground (refuses if running)
.venv/bin/trader db status | db backup         # schema/row counts; online backup
```

Layout:
* `trader/`: the app. The engine, broker, risk and strategies are shared by backtests and
  live paper trading.
* `tests/`: the test suite.
* `deploy/`: example service files and the Dockerfile.
* `packaging/`: the optional binary build.

Setting `TRADER_FAKE_NOW=2021-03-02T00:12:00+00:00` pins the clock, for testing only.

### Optional self-contained build (PyInstaller)

```bash
./packaging/build_pyinstaller.sh      # -> dist/trader/  (about 290 MB)
dist/trader/trader init && dist/trader/trader start
```

It works on Linux: the full start → daily run → dashboard → stop cycle was tested. It is
**optional and not recommended on a Mac**:

* it must be built on a Mac (separately for Apple Silicon and Intel);
* the result is unsigned, so macOS Gatekeeper warns about it.

`./install.sh` is the supported route.

### Docker (optional, Linux hosts)

See [`deploy/Dockerfile`](deploy/Dockerfile). It is **untested** (no Docker was available). It must use
`--network host`, because the app will not listen on 0.0.0.0.

---

## 17. What has and has not been verified

Verified here, on Linux, by the test suite and end-to-end runs:
* installing from scratch;
* the start/stop/status/restart/logs scripts;
* catch-up and crash recovery;
* the dashboard (in a real browser);
* the backtest look-ahead proofs;
* the alert queue with a local mail server;
* the systemd service files (`systemd-analyze verify`);
* uninstall;
* the PyInstaller build;
* the AI analyst, against a local stand-in for the Anthropic API. This goes through the real SDK over
  HTTP and covers answers, vetoes, reductions, timeouts, errors, refusals and bad output.

**Not verified** (it was not possible where this was built):
* a real Mac (launchd agents and `.command` files);
* Windows (all `.bat`/`.ps1` files and Task Scheduler);
* Docker;
* downloading real Bitstamp/Coinbase data (the network blocked exchanges, so every test used
  clearly labelled **synthetic** prices);
* sending through a real email provider;
* a real AI review (there is no API key here). Run `.venv/bin/trader llm test` once on your Mac.

The first `./install.sh` on your Mac is the real test of these. Its `doctor` checklist
reports anything that does not work.

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, config, CLI, installer, doctor, database, market data, indicators | ✅ |
| 2 | Simulated broker, risk manager, S1, backtest, look-ahead proof | ✅ |
| 3 | S2, S3, walk-forward research, sensitivity, Monte Carlo, regimes | ✅ |
| 4 | Always-on runtime, catch-up, crash recovery, start/stop scripts | ✅ |
| 5 | Dashboard and email alerts | ✅ |
| 6 | Hardening, auto-start service, uninstall, packaging, this guide | ✅ |
| 7 | Optional AI analyst with a rules-only shadow twin, plus the [self-improvement research note](docs/AI_SELF_IMPROVEMENT.md) | ✅ |
