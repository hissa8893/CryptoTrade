"""Scheduler wiring inside `trader serve` (the settings that rescue runs missed while asleep)."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trader import timeutil
from trader.cli import app as cli
from trader.config import load_config
from trader.runtime import Runtime
from trader.server import create_app


def test_scheduler_jobs_and_health(home):
    CliRunner().invoke(cli, ["init"])
    home.config_file.write_text(home.config_file.read_text().replace("source: exchange", "source: synthetic"))
    timeutil.set_now(datetime(2020, 11, 2, 0, 12, tzinfo=timezone.utc))
    cfg = load_config(home)
    application = create_app(cfg, home, Runtime(cfg, home))
    with TestClient(application, client=("127.0.0.1", 50000)) as client:
        sched = application.state.scheduler
        jobs = {j.id: j for j in sched.get_jobs()}
        assert {"daily", "watchdog", "heartbeat"} <= set(jobs)
        daily = jobs["daily"]
        assert str(daily.trigger) == "cron[hour='0', minute='10']" and str(daily.trigger.timezone) == "UTC"
        assert daily.misfire_grace_time == 6 * 3600 and daily.coalesce and daily.max_instances == 1
        assert str(jobs["watchdog"].trigger) == "interval[0:05:00]"
        assert home.pid_file.exists()
        h = client.get("/health").json()
        assert h["status"] == "ok" and h["mode"] == "paper" and h["next_run"].endswith("00:10:00+00:00")
    assert not sched.running  # lifespan shutdown stopped it (waiting for jobs)
    assert not home.pid_file.exists()


def test_non_local_clients_are_refused(home):
    CliRunner().invoke(cli, ["init"])
    cfg = load_config(home)
    application = create_app(cfg, home, Runtime(cfg, home), start_scheduler=False)
    with TestClient(application, client=("192.168.1.50", 50000)) as client:
        assert client.get("/health").status_code == 401  # other devices must sign in (Phase 5)
        assert client.post("/api/shutdown").status_code == 403  # and can never shut it down
