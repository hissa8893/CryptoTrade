"""Daily runtime: catch-up, idempotency, live == backtest, crash recovery (incl. kill -9),
stale data, reconciliation, alerts, locking."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from trader import timeutil
from trader.backtest import run_backtest
from trader.cli import app
from trader.config import load_config
from trader.runtime import FileLock, Runtime

T0 = datetime(2020, 11, 2, 0, 12, tzinfo=timezone.utc)  # first run processes 2020-11-01
TABLES = ("signals", "decisions", "orders", "trades", "equity_snapshots", "risk_events", "positions", "job_runs",
          "run_state", "alerts")


@pytest.fixture
def live(home):
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    home.config_file.write_text(home.config_file.read_text().replace("source: exchange", "source: synthetic"))
    timeutil.set_now(T0)
    return home


def rt_for(paths):
    return Runtime(load_config(paths), paths)


def counts(paths) -> dict:
    c = sqlite3.connect(paths.db_file)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()


def at(days: int, hour: int = 0, minute: int = 12):
    timeutil.set_now(datetime(2020, 11, 2, hour, minute, tzinfo=timezone.utc) + timedelta(days=days))


def job_rows(paths):
    c = sqlite3.connect(paths.db_file)
    try:
        return c.execute("SELECT bar_date, status, attempts FROM job_runs ORDER BY bar_date").fetchall()
    finally:
        c.close()


# ----------------------------------------------------------------------------- scheduling / catch-up
def test_first_run_processes_only_the_latest_closed_day(live):
    rt = rt_for(live)
    res = rt.catch_up("test")
    assert [(r.bar_date, r.status) for r in res] == [("2020-11-01", "ok")]
    assert len(res[0].accounts) == 4  # S1, S2, S3 sub-accounts + combined portfolio
    assert rt.pending_days() == []


def test_never_processes_an_unclosed_day(live):
    rt = rt_for(live)
    rt.catch_up("test")
    at(1, hour=0, minute=0)  # exactly midnight: 2020-11-02 has just closed
    assert rt.pending_days() == ["2020-11-02"]
    at(0, hour=23, minute=59)
    assert rt.pending_days() == []  # 2020-11-02 still open at 23:59


def test_catch_up_three_missed_days_in_order(live):
    rt = rt_for(live)
    rt.catch_up("test")
    at(3)  # computer was off for 3 days
    res = rt.catch_up("test")
    assert [r.bar_date for r in res] == ["2020-11-02", "2020-11-03", "2020-11-04"]
    assert all(r.status == "ok" for r in res)
    c = sqlite3.connect(live.db_file)
    started = [r[0] for r in c.execute("SELECT started_at FROM job_runs ORDER BY bar_date")]
    assert started == sorted(started)  # processed in date order
    per_account = c.execute("SELECT r.run_key, COUNT(*) FROM equity_snapshots e JOIN runs r ON r.id = e.run_id "
                            "GROUP BY r.run_key").fetchall()
    assert len(per_account) == 4 and all(n == 4 for _, n in per_account)  # one snapshot per day per account


def test_running_the_same_day_twice_creates_zero_duplicates(live):
    rt = rt_for(live)
    rt.catch_up("test")
    at(5)
    rt.catch_up("test")
    before = counts(live)
    assert rt.catch_up("test") == []  # nothing pending
    frames = rt.md.load_all()
    r = rt.run_day("2020-11-06", frames)  # force a re-run of an already-processed day
    assert r.status == "ok" and r.accounts == {}  # every account already had it -> nothing written
    after = counts(live)
    assert after == before
    assert [x[0] for x in job_rows(live)] == sorted({x[0] for x in job_rows(live)})


def test_live_paper_trading_matches_backtest_exactly(live):
    """One code path: the day-by-day daemon (state saved and reloaded every day) must produce
    exactly what one continuous backtest produces over the same days."""
    rt = rt_for(live)
    rt.catch_up("test")  # 2020-11-01
    for k in (1, 2):  # two single days
        at(k)
        rt.catch_up("test")
    at(89)  # then a long gap: 87 missed days caught up one by one
    res = rt.catch_up("test")
    assert len(res) == 87 and all(r.status == "ok" for r in res)
    c = sqlite3.connect(live.db_file)
    rid = c.execute("SELECT id FROM runs WHERE run_key = 'paper:synthetic:PORTFOLIO'").fetchone()[0]
    live_eq = [(d, e) for d, e in c.execute("SELECT bar_date, equity FROM equity_snapshots WHERE run_id = ? "
                                            "ORDER BY bar_date", (rid,))]
    live_trades = c.execute("SELECT symbol, strategy, entry_ts, exit_ts, pnl FROM trades WHERE run_id = ? "
                            "ORDER BY exit_ts, symbol, strategy", (rid,)).fetchall()
    frames = rt.md.load_all()
    bt = run_backtest(load_config(live), frames, ["S1", "S2", "S3"], symbols=rt.md.symbols(),
                      data_source="synthetic", start="2020-11-01", end=live_eq[-1][0])
    bt_eq = [(e["bar_date"], e["equity"]) for e in bt.journal.equity]
    assert live_eq == bt_eq  # bit-for-bit identical every day
    bt_trades = sorted((t.symbol, t.strategy, t.entry_date, t.exit_date, t.pnl) for t in bt.journal.trades)
    assert sorted(live_trades) == bt_trades and len(bt_trades) >= 5


# ----------------------------------------------------------------------------- crash recovery
def test_exception_mid_day_rolls_back_every_account(live, monkeypatch):
    rt = rt_for(live)
    rt.catch_up("test")
    at(1)
    before = counts(live)
    import trader.runtime as rtmod

    real = rtmod.write_journal

    def boom(c, run_id, key, j, created):
        real(c, run_id, key, j, created)
        if key.endswith("PORTFOLIO"):  # after three accounts were already written
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(rtmod, "write_journal", boom)
    r = rt.catch_up("test")[0]
    assert r.status == "failed" and "simulated crash" in r.error
    after = counts(live)
    for t in ("signals", "decisions", "orders", "trades", "equity_snapshots", "positions", "run_state"):
        assert after[t] == before[t], t  # nothing from the failed day survived
    monkeypatch.setattr(rtmod, "write_journal", real)
    rt._failed_at.clear()
    r2 = rt.catch_up("test")[0]
    assert r2.status == "ok" and r2.bar_date == "2020-11-02"


def _env(paths, fake_now: str, **extra):
    env = dict(os.environ, TRADER_HOME=str(paths.root), TRADER_FAKE_NOW=fake_now)
    env.update(extra)
    return env


def test_kill_dash_9_mid_write_leaves_no_partial_rows_and_recovers(live):
    """A real process killed with SIGKILL while inside the day's transaction."""
    py = [sys.executable, "-m", "trader", "run-once"]
    subprocess.run(py, env=_env(live, "2020-11-02T00:12:00+00:00"), check=True, capture_output=True, timeout=120)
    before = counts(live)
    marker = live.run / "in_tx.marker"
    marker.unlink(missing_ok=True)
    proc = subprocess.Popen(py, env=_env(live, "2020-11-05T00:12:00+00:00", TRADER_TEST_PAUSE_IN_TX="60"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(600):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "process never reached the transaction"
    os.kill(proc.pid, signal.SIGKILL)  # no cleanup handlers, no graceful anything
    proc.wait(timeout=10)
    after = counts(live)
    for t in ("signals", "decisions", "orders", "trades", "equity_snapshots", "positions", "run_state", "alerts"):
        assert after[t] == before[t], t  # the uncommitted day vanished completely
    assert ("2020-11-02", "running", 1) in job_rows(live)  # the marker row committed before the day's tx
    # restart: recovery first, then the interrupted day and the two after it
    out = subprocess.run(py, env=_env(live, "2020-11-05T00:12:00+00:00"), capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    rows = job_rows(live)
    assert [r[:2] for r in rows] == [("2020-11-01", "ok"), ("2020-11-02", "ok"), ("2020-11-03", "ok"),
                                     ("2020-11-04", "ok")]
    c = sqlite3.connect(live.db_file)
    assert c.execute("SELECT COUNT(*) FROM risk_events WHERE type = 'run_interrupted'").fetchone()[0] == 1
    # positions table restored = saved engine state, for every account
    for run_id, sj in c.execute("SELECT run_id, state_json FROM run_state"):
        want = sorted((p["symbol"], p["strategy"], p["qty"]) for p in json.loads(sj)["broker"]["positions"])
        have = sorted(c.execute("SELECT symbol, strategy, qty FROM positions WHERE run_id = ?", (run_id,)).fetchall())
        assert want == have


def test_positions_table_reconciled_from_saved_state(live):
    rt = rt_for(live)
    rt.catch_up("test")
    at(20)
    rt.catch_up("test")
    c = sqlite3.connect(live.db_file)
    n = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert n > 0, "need open positions for this test"
    c.execute("DELETE FROM positions")
    c.commit()
    summary = rt.recover()
    assert summary["reconciled"]
    assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == n
    assert c.execute("SELECT COUNT(*) FROM risk_events WHERE type = 'positions_reconciled'").fetchone()[0] >= 1


# ----------------------------------------------------------------------------- stale data
def test_stale_data_skips_the_day_alerts_and_never_trades(live, monkeypatch):
    rt = rt_for(live)
    rt.catch_up("test")
    at(1)
    before = counts(live)
    real_load = rt.md.load_all
    monkeypatch.setattr(rt.md, "update", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("exchange down")))
    monkeypatch.setattr(rt.md, "load_all", lambda *a, **k: {s: df.loc[:"2020-11-01"] for s, df in real_load().items()})
    r = rt.catch_up("test")[0]
    assert r.status == "skipped" and "stale" in r.error and "exchange down" in r.error
    after = counts(live)
    assert after["equity_snapshots"] == before["equity_snapshots"] and after["signals"] == before["signals"]
    c = sqlite3.connect(live.db_file)
    assert c.execute("SELECT severity FROM risk_events WHERE type = 'data_not_ready'").fetchone()[0] == "urgent"
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE subject LIKE '%skipped%'").fetchone()[0] == 1
    # watchdog backs off, then the data comes back and the day is processed
    assert rt.catch_up_if_due() == []
    monkeypatch.undo()
    rt._failed_at.clear()
    assert rt.catch_up("test")[0].status == "ok"


def test_coin_without_a_position_is_excluded_not_blocking(live, monkeypatch):
    rt = rt_for(live)
    rt.catch_up("test")
    at(1)
    real_load = rt.md.load_all
    held = {p["symbol"] for st in rt._states().values() for p in st["broker"]["positions"]}
    victim = next(s for s in rt.md.symbols() if s not in held and not s.startswith("BTC"))
    monkeypatch.setattr(rt.md, "load_all",
                        lambda *a, **k: {s: (df.loc[:"2020-11-01"] if s == victim else df) for s, df in real_load().items()})
    r = rt.catch_up("test")[0]
    assert r.status == "ok"
    c = sqlite3.connect(live.db_file)
    assert c.execute("SELECT COUNT(*) FROM risk_events WHERE type = 'symbol_excluded' AND symbol = ?",
                     (victim,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM signals WHERE bar_date = '2020-11-02' AND symbol = ?",
                     (victim,)).fetchone()[0] == 0


# ----------------------------------------------------------------------------- alerts + locking
def test_alerts_deduplicated_and_skipped_when_channel_unconfigured(live):
    rt = rt_for(live)
    rt.catch_up("test")
    at(3)
    rt.catch_up("test")
    rt.catch_up("test")
    c = sqlite3.connect(live.db_file)
    rows = c.execute("SELECT subject, status, error FROM alerts WHERE subject LIKE '%daily summary%'").fetchall()
    assert len(rows) == 2  # one per catch-up batch (latest day), never duplicated
    assert all(s == "skipped" and "not configured" in e for _, s, e in rows)  # email not set up in tests


def test_daily_summary_email_is_really_sent_when_configured(live):
    from tests.test_alerts import _SMTPHandler
    import socketserver
    import threading

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    srv.messages, srv.rcpt, srv.mail_from = [], [], None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        live.env_file.write_text(f"SMTP_HOST=127.0.0.1\nSMTP_PORT={srv.server_address[1]}\nSMTP_STARTTLS=false\n"
                                 "ALERT_EMAIL_FROM=bot@example.com\nALERT_EMAIL_TO=me@example.com\n")
        rt = rt_for(live)
        rt.catch_up("test")
        assert len(srv.messages) == 1
        assert "daily summary 2020-11-01" in srv.messages[0] and "PORTFOLIO: equity $10,000.00" in srv.messages[0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_file_lock_blocks_a_second_processor(live):
    held = FileLock(live.run / "catchup.lock")
    assert held.acquire()
    try:
        assert rt_for(live).catch_up("test") == []  # another process "holds" it
        assert counts(live)["job_runs"] == 0
    finally:
        held.release()
    assert rt_for(live).catch_up("test")[0].status == "ok"


def test_run_once_refuses_while_daemon_running(live, monkeypatch):
    import trader.control as control

    monkeypatch.setattr(control, "running_pid", lambda paths: 4242)
    r = CliRunner().invoke(app, ["run-once"])
    assert r.exit_code == 1 and "processes days itself" in r.output


def test_retries_during_an_outage_keep_one_event_with_the_real_cause(live, monkeypatch):
    """Regression: every retry added another urgent event (~96/day from the watchdog), and a
    never-downloaded dataset was reported as a config problem."""
    rt = rt_for(live)
    monkeypatch.setattr(rt.md, "update", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("403 blocked")))
    monkeypatch.setattr(rt.md, "load_all", lambda *a, **k: {})
    monkeypatch.setattr(rt.md, "symbols", lambda: [])
    for _ in range(3):
        rt._failed_at.clear()
        assert rt.catch_up("test")[0].status == "skipped"
    c = sqlite3.connect(live.db_file)
    rows = c.execute("SELECT message FROM risk_events WHERE type = 'data_not_ready'").fetchall()
    assert len(rows) == 1 and "attempt 3" in rows[0][0]
    assert "no price data has been downloaded yet" in rows[0][0] and "403 blocked" in rows[0][0]
    assert c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_status_uses_the_app_clock(live):
    """Regression: status compared a (simulated) finish time with the real clock -> '51743 h ago'."""
    from trader.control import status_line

    rt = rt_for(live)
    rt.catch_up("test")
    at(0, hour=9, minute=12)  # 9 hours after the 00:12 run
    line, code = status_line(load_config(live), live)
    assert code == 3 and "finished 9 h ago" in line and "⚠️" not in line
    at(2, hour=3)  # > 26 h later
    assert "last successful run is 51 h old" in status_line(load_config(live), live)[0]
