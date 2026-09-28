"""Process control behind the start/stop/status/restart/logs scripts (same code on every OS)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import webbrowser
from datetime import datetime

from trader.config import AppConfig
from trader.paths import Paths, self_command
from trader.timeutil import now_utc

STOPPED_EXIT = 3  # like `systemctl status`: 0 = running, 3 = not running


# ------------------------------------------------------------------------------ process helpers
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:  # reap it if it is our own exited child (a zombie still answers kill(pid, 0))
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return _proc_state(pid) != "Z"


def _proc_state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        pass
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip()[:1]


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        return out.stdout
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout


def is_trader_process(pid: int) -> bool:
    """Guard against PID reuse: never signal a process that is not our server."""
    cmd = _cmdline(pid)
    if os.name == "nt":
        return "python" in cmd.lower() or "trader" in cmd.lower()  # tasklist shows only the image name
    return "trader" in cmd and "serve" in cmd


def read_pid(paths: Paths) -> int | None:
    try:
        return int(paths.pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def running_pid(paths: Paths) -> int | None:
    pid = read_pid(paths)
    if pid and pid_alive(pid) and is_trader_process(pid):
        return pid
    return None


def clean_stale_pid(paths: Paths) -> int | None:
    """Remove a PID file whose process no longer exists (or is not ours). Returns the stale PID."""
    pid = read_pid(paths)
    if paths.pid_file.exists() and running_pid(paths) is None:
        paths.pid_file.unlink(missing_ok=True)
        return pid if pid is not None else -1
    return None


def _client():
    import httpx

    return httpx.Client(timeout=3.0, trust_env=False)  # never route localhost through a proxy


def base_url(cfg: AppConfig) -> str:
    return f"http://127.0.0.1:{cfg.server.port}"


def dashboard_url(cfg: AppConfig) -> str:
    return f"http://{cfg.server.host}:{cfg.server.port}"


def health(cfg: AppConfig) -> dict | None:
    try:
        with _client() as c:
            r = c.get(base_url(cfg) + "/health")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _tail(path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except FileNotFoundError:
        return ""


def _ago(ts: str | None) -> str:
    if not ts:
        return "never"
    dt = datetime.fromisoformat(ts)
    secs = (now_utc() - dt).total_seconds()
    if secs < 90:
        return f"{int(secs)} s ago"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    return f"{secs / 3600:.0f} h ago"


def _dur(secs: float) -> str:
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s")


# ------------------------------------------------------------------------------ commands
def start(cfg: AppConfig, paths: Paths, *, open_browser: bool = True, wait: float = 60.0) -> int:
    pid = running_pid(paths)
    if pid:
        print(f"already running (PID {pid}) — dashboard: {dashboard_url(cfg)}")
        return 0
    stale = clean_stale_pid(paths)
    if stale:
        print(f"removed stale PID file (process {stale} no longer exists)")
    from trader.server import bind_socket

    try:
        bind_socket(cfg.server.host, cfg.server.port).close()
    except OSError:
        print(f"❌ port {cfg.server.port} on {cfg.server.host} is already in use by another program; "
              "change server.port in config.yaml")
        return 1
    paths.logs.mkdir(parents=True, exist_ok=True)
    out = open(paths.logs / "serve.out", "ab")
    kw: dict = {"cwd": str(paths.root), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT,
                "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True  # survives the terminal closing
    proc = subprocess.Popen([*self_command(), "serve"], **kw)
    out.close()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"❌ the trader exited during startup (code {proc.returncode}). Last output:")
            print(_tail(paths.logs / "serve.out"))
            return 1
        h = health(cfg)
        if h and h.get("pid") == proc.pid:
            print(f"✅ started (PID {proc.pid}) — dashboard: {dashboard_url(cfg)}")
            if open_browser:
                try:
                    webbrowser.open(dashboard_url(cfg))
                except Exception:
                    pass
            return 0
        time.sleep(0.3)
    print(f"❌ the trader did not answer within {wait:.0f} s; stopping it. See {paths.logs / 'serve.out'}")
    proc.kill()
    return 1


def stop(cfg: AppConfig, paths: Paths, *, timeout: float = 30.0) -> int:
    pid = running_pid(paths)
    if not pid:
        stale = clean_stale_pid(paths)
        print("not running" + (f" (removed stale PID file for process {stale})" if stale else ""))
        return 0
    try:
        token = paths.shutdown_token_file.read_text().strip()
        with _client() as c:
            r = c.post(base_url(cfg) + "/api/shutdown", headers={"X-Shutdown-Token": token})
        if r.status_code != 200:
            print(f"shutdown request refused (HTTP {r.status_code}: {r.text[:100]})")
    except Exception as exc:
        print(f"could not reach the shutdown endpoint ({type(exc).__name__}); waiting, then forcing if needed")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            if read_pid(paths) == pid:  # normally the server removes it itself on a clean exit
                paths.pid_file.unlink(missing_ok=True)
            print(f"✅ stopped (PID {pid})")
            return 0
        time.sleep(0.2)
    if is_trader_process(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
        for _ in range(50):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
    if read_pid(paths) == pid:
        paths.pid_file.unlink(missing_ok=True)
    print(f"⚠️  did not stop within {timeout:.0f} s — force-killed (PID {pid}). "
          "Any unfinished day was rolled back and will be re-run on next start.")
    return 0


def status_line(cfg: AppConfig, paths: Paths) -> tuple[str, int]:
    from sqlalchemy import text

    from trader.db import Database

    stale = clean_stale_pid(paths)
    last = None
    if paths.db_file.exists():
        db = Database(paths.db_file)
        try:
            with db.read() as c:
                last = c.execute(text("SELECT bar_date, finished_at FROM job_runs WHERE status = 'ok' "
                                      "ORDER BY bar_date DESC LIMIT 1")).fetchone()
        except Exception:
            last = None
        finally:
            db.dispose()
    last_s = f"last successful run: day {last[0]} (finished {_ago(last[1])})" if last else "last successful run: none yet"
    warn = ""
    if last and last[1]:
        age_h = (now_utc() - datetime.fromisoformat(last[1])).total_seconds() / 3600
        if age_h > cfg.scheduler.heartbeat_stale_hours:
            warn = f" · ⚠️  last successful run is {age_h:.0f} h old"
    pid = running_pid(paths)
    if pid:
        h = health(cfg) or {}
        nxt = h.get("next_run")
        nxt_s = f"next run: {nxt[:16].replace('T', ' ')} UTC" if nxt else "next run: unknown"
        up = f"up {_dur(h['uptime_s'])}" if "uptime_s" in h else "not answering"
        busy = " · processing now" if h.get("busy") else ""
        return f"● running · PID {pid} · {up} · {last_s} · {nxt_s}{busy}{warn}", 0
    extra = f" (removed stale PID file for process {stale})" if stale else ""
    return f"○ stopped · {last_s} · next run: none (not running){warn}{extra}", STOPPED_EXIT


def status(cfg: AppConfig, paths: Paths) -> int:
    line, code = status_line(cfg, paths)
    print(line)
    return code


def restart(cfg: AppConfig, paths: Paths, *, open_browser: bool = True) -> int:
    stop(cfg, paths)
    return start(cfg, paths, open_browser=open_browser)


def _fmt_log(line: str) -> str:
    try:
        d = json.loads(line)
    except ValueError:
        return line.rstrip()
    extra = f"\n{d['exc']}" if d.get("exc") else ""
    return f"{d.get('ts', '')[:19]}Z {d.get('level', ''):<7} {d.get('logger', '')}: {d.get('msg', '')}{extra}"


def logs(paths: Paths, *, lines: int = 40, follow: bool = False) -> int:
    f = paths.logs / "trader.log"
    if not f.exists():
        print(f"no log yet at {f}")
        return 1
    for line in f.read_text(errors="replace").splitlines()[-lines:]:
        print(_fmt_log(line))
    if not follow:
        return 0
    pos, ino = f.stat().st_size, f.stat().st_ino
    try:
        while True:
            time.sleep(0.5)
            if not f.exists():
                continue
            st = f.stat()
            if st.st_ino != ino or st.st_size < pos:  # rotated at UTC midnight
                pos, ino = 0, st.st_ino
            if st.st_size > pos:
                with open(f, errors="replace") as fh:
                    fh.seek(pos)
                    for line in fh.read().splitlines():
                        print(_fmt_log(line), flush=True)
                    pos = fh.tell()
    except KeyboardInterrupt:
        return 0
