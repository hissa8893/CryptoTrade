"""Real processes through the real scripts: start/stop/status/restart, stale PID files,
PID-reuse guard, force-kill, busy port, shutdown token, daemon catch-up, no orphans."""

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from dataclasses import dataclass

from trader.cli import app
from trader.paths import Paths

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX scripts")


@dataclass(frozen=True)
class Inst(Paths):
    port: int = 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def inst(home):
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    port = free_port()
    cfg = home.config_file.read_text().replace("source: exchange", "source: synthetic").replace("port: 8765", f"port: {port}")
    home.config_file.write_text(cfg)
    inst = Inst(root=home.root, port=port)
    yield inst
    home = inst
    # never leave a daemon behind, whatever happened in the test
    subprocess.run([str(REPO / "stop.sh"), "--timeout", "5"], env=env_for(home), capture_output=True, timeout=60)
    assert daemon_pids(home) == [], "orphaned trader process"


def env_for(home, fake_now="2020-11-02T00:12:00+00:00", **extra):
    e = dict(os.environ, TRADER_HOME=str(home.root), TRADER_FAKE_NOW=fake_now)
    e.update(extra)
    return e


def sh(home, script, *args, env=None, timeout=120):
    r = subprocess.run([str(REPO / script), *args], env=env or env_for(home), capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def daemon_pids(home) -> list[int]:
    """Every live `trader serve` process that belongs to THIS test install (by its environment)."""
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ")
            env = (p / "environ").read_bytes().split(b"\0")
            stat = (p / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if b"trader serve" in cmd and f"TRADER_HOME={home.root}".encode() in env and stat != "Z":
            out.append(int(p.name))
    return out


def wait_health(home, pred, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            h = httpx.get(f"http://127.0.0.1:{home.port}/health", timeout=2, trust_env=False).json()
            if pred(h):
                return h
        except Exception:
            pass
        time.sleep(0.3)
    raise AssertionError("health condition not reached")


def test_full_lifecycle_twice_in_a_row(inst):
    code, out = sh(inst, "start.sh", "--no-browser")
    assert code == 0 and "started (PID" in out, out
    pid = int(out.split("PID ")[1].split(")")[0])
    assert daemon_pids(inst) == [pid]
    code, out = sh(inst, "start.sh", "--no-browser")
    assert code == 0 and f"already running (PID {pid})" in out
    assert daemon_pids(inst) == [pid]  # no second process
    wait_health(inst, lambda h: h["last_ok_day"] == "2020-11-01")
    code, out = sh(inst, "status.sh")
    assert code == 0 and out.startswith(f"● running · PID {pid}") and "last successful run: day 2020-11-01" in out
    assert "next run:" in out and "UTC" in out
    code, out = sh(inst, "stop.sh")
    assert code == 0 and f"stopped (PID {pid})" in out and "force" not in out
    assert daemon_pids(inst) == [] and not inst.pid_file.exists()
    code, out = sh(inst, "stop.sh")
    assert code == 0 and out.splitlines()[0] == "not running"
    code, out = sh(inst, "status.sh")
    assert code == 3 and out.startswith("○ stopped")
    # second round, via restart
    code, out = sh(inst, "start.sh", "--no-browser")
    pid2 = int(out.split("PID ")[1].split(")")[0])
    code, out = sh(inst, "restart.sh", "--no-browser")
    assert code == 0 and f"stopped (PID {pid2})" in out and "started (PID" in out
    pid3 = int(out.split("started (PID ")[1].split(")")[0])
    assert pid3 != pid2 and daemon_pids(inst) == [pid3]
    code, out = sh(inst, "restart.sh", "--no-browser")
    assert code == 0 and daemon_pids(inst) and daemon_pids(inst) != [pid3]
    assert sh(inst, "stop.sh")[0] == 0 and daemon_pids(inst) == []
    code, out = sh(inst, "logs.sh", "-n", "5")
    assert code == 0 and "Finished server process" in out


def test_stale_pid_file_is_detected_and_cleaned(inst):
    dead = subprocess.Popen(["true"])
    dead.wait()
    inst.pid_file.write_text(str(dead.pid))
    code, out = sh(inst, "status.sh")
    assert code == 3 and f"removed stale PID file for process {dead.pid}" in out
    assert not inst.pid_file.exists()
    inst.pid_file.write_text(str(dead.pid))
    code, out = sh(inst, "start.sh", "--no-browser")
    assert code == 0 and "removed stale PID file" in out and "started" in out


def test_pid_reuse_never_kills_an_unrelated_process(inst):
    victim = subprocess.Popen(["sleep", "60"])
    try:
        inst.pid_file.write_text(str(victim.pid))  # PID file now points at something that is NOT the trader
        code, out = sh(inst, "stop.sh", "--timeout", "2")
        assert code == 0 and "not running" in out
        assert victim.poll() is None  # untouched
    finally:
        victim.kill()
        victim.wait()


def test_hung_server_is_force_killed_after_timeout(inst):
    env = env_for(inst, TRADER_TEST_IGNORE_SHUTDOWN="1")
    code, out = sh(inst, "start.sh", "--no-browser", env=env)
    assert code == 0, out
    t0 = time.time()
    code, out = sh(inst, "stop.sh", "--timeout", "3")
    assert code == 0 and "did not stop within 3 s — force-killed" in out
    assert time.time() - t0 < 20
    assert daemon_pids(inst) == [] and not inst.pid_file.exists()


def test_busy_port_is_a_clean_error(inst):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", inst.port))
    s.listen()
    try:
        code, out = sh(inst, "start.sh", "--no-browser")
        assert code == 1 and "already in use" in out
        assert daemon_pids(inst) == []
    finally:
        s.close()


def test_shutdown_endpoint_requires_the_token_and_headers_are_secure(inst):
    assert sh(inst, "start.sh", "--no-browser")[0] == 0
    base = f"http://127.0.0.1:{inst.port}"
    with httpx.Client(trust_env=False, timeout=5) as c:
        assert c.post(base + "/api/shutdown").status_code == 403
        assert c.post(base + "/api/shutdown", headers={"X-Shutdown-Token": "wrong"}).status_code == 403
        r = c.get(base + "/health")
        assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in r.headers
    assert len(daemon_pids(inst)) == 1  # still running after the bad attempts


def test_daemon_processes_days_and_catches_up_after_downtime(inst):
    """The real daemon: day 1, stop (computer off for 3 days), start -> 3 missed days in order."""
    assert sh(inst, "start.sh", "--no-browser")[0] == 0
    wait_health(inst, lambda h: h["last_ok_day"] == "2020-11-01")
    assert sh(inst, "stop.sh")[0] == 0
    env = env_for(inst, fake_now="2020-11-05T00:12:00+00:00")
    assert sh(inst, "start.sh", "--no-browser", env=env)[0] == 0
    h = wait_health(inst, lambda h: h["last_ok_day"] == "2020-11-04" and not h["busy"])
    assert h["pending_days"] == []
    c = sqlite3.connect(inst.db_file)
    rows = c.execute("SELECT bar_date, status, attempts FROM job_runs ORDER BY bar_date").fetchall()
    assert rows == [("2020-11-01", "ok", 1), ("2020-11-02", "ok", 1), ("2020-11-03", "ok", 1), ("2020-11-04", "ok", 1)]
    dup = c.execute("SELECT COUNT(*) FROM (SELECT run_id, bar_date FROM equity_snapshots GROUP BY run_id, bar_date "
                    "HAVING COUNT(*) > 1)").fetchone()[0]
    assert dup == 0
    assert c.execute("SELECT COUNT(DISTINCT run_id) FROM equity_snapshots").fetchone()[0] == 4
    assert list((inst.backups).glob("trader-*.db"))  # daily backup written
    assert sh(inst, "stop.sh")[0] == 0
