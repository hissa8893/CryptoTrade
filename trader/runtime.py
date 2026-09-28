"""Daily paper-trading runtime (used by `trader serve` and `trader run-once`).

For every UTC day whose candle has closed and that has not been processed yet, IN ORDER:
  confirm the day is closed -> fetch + validate data -> run each paper account's engine
  for that day (fills at the open, stops, mark-to-market, signals, risk checks, orders for
  the next open) -> write everything -> snapshot equity -> alerts -> heartbeat.

Guarantees:
* One atomic transaction per day across all accounts: a crash or kill mid-day leaves no
  partial rows; the day is simply re-run from the saved state of the day before.
* Idempotent: a day marked ok is never processed again; the engine refuses to re-step a
  day; unique constraints reject duplicates.
* Catch-up: after downtime (Mac asleep/off) every missed day is processed in order.
* Never trades on stale data: if the data for a day is missing or invalid for the market
  regime asset or for any coin with an open position, the day is marked `skipped`, an
  urgent alert is queued, and it is retried later.
* Accounts: each enabled strategy is its own sub-account, plus one combined PORTFOLIO
  account; all use the SAME engine code as backtests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from trader import __version__
from trader.alerts import dispatch_pending, queue_alert
from trader.backtest import make_engine
from trader.config import AppConfig, Secrets, load_secrets
from trader.data import MarketData
from trader.db import Database, git_commit
from trader.journal_store import save_engine_state, write_journal, write_positions
from trader.llm import Analyst, Review, ReviewBook
from trader.paths import Paths
from trader.strategies import AVAILABLE
from trader.timeutil import last_closed_day, now_iso, now_utc

log = logging.getLogger(__name__)

RETRY_SECONDS = 15 * 60  # after a failed/skipped attempt, wait this long before the watchdog retries


# ------------------------------------------------------------------------------ cross-process lock
class FileLock:
    """Non-blocking exclusive lock on a file (so a daemon and `run-once` never process at once)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass(frozen=True)
class Account:
    key: str
    strategies: tuple[str, ...]
    mode: str = "paper"  # 'shadow' = the rules-only twin of the AI-filtered account
    ai: bool = False  # True = entries go past the AI analyst

    def __iter__(self):  # `for key, strategies in accounts()` keeps working
        return iter((self.key, list(self.strategies)))


class _Stopping(Exception):
    pass


@dataclass
class DayResult:
    bar_date: str
    status: str  # ok | skipped | failed
    error: str | None = None
    accounts: dict = field(default_factory=dict)  # run_key -> summary


def _test_pause_in_tx(paths: Paths) -> None:
    """TEST HOOK ONLY: pause inside the day's transaction so a test can kill the process
    mid-write and prove nothing partial is committed."""
    secs = os.environ.get("TRADER_TEST_PAUSE_IN_TX")
    if secs:
        (paths.run / "in_tx.marker").write_text(str(os.getpid()))
        time.sleep(float(secs))


class Runtime:
    def __init__(self, cfg: AppConfig, paths: Paths, *, db: Database | None = None,
                 market_data: MarketData | None = None, secrets: Secrets | None = None):
        self.cfg = cfg
        self.paths = paths
        self.db = db or Database(paths.db_file)
        self.db.migrate()
        self.md = market_data or MarketData(cfg, paths)
        self.secrets = secrets or load_secrets(paths)
        self._lock = threading.Lock()
        self._failed_at: dict[str, float] = {}
        self.busy = False
        self.stop_requested = threading.Event()  # set on shutdown: finish the current day, leave the rest

    # -- accounts ----------------------------------------------------------------------------
    def accounts(self) -> list[Account]:
        """S1/S2/S3 each alone, the combined PORTFOLIO, and - once the AI analyst has been switched
        on - an AI-filtered portfolio plus its rules-only SHADOW twin. The twin starts the same day
        with the same money and sees the same signals, so any difference between the two is the AI's
        doing. Once started, the pair keeps running (rules-only reviews while the AI is off), so the
        comparison never has gaps."""
        enabled = tuple(s for s in AVAILABLE if getattr(self.cfg.strategies, s.lower()).enabled)
        src = self.md.source
        accts = [Account(f"paper:{src}:{s}", (s,)) for s in enabled]
        if len(enabled) > 1:
            accts.append(Account(f"paper:{src}:PORTFOLIO", enabled))
        if enabled and (self.cfg.llm.enabled or self._ai_started(src)):
            accts.append(Account(f"paper:{src}:AI", enabled, "paper", ai=True))
            accts.append(Account(f"paper:{src}:AI_SHADOW", enabled, "shadow"))
        return accts

    def _ai_started(self, src: str) -> bool:
        with self.db.read() as c:
            return c.execute(text("SELECT 1 FROM runs WHERE run_key = :k"), {"k": f"paper:{src}:AI"}).first() is not None

    def _ensure_runs(self, c, first_day: str) -> dict[str, tuple[int, str]]:
        out = {}
        for acct in self.accounts():
            key, strategies = acct.key, list(acct.strategies)
            row = c.execute(text("SELECT id, start FROM runs WHERE run_key = :k"), {"k": key}).fetchone()
            if row is None:
                params = {"strategies": {s: getattr(self.cfg.strategies, s.lower()).model_dump() for s in strategies},
                          "risk": self.cfg.risk.model_dump(), "costs": self.cfg.costs.model_dump()}
                c.execute(text(
                    "INSERT INTO runs (run_key, mode, strategy, params_json, start, created_at, git_commit, app_version, "
                    "starting_equity, data_source) VALUES (:k, :m, :s, :p, :st, :c, :g, :v, :eq, :src)"),
                    {"k": key, "m": acct.mode, "s": "PORTFOLIO" if len(strategies) > 1 else strategies[0],
                     "p": json.dumps({**params, "ai": acct.ai}),
                     "st": first_day, "c": now_iso(), "g": git_commit(self.paths.root), "v": __version__,
                     "eq": self.cfg.accounts.starting_equity, "src": self.md.source})
                row = c.execute(text("SELECT id, start FROM runs WHERE run_key = :k"), {"k": key}).fetchone()
                log.info("created paper account %s starting %s", key, first_day)
            out[key] = (int(row[0]), row[1])
        return out

    def _states(self) -> dict[str, dict]:
        keys = [k for k, _ in self.accounts()]
        with self.db.read() as c:
            rows = c.execute(text(
                "SELECT r.run_key, s.state_json FROM runs r JOIN run_state s ON s.run_id = r.id")).fetchall()
        return {k: json.loads(v) for k, v in rows if k in keys}

    # -- schedule ----------------------------------------------------------------------------
    def last_ok(self) -> tuple[str, str] | None:
        """(bar_date, finished_at) of the most recent successfully processed day."""
        with self.db.read() as c:
            row = c.execute(text(
                "SELECT bar_date, finished_at FROM job_runs WHERE status = 'ok' ORDER BY bar_date DESC LIMIT 1")).fetchone()
        return (row[0], row[1]) if row else None

    def pending_days(self, now=None) -> list[str]:
        """Closed days not yet processed. The very first run starts with the latest closed day."""
        last_closed = last_closed_day(now)
        ok = self.last_ok()
        if ok is None:
            return [last_closed.isoformat()]
        d, out = date.fromisoformat(ok[0]) + timedelta(days=1), []
        while d <= last_closed:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    def heartbeat(self) -> None:
        self.db.set_kv("heartbeat", now_iso())

    def startup_check(self) -> float | None:
        """Called when the daemon starts: if the last heartbeat is older than the stale limit, the
        trader was down (computer off, crash) -> urgent alert + event. Returns the downtime in hours."""
        hb = self.db.get_kv("heartbeat")
        if not hb:
            return None
        hours = (now_utc() - datetime.fromisoformat(hb)).total_seconds() / 3600
        if hours <= self.cfg.scheduler.heartbeat_stale_hours:
            return hours
        msg = (f"No heartbeat for {hours:.0f} h (last seen {hb[:16].replace('T', ' ')} UTC): the trader was not "
               "running. It has restarted and is catching up every missed day in order.")
        with self.db.tx() as c:
            c.execute(text(
                "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, message, details_json, dedupe_key) "
                "VALUES (NULL, :ts, NULL, 'heartbeat_missing', 'urgent', :m, :det, :k) ON CONFLICT (dedupe_key) DO NOTHING"),
                {"ts": now_iso(), "m": msg, "det": json.dumps({"last_heartbeat": hb, "hours": hours}), "k": f"downtime:{hb}"})
            queue_alert(c, "urgent", f"[trader] no heartbeat for {hours:.0f} h", msg, f"downtime:{hb}")
        log.warning("trader was down for %.0f h (last heartbeat %s)", hours, hb)
        return hours

    def check_heartbeat(self, running: bool) -> tuple[bool, str]:
        """For an external scheduler (launchd/systemd/Task Scheduler), independent of the daemon.
        Urgent alert (at most once a day) only when there has been NO heartbeat for longer than
        scheduler.heartbeat_stale_hours (26 h) - a deliberate short stop is not an emergency.
        Returns (healthy, message); healthy = running with a fresh heartbeat."""
        hb = self.db.get_kv("heartbeat")
        hours = (now_utc() - datetime.fromisoformat(hb)).total_seconds() / 3600 if hb else None
        limit = self.cfg.scheduler.heartbeat_stale_hours
        if running and hours is not None and hours <= limit:
            return True, f"ok: running, heartbeat {hours * 60:.0f} min ago"
        if hours is None:
            return False, "never started (no heartbeat yet); no alert"
        if hours <= limit:
            return False, f"not running, but last heartbeat only {hours:.1f} h ago (alert after {limit:.0f} h)"
        why = f"no heartbeat for {hours:.0f} h" + ("" if running else " (trader not running)")
        with self.db.tx() as c:
            queue_alert(c, "urgent", f"[trader] {why}",
                        f"The paper trader has had {why}; last seen {hb[:16].replace('T', ' ')} UTC "
                        f"(checked {now_iso()} UTC). Start it with ./start.sh.",
                        f"watch:{now_utc().date().isoformat()}")
        dispatch_pending(self.cfg, self.secrets, self.db)
        return False, why + " — urgent alert sent"

    def retry_alerts(self) -> None:
        """Scheduler job (every 15 min): deliver alerts still pending, or failed in the last 24 h
        (e.g. the mail server was briefly down at 00:10)."""
        if self.busy:
            return  # the running catch-up dispatches when it finishes
        try:
            dispatch_pending(self.cfg, self.secrets, self.db)
        except Exception:
            log.exception("alert retry failed")

    # -- entry points -------------------------------------------------------------------------
    def catch_up_if_due(self) -> list[DayResult]:
        """Watchdog (every few minutes): run if a closed day is waiting, with a back-off after
        failures. Also what rescues a daily run missed while the computer was asleep."""
        days = self.pending_days()
        if not days:
            return []
        failed = self._failed_at.get(days[0])
        if failed is not None and time.monotonic() - failed < RETRY_SECONDS:
            return []
        return self.catch_up("watchdog")

    def catch_up(self, reason: str = "manual") -> list[DayResult]:
        if not self._lock.acquire(blocking=False):
            log.info("catch-up already in progress in this process; skipping (%s)", reason)
            return []
        flock = FileLock(self.paths.run / "catchup.lock")
        if not flock.acquire():
            self._lock.release()
            log.info("another process is processing days; skipping (%s)", reason)
            return []
        self.busy = True
        try:
            self.recover()
            self.heartbeat()
            days = self.pending_days()
            if not days:
                return []
            log.info("catch-up (%s): %d day(s) to process: %s .. %s", reason, len(days), days[0], days[-1])
            fetch_error, updates = None, []
            try:
                updates = self.md.update()
            except Exception as exc:  # exchange down etc.: cached data is still checked per day below
                fetch_error = f"{type(exc).__name__}: {exc}"
                log.error("data update failed: %s", fetch_error)
            frames = self.md.load_all()
            results = []
            for d in days:
                if self.stop_requested.is_set():
                    log.info("stop requested: day %s and later left for the next start", d)
                    break
                r = self.run_day(d, frames, updates, fetch_error)
                results.append(r)
                if r.status != "ok":
                    self._failed_at[d] = time.monotonic()
                    break
                self._failed_at.pop(d, None)
            ok_days = [r for r in results if r.status == "ok"]
            if ok_days:
                self._queue_daily_summary(ok_days[-1])
                try:
                    dest = self.db.backup(self.paths.backups, keep=14)
                    log.info("database backed up to %s", dest)
                except Exception:
                    log.exception("database backup failed")
            try:
                dispatch_pending(self.cfg, self.secrets, self.db)
            except Exception:
                log.exception("alert dispatch failed")
            self.heartbeat()
            return results
        finally:
            self.busy = False
            flock.release()
            self._lock.release()

    # -- AI analyst -------------------------------------------------------------------------------
    def _engine(self, acct: Account, frames: dict, tradable: list[str], d: str, start: str, state: dict | None,
                advisor=None):
        engine_frames = {s: df.loc[:d] for s, df in frames.items() if s in tradable}
        eng, *_ = make_engine(self.cfg, engine_frames, list(acct.strategies), symbols=tradable, start=start,
                              advisor=advisor)
        if state:
            eng.load_state(state)
        return eng

    def _ai_policy(self, d: str) -> str | None:
        """Why the rules decide alone today (no AI call), or None when the AI is asked."""
        if not self.cfg.llm.enabled:
            return "disabled"
        if (last_closed_day() - date.fromisoformat(d)).days > self.cfg.llm.max_age_days:
            return "too_old"  # a long catch-up is decided by the rules (cost, and the model may know later prices)
        return None

    def _review_book(self, acct: Account, d: str, stored: dict[str, Review], collect: bool = False) -> ReviewBook:
        return ReviewBook(acct.key, stored, model=self.cfg.llm.model, max_bars=self.cfg.llm.max_bars,
                          synthetic=self.md.source == "synthetic", collect=collect, policy=self._ai_policy(d))

    @staticmethod
    def _stored_reviews(c, key: str, d: str) -> dict[str, Review]:
        rows = c.execute(text(
            "SELECT prompt_hash, decision, size_multiplier, confidence, reasons_json, status, fallback_reason, model, "
            "served_model, prompt_version, cost_usd, latency_ms, input_tokens, output_tokens FROM ai_reviews "
            "WHERE run_key = :k AND bar_date = :d"), {"k": key, "d": d}).fetchall()
        return {r[0]: Review(decision=r[1], multiplier=r[2], confidence=r[3], reasons=json.loads(r[4]), status=r[5],
                             fallback_reason=r[6], model=r[7], served_model=r[8], prompt_hash=r[0],
                             prompt_version=r[9], cost_usd=r[10], latency_ms=r[11], input_tokens=r[12],
                             output_tokens=r[13]) for r in rows}

    def prepare_ai_reviews(self, d: str, frames: dict, tradable: list[str]) -> int:
        """Before day d's transaction: run each AI account's engine for d WITHOUT saving anything, note
        every entry the risk engine approves, ask the analyst about each one (once - verdicts are
        stored), and repeat until nothing new comes up (a veto can free room for another signal).
        Returns the number of new reviews fetched."""
        accts = [a for a in self.accounts() if a.ai]
        if not accts or self._ai_policy(d):
            return 0
        analyst = Analyst(self.cfg.llm, self.secrets.anthropic_api_key.get_secret_value()
                          if self.secrets.anthropic_api_key else None)
        fetched = 0
        for acct in accts:
            with self.db.read() as c:
                row = c.execute(text("SELECT r.start, s.state_json FROM runs r LEFT JOIN run_state s ON s.run_id = r.id "
                                     "WHERE r.run_key = :k"), {"k": acct.key}).fetchone()
            start, state = (row[0], json.loads(row[1]) if row[1] else None) if row else (d, None)
            if state and state["last_date"] and state["last_date"] >= d:
                continue
            for _ in range(100):  # one new review per round; a day has at most (strategies x coins) entries
                with self.db.read() as c:
                    book = self._review_book(acct, d, self._stored_reviews(c, acct.key, d), collect=True)
                self._engine(acct, frames, tradable, d, start, state, book).run(until=d)
                if not book.missing:
                    break
                for ph, req in book.missing.items():
                    if self.stop_requested.is_set():
                        raise _Stopping()
                    rv = analyst.review(req.payload)
                    if rv.prompt_hash != ph:
                        raise RuntimeError("AI review prompt hash mismatch (payload not deterministic)")
                    self._store_review(acct.key, req, rv)
                    fetched += 1
                    log.info("AI review %s %s %s: %s x%.2f (%s)", d, req.strategy, req.symbol, rv.decision,
                             rv.multiplier, rv.status if rv.status == "ok" else rv.fallback_reason)
            else:
                log.error("AI reviews for %s did not settle after 100 rounds; unreviewed entries follow the rules", d)
        return fetched

    def _store_review(self, key: str, req, rv: Review) -> None:
        with self.db.tx() as c:
            c.execute(text(
                "INSERT INTO ai_reviews (run_key, bar_date, strategy, symbol, prompt_hash, prompt_version, model, "
                "served_model, status, fallback_reason, decision, size_multiplier, confidence, reasons_json, "
                "request_json, response_text, error, input_tokens, output_tokens, latency_ms, cost_usd, created_at) "
                "VALUES (:k, :d, :st, :sym, :ph, :pv, :m, :sm, :status, :fr, :dec, :mult, :conf, :reasons, :req, "
                ":resp, :err, :tin, :tout, :lat, :cost, :c) ON CONFLICT (run_key, prompt_hash) DO NOTHING"),
                {"k": key, "d": req.date, "st": req.strategy, "sym": req.symbol, "ph": rv.prompt_hash,
                 "pv": rv.prompt_version, "m": rv.model, "sm": rv.served_model, "status": rv.status,
                 "fr": rv.fallback_reason, "dec": rv.decision, "mult": rv.multiplier, "conf": rv.confidence,
                 "reasons": json.dumps(rv.reasons), "req": json.dumps(req.payload, sort_keys=True),
                 "resp": rv.response_text, "err": rv.error, "tin": rv.input_tokens, "tout": rv.output_tokens,
                 "lat": rv.latency_ms, "cost": rv.cost_usd, "c": now_iso()})

    def _defer(self, d: str, why: str) -> DayResult:
        """Stopped part-way through preparing day d: nothing was traded; it runs again on the next start."""
        with self.db.tx() as c:
            c.execute(text("UPDATE job_runs SET status = 'failed', finished_at = :t, error = :e WHERE bar_date = :d"),
                      {"t": now_iso(), "e": f"deferred: {why}; will be re-processed", "d": d})
        log.info("day %s deferred: %s", d, why)
        return DayResult(d, "deferred", why)

    # -- crash recovery -------------------------------------------------------------------------
    def recover(self) -> dict:
        """Run before anything else: find days interrupted mid-run and make sure the positions
        table matches the saved engine state (the source of truth)."""
        summary = {"interrupted": [], "reconciled": []}
        with self.db.tx() as c:
            for (d, attempts) in c.execute(text("SELECT bar_date, attempts FROM job_runs WHERE status = 'running'")).fetchall():
                c.execute(text("UPDATE job_runs SET status = 'failed', finished_at = :t, "
                               "error = 'interrupted (process stopped mid-run); will be re-processed' WHERE bar_date = :d"),
                          {"t": now_iso(), "d": d})
                c.execute(text(
                    "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, message, details_json, dedupe_key) "
                    "VALUES (NULL, :ts, :d, 'run_interrupted', 'warn', :m, '{}', :k) ON CONFLICT (dedupe_key) DO NOTHING"),
                    {"ts": now_iso(), "d": d, "k": f"job:{d}:interrupted:{attempts}",
                     "m": f"The run for {d} was interrupted (crash, kill or power loss). Nothing from it was saved; "
                          "it is being re-processed from the previous day's saved state."})
                summary["interrupted"].append(d)
                log.warning("day %s was interrupted mid-run; will re-process", d)
            rows = c.execute(text(
                "SELECT r.id, r.run_key, s.state_json FROM runs r JOIN run_state s ON s.run_id = r.id "
                "WHERE r.mode IN ('paper', 'shadow')")).fetchall()
            for run_id, key, sj in rows:
                state = json.loads(sj)
                want = sorted((p["strategy"], p["symbol"], round(p["qty"], 12), round(p["stop"], 10))
                              for p in state["broker"]["positions"])
                have = sorted((s, sym, round(q, 12), round(st, 10)) for s, sym, q, st in c.execute(text(
                    "SELECT strategy, symbol, qty, current_stop FROM positions WHERE run_id = :r"), {"r": run_id}))
                if want != have:
                    write_positions(c, run_id, state["broker"]["positions"], state.get("last_close", {}), now_iso())
                    c.execute(text(
                        "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, message, details_json, dedupe_key) "
                        "VALUES (:r, :ts, :d, 'positions_reconciled', 'warn', :m, '{}', :k) "
                        "ON CONFLICT (dedupe_key) DO NOTHING"),
                        {"r": run_id, "ts": now_iso(), "d": state.get("last_date"),
                         "k": f"{key}:reconcile:{now_iso()}",
                         "m": "Open-positions table did not match the saved engine state; rebuilt it from the saved state."})
                    summary["reconciled"].append(key)
                    log.warning("reconciled positions table for %s", key)
        return summary

    # -- one day ------------------------------------------------------------------------------
    def _data_problems(self, d: str, frames: dict, updates, fetch_error: str | None) -> dict[str, str]:
        problems = {}
        for sym in self.md.symbols():
            df = frames.get(sym)
            if df is None or df.empty:
                problems[sym] = "no validated data (see `trader data status`)"
            elif df.index[-1].date().isoformat() < d:
                problems[sym] = f"stale: last candle {df.index[-1].date()}, need {d}"
        for u in updates or []:
            if not u.report.ok and u.symbol not in problems:
                problems[u.symbol] = "; ".join(u.report.errors)
        if fetch_error:
            for sym in list(problems):
                problems[sym] += f" (fetch failed: {fetch_error})"
        return problems

    def run_day(self, d: str, frames: dict, updates=None, fetch_error: str | None = None) -> DayResult:
        started = now_iso()
        with self.db.tx() as c:
            c.execute(text(
                "INSERT INTO job_runs (bar_date, started_at, status, heartbeat_at, attempts) VALUES (:d, :t, 'running', :t, 1) "
                "ON CONFLICT (bar_date) DO UPDATE SET started_at = :t, status = 'running', heartbeat_at = :t, "
                "finished_at = NULL, error = NULL, attempts = job_runs.attempts + 1"), {"d": d, "t": started})

        regime_sym = next((s for s in self.md.symbols() if s.split("/")[0] == self.cfg.risk.regime_asset), None)
        problems = self._data_problems(d, frames, updates, fetch_error)
        held = {p["symbol"] for st in self._states().values() for p in st["broker"]["positions"]}
        held |= {o["symbol"] for st in self._states().values() for o in st["broker"]["pending"]}
        blocking = {s: why for s, why in problems.items() if s == regime_sym or s in held}
        if not self.md.symbols():
            blocking["data"] = "no price data has been downloaded yet" + (
                f" (download failed: {fetch_error})" if fetch_error else "; run: .venv/bin/trader data fetch")
        elif regime_sym is None:
            blocking["regime"] = (f"no {self.cfg.risk.regime_asset} price data (needed for the market-regime filter)"
                                  + (f"; download failed: {fetch_error}" if fetch_error else ""))
        if blocking:
            why = "; ".join(f"{s}: {w}" for s, w in blocking.items())
            return self._give_up(d, "skipped", f"data not ready/valid for {d} — {why}", "data_not_ready")

        tradable = [s for s in self.md.symbols() if s not in problems]
        try:
            self.prepare_ai_reviews(d, frames, tradable)  # network calls happen HERE, never inside the tx
        except _Stopping:
            return self._defer(d, "trader stopping while fetching AI reviews")
        try:
            accounts_out = {}
            with self.db.tx() as c:
                runs = self._ensure_runs(c, d)
                for sym, why in problems.items():  # excluded today, but nothing is held in them
                    c.execute(text(
                        "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, symbol, message, details_json, "
                        "dedupe_key) VALUES (NULL, :ts, :d, 'symbol_excluded', 'warn', :sym, :m, '{}', :k) "
                        "ON CONFLICT (dedupe_key) DO NOTHING"),
                        {"ts": now_iso(), "d": d, "sym": sym, "k": f"job:{d}:excluded:{sym}",
                         "m": f"{sym} not traded on {d}: {why}. No position is held in it."})
                    queue_alert(c, "urgent", f"[trader] data problem: {sym}", f"{sym} excluded from trading on {d}: {why}",
                                f"data:{d}:{sym}")
                for acct in self.accounts():
                    key = acct.key
                    run_id, start = runs[key]
                    row = c.execute(text("SELECT state_json FROM run_state WHERE run_id = :r"), {"r": run_id}).fetchone()
                    state = json.loads(row[0]) if row else None
                    if state and state["last_date"] and state["last_date"] >= d:
                        continue  # defensive: this account already has day d
                    advisor = self._review_book(acct, d, self._stored_reviews(c, key, d)) if acct.ai else None
                    eng = self._engine(acct, frames, tradable, d, start, state, advisor)
                    j = eng.run(until=d)
                    if eng.last_date != d:
                        raise RuntimeError(f"{key}: engine did not process {d} (last {eng.last_date})")
                    write_journal(c, run_id, key, j, now_iso())
                    save_engine_state(c, run_id, eng, now_iso())
                    for e in j.events:
                        if e.severity == "urgent":
                            queue_alert(c, "urgent", f"[trader] {key.split(':')[-1]}: {e.type.replace('_', ' ')}",
                                        f"{d}: {e.message}", f"ev:{key}:{e.date}:{e.type}")
                    eq = j.equity[-1]
                    accounts_out[key] = {"equity": eq["equity"], "positions": eq["positions"],
                                         "trades_closed": len(j.trades), "drawdown": eq["drawdown_pct"]}
                _test_pause_in_tx(self.paths)
                c.execute(text("UPDATE job_runs SET status = 'ok', finished_at = :t, heartbeat_at = :t, error = NULL "
                               "WHERE bar_date = :d"), {"t": now_iso(), "d": d})
            log.info("processed %s for %d account(s)", d, len(accounts_out))
            return DayResult(d, "ok", None, accounts_out)
        except Exception as exc:
            log.exception("run for %s failed", d)
            return self._give_up(d, "failed", f"{type(exc).__name__}: {exc}", "run_failed")

    def _give_up(self, d: str, status: str, error: str, event_type: str) -> DayResult:
        with self.db.tx() as c:
            c.execute(text("UPDATE job_runs SET status = :s, finished_at = :t, error = :e WHERE bar_date = :d"),
                      {"s": status, "t": now_iso(), "e": error, "d": d})
            attempts = c.execute(text("SELECT attempts FROM job_runs WHERE bar_date = :d"), {"d": d}).scalar()
            msg = (f"Daily run for {d} {status}: {error}. Nothing was traded for this day; it is retried "
                   f"automatically (attempt {attempts}).")
            # ONE event per day and cause, refreshed on each retry (a long outage must not flood the feed)
            c.execute(text(
                "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, message, details_json, dedupe_key) "
                "VALUES (NULL, :ts, :d, :t, 'urgent', :m, :det, :k) ON CONFLICT (dedupe_key) DO UPDATE SET "
                "ts = excluded.ts, message = excluded.message, details_json = excluded.details_json"),
                {"ts": now_iso(), "d": d, "t": event_type, "m": msg, "k": f"job:{d}:{event_type}",
                 "det": json.dumps({"attempts": attempts})})
            queue_alert(c, "urgent", f"[trader] daily run {status} for {d}", msg, f"job:{d}:{status}")
        log.error("day %s %s: %s", d, status, error)
        return DayResult(d, status, error)

    def _queue_daily_summary(self, r: DayResult) -> None:
        lines = []
        with self.db.read() as c:
            for key, v in r.accounts.items():
                prev = c.execute(text(
                    "SELECT e.equity FROM equity_snapshots e JOIN runs r ON r.id = e.run_id WHERE r.run_key = :k "
                    "AND e.bar_date < :d ORDER BY e.bar_date DESC LIMIT 1"), {"k": key, "d": r.bar_date}).scalar()
                base = prev if prev else self.cfg.accounts.starting_equity
                pnl = v["equity"] - base
                lines.append(f"{key.split(':')[-1]}: equity ${v['equity']:,.2f}, day P&L {'+' if pnl >= 0 else '-'}"
                             f"${abs(pnl):,.2f} ({pnl / base * 100:+.2f}%), open positions {v['positions']}")
        body = f"Paper trading summary for {r.bar_date} (UTC, simulation only):\n" + "\n".join(lines)
        with self.db.tx() as c:
            queue_alert(c, "info", f"[trader] daily summary {r.bar_date}", body, f"summary:{r.bar_date}")
