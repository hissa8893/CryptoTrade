"""`trader serve`: ONE process running both the scheduler and the web server.

FastAPI app + APScheduler BackgroundScheduler started in the app's lifespan. uvicorn is
run programmatically so we hold the Server object: the local shutdown endpoint stops the
process by setting `server.should_exit = True` (no OS signals, which are unreliable on
Windows); the lifespan shutdown then waits for any in-flight job (`scheduler.shutdown
(wait=True)`) so database writes finish cleanly.

Jobs (all UTC):
  daily      cron at scheduler.run_hour_utc:run_minute_utc -> process every closed day not yet done
  watchdog   every 5 minutes -> same, if a day is waiting (rescues runs missed while asleep)
  heartbeat  every 60 seconds -> "alive" timestamp in the DB
  startup    once, right after start -> crash recovery + catch-up
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from trader import __version__
from trader.config import AppConfig, assert_paper_mode
from trader.paths import Paths
from trader.dashboard import Dashboard
from trader.runtime import Runtime
from trader.timeutil import iso, now_iso
from trader.web import DASH_CSP, register_dashboard, session_ok

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": DASH_CSP,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _is_loopback(host: str | None) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def write_pid_file(paths: Paths) -> None:
    paths.run.mkdir(parents=True, exist_ok=True)
    tmp = paths.pid_file.with_suffix(".tmp")
    tmp.write_text(str(os.getpid()))
    os.replace(tmp, paths.pid_file)


def remove_pid_file(paths: Paths) -> None:
    try:
        if paths.pid_file.read_text().strip() == str(os.getpid()):
            paths.pid_file.unlink()
    except (FileNotFoundError, ValueError):
        pass


def create_app(cfg: AppConfig, paths: Paths, runtime: Runtime, *, start_scheduler: bool = True) -> FastAPI:
    assert_paper_mode(cfg)
    token_file = paths.shutdown_token_file

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started = time.time()
        app.state.started_iso = now_iso()
        write_pid_file(paths)
        runtime.startup_check()
        sched = BackgroundScheduler(timezone=timezone.utc, job_defaults={
            "coalesce": True, "max_instances": 1, "misfire_grace_time": cfg.scheduler.misfire_grace_seconds})
        if start_scheduler:
            sched.add_job(runtime.catch_up, CronTrigger(hour=cfg.scheduler.run_hour_utc,
                                                        minute=cfg.scheduler.run_minute_utc, timezone=timezone.utc),
                          id="daily", kwargs={"reason": "daily"}, name="daily run")
            sched.add_job(runtime.catch_up_if_due, IntervalTrigger(minutes=5), id="watchdog", name="watchdog")
            sched.add_job(runtime.heartbeat, IntervalTrigger(seconds=60), id="heartbeat", name="heartbeat")
            sched.add_job(runtime.catch_up, id="startup", kwargs={"reason": "startup"}, name="startup catch-up")
            sched.start()
            log.info("scheduler started; daily run at %02d:%02d UTC", cfg.scheduler.run_hour_utc,
                     cfg.scheduler.run_minute_utc)
        app.state.scheduler = sched
        try:
            yield
        finally:
            log.info("shutting down: waiting for any running job to finish")
            if sched.running:
                sched.shutdown(wait=True)
            runtime.db.dispose()
            remove_pid_file(paths)
            log.info("stopped cleanly")

    app = FastAPI(title="trader", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # This computer gets in directly. Other devices need a signed-in session (token login);
        # the shutdown endpoint is never reachable from another device.
        path = request.url.path
        if not _is_loopback(request.client.host if request.client else None):
            if path == "/api/shutdown":
                return JSONResponse({"error": "local only"}, status_code=403, headers=SECURITY_HEADERS)
            if not (path == "/login" or path.startswith("/static/") or session_ok(app, request)):
                if request.method == "GET" and (path in ("/", "/research") or path.startswith("/reports/")):
                    return RedirectResponse("/login", status_code=303, headers=SECURITY_HEADERS)
                return JSONResponse({"error": "sign in required"}, status_code=401, headers=SECURITY_HEADERS)
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def _next_run() -> str | None:
        sched = getattr(app.state, "scheduler", None)
        job = sched.get_job("daily") if sched and sched.running else None
        return iso(job.next_run_time) if job and job.next_run_time else None

    @app.get("/health")
    def health():
        ok = runtime.last_ok()
        return {
            "status": "ok", "pid": os.getpid(), "version": __version__, "mode": cfg.mode,
            "started_at": app.state.started_iso, "uptime_s": round(time.time() - app.state.started, 1),
            "busy": runtime.busy, "last_ok_day": ok[0] if ok else None, "last_ok_at": ok[1] if ok else None,
            "pending_days": runtime.pending_days(), "next_run": _next_run(),
            "heartbeat": runtime.db.get_kv("heartbeat"), "data_source": runtime.md.source,
        }

    @app.post("/api/shutdown")
    def shutdown(request: Request):
        if not _is_loopback(request.client.host if request.client else None):
            return JSONResponse({"error": "local only"}, status_code=403)
        given = request.headers.get("x-shutdown-token", "")
        try:
            expected = token_file.read_text().strip()
        except FileNotFoundError:
            return JSONResponse({"error": "no shutdown token on this install"}, status_code=500)
        if not expected or not hmac.compare_digest(given, expected):
            return JSONResponse({"error": "bad token"}, status_code=403)
        server = getattr(app.state, "server", None)
        if server is None:
            return JSONResponse({"error": "not running under uvicorn"}, status_code=500)
        log.info("shutdown requested via local endpoint")
        if not os.environ.get("TRADER_TEST_IGNORE_SHUTDOWN"):  # test hook: simulate a hung server
            server.should_exit = True
        return {"ok": True}

    register_dashboard(app, Dashboard(cfg, paths, runtime.db, runtime.md, runtime, next_run=_next_run))
    return app


def bind_socket(host: str, port: int) -> socket.socket:
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    if os.name != "nt":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.set_inheritable(True)
    return s


def serve(cfg: AppConfig, paths: Paths) -> None:
    import uvicorn

    from trader.control import running_pid

    other = running_pid(paths)
    if other and other != os.getpid():
        raise SystemExit(f"already running (PID {other})")
    # bind BEFORE starting anything, so a busy port is a clean error (not a half-started daemon)
    sockets = [bind_socket(cfg.server.host, cfg.server.port)]
    if not _is_loopback(cfg.server.host):
        sockets.append(bind_socket("127.0.0.1", cfg.server.port))  # local control always works
    runtime = Runtime(cfg, paths)
    app = create_app(cfg, paths, runtime)
    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=30)
    server = uvicorn.Server(config)
    app.state.server = server
    log.info("serving on http://%s:%d (pid %d)", cfg.server.host, cfg.server.port, os.getpid())
    server.run(sockets=sockets)
