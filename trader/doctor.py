"""`trader doctor`: a ✅/❌ health checklist for the installation."""

from __future__ import annotations

import importlib
import os
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable

from trader.config import AppConfig, Secrets, load_config, load_secrets
from trader.paths import Paths, cli_hint, venv_bin

REQUIRED_PACKAGES = [
    "ccxt", "pandas", "numpy", "sqlalchemy", "apscheduler", "fastapi", "uvicorn", "jinja2",
    "pydantic", "pydantic_settings", "typer", "httpx", "pyarrow", "yaml", "dotenv",
]

ICONS = {"ok": "✅", "fail": "❌", "warn": "⚠️ ", "skip": "➖"}


@dataclass
class Check:
    name: str
    status: str  # ok | fail | warn | skip
    detail: str = ""

    def line(self) -> str:
        return f"{ICONS[self.status]} {self.name}" + (f" — {self.detail}" if self.detail else "")


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # a crashing check is a failed check, never a crashed doctor
        return Check(name, "fail", f"{type(exc).__name__}: {exc}")


def check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    return Check("Python version", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
                 + ("" if ok else " — need 3.11+"))


def check_packages() -> Check:
    missing = []
    for mod in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            missing.append(f"{mod} ({type(exc).__name__})")
    if missing:
        return Check("Packages import", "fail", "missing: " + ", ".join(missing))
    return Check("Packages import", "ok", f"{len(REQUIRED_PACKAGES)} packages")


def check_config(paths: Paths) -> tuple[Check, AppConfig | None]:
    if not paths.config_file.exists():
        return Check("Config", "fail", f"{paths.config_file} missing — run the installer"), None
    cfg = load_config(paths)
    return Check("Config valid, mode=paper", "ok", f"exchange={cfg.data.exchange}, source={cfg.data.source}"), cfg


def check_env_file(paths: Paths) -> Check:
    p = paths.env_file
    if not p.exists():
        return Check(".env secrets file", "fail", "missing — run the installer")
    if os.name == "nt":
        return Check(".env secrets file", "ok", "present (Windows ACLs not checked)")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        return Check(".env secrets file", "fail", f"permissions {oct(mode)} — must be owner-only (chmod 600 .env)")
    return Check(".env secrets file", "ok", f"owner-only ({oct(mode)})")


def check_dirs(paths: Paths) -> Check:
    bad = []
    for d in (paths.data, paths.logs, paths.run, paths.reports, paths.cache):
        if not d.is_dir():
            bad.append(f"{d.name}: missing")
            continue
        probe = d / ".write_probe"
        try:
            probe.write_text("x")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{d.name}: {exc}")
    return Check("Directories writable", "fail" if bad else "ok", "; ".join(bad) or "data, logs, run, reports")


def check_db(paths: Paths) -> Check:
    from trader.db import Database

    db = Database(paths.db_file)
    try:
        cur, latest = db.schema_version(), db.latest_version()
        if cur < latest:
            return Check("Database", "fail", f"schema v{cur} < v{latest} — run: {cli_hint('db migrate')}")
        db.set_kv("doctor_last_check", str(time.time()))
        with db.read() as c:
            mode = c.exec_driver_sql("PRAGMA journal_mode").scalar()
        return Check("Database writable", "ok", f"{paths.db_file.name} schema v{cur}, journal={mode}")
    finally:
        db.dispose()


def check_exchange(cfg: AppConfig, paths: Paths) -> Check:
    from trader.data import PublicMarketData, resolve_symbols
    from trader.timeutil import ms, now_utc
    from datetime import timedelta

    if cfg.data.source == "synthetic":
        return Check("Exchange data reachable", "warn", "data.source=synthetic — using SYNTHETIC prices, not real ones")
    ex_id = cfg.data.exchange
    client = PublicMarketData(ex_id, timeout_s=15)
    markets = client.load_markets()
    found = resolve_symbols(markets, cfg.data.assets, cfg.data.quote_preference)
    sym = found.get("BTC") or next(iter(found.values()), None)
    if not sym:
        return Check("Exchange data reachable", "fail", f"{ex_id}: none of {cfg.data.assets} listed")
    since = ms(now_utc() - timedelta(days=3))
    rows = client.fetch_ohlcv(sym, "1d", since, 5)
    if not rows:
        return Check("Exchange data reachable", "fail", f"{ex_id}: {sym} returned no candles")
    missing = [a for a in cfg.data.assets if a not in found]
    detail = f"{ex_id}: {len(found)}/{len(cfg.data.assets)} assets listed; {sym} last close {rows[-1][4]}"
    if missing:
        detail += f"; not listed: {missing}"
    return Check("Exchange data reachable", "warn" if missing else "ok", detail)


def check_cache(cfg: AppConfig, paths: Paths) -> Check:
    from trader.data import MarketData, freshness_problem, history_years

    md = MarketData(cfg, paths)
    syms = md.symbols()
    if not syms:
        return Check("Price history cached", "fail", f"no cached data — run: {cli_hint('data fetch')}")
    parts, status = [], "ok"
    for s in syms:
        if md.cache.load(s) is None:
            parts.append(f"{s}: none")
            status = "fail"
            continue
        df, rep = md.load_checked(s)
        if not rep.ok:
            parts.append(f"{s}: NOT TRADABLE ({'; '.join(rep.errors)})")
            status = "fail"
            continue
        yrs = history_years(df)
        stale = freshness_problem(df)
        tag = f"{s} {df.index[0].date()}→{df.index[-1].date()} ({yrs:.1f}y)"
        if stale:
            tag += f" [{stale}]"
            status = "warn" if status == "ok" else status
        if s.split("/")[0] in ("BTC", "ETH") and yrs < 5:
            tag += " [<5y history]"
            status = "warn" if status == "ok" else status
        parts.append(tag)
    src = "SYNTHETIC " if md.source == "synthetic" else ""
    return Check(f"{src}Price history cached", status, "; ".join(parts))


def check_port(cfg: AppConfig, paths: Paths) -> Check:
    host, port = cfg.server.host, cfg.server.port
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError:
        from trader.control import running_pid

        pid = running_pid(paths)
        if pid:
            return Check("Dashboard port", "ok", f"{host}:{port} in use by the running trader (PID {pid})")
        return Check("Dashboard port", "fail", f"{host}:{port} is in use by another program — change server.port")
    finally:
        s.close()
    return Check("Dashboard port", "ok", f"{host}:{port} free")


def check_clock(cfg: AppConfig) -> Check:
    import httpx

    urls = {
        "bitstamp": "https://www.bitstamp.net/api/v2/ticker/btcusd/",
        "coinbaseexchange": "https://api.exchange.coinbase.com/time",
    }
    candidates = [urls.get(cfg.data.exchange), "https://pypi.org/simple/", "https://www.cloudflare.com/"]
    errors = []
    for url in [u for u in candidates if u]:
        try:
            t0 = time.time()
            r = httpx.head(url, timeout=10, follow_redirects=True)
            t1 = time.time()
            date_hdr = r.headers.get("date")
            if not date_hdr:
                errors.append(f"{url}: no Date header")
                continue
            server = parsedate_to_datetime(date_hdr).timestamp()
            skew = (t0 + t1) / 2 - server
            host = httpx.URL(url).host
            if abs(skew) <= 60:
                return Check("Clock synced (±60 s)", "ok", f"skew {skew:+.1f}s vs {host}")
            return Check("Clock synced (±60 s)", "fail", f"skew {skew:+.1f}s vs {host} — enable automatic time sync")
        except Exception as exc:
            errors.append(f"{httpx.URL(url).host}: {type(exc).__name__}")
    return Check("Clock synced (±60 s)", "warn", "could not reach a time source: " + "; ".join(errors))


def check_alerts(cfg: AppConfig, secrets: Secrets, send_test: bool = False) -> Check:
    ch = cfg.alerts.channel
    if ch == "none":
        return Check("Alert channel", "skip", "alerts disabled (alerts.channel: none)")
    if ch == "email":
        if not secrets.email_configured():
            return Check("Alert channel (email)", "skip", "not configured yet — fill SMTP_* and ALERT_EMAIL_* in .env")
        from trader.alerts import smtp_connect

        with smtp_connect(secrets, timeout=15):
            pass
        if send_test:
            from trader.alerts import send_test_alert

            send_test_alert(cfg, secrets)
            return Check("Alert channel (email)", "ok", f"login OK; test email sent to {secrets.alert_email_to}")
        return Check("Alert channel (email)", "ok", f"SMTP login OK ({secrets.smtp_host}); use --send-test-alert to send one")
    if ch == "telegram":
        if not secrets.telegram_configured():
            return Check("Alert channel (telegram)", "skip", "not configured yet — fill TELEGRAM_* in .env")
        import httpx

        tok = secrets.telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
        r = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10)
        ok = r.status_code == 200 and r.json().get("ok")
        return Check("Alert channel (telegram)", "ok" if ok else "fail", "bot token valid" if ok else f"HTTP {r.status_code}")
    return Check("Alert channel", "fail", f"unknown channel {ch}")


def check_ai(cfg: AppConfig, secrets: Secrets) -> Check:
    name = "AI analyst (optional)"
    if not cfg.llm.enabled:
        return Check(name, "skip", "off (llm.enabled: false) - trades follow the rules alone")
    try:
        importlib.import_module("anthropic")
    except ImportError:
        return Check(name, "fail", f"package missing - run: {venv_bin('pip')} install -r requirements-llm.txt "
                                   "(until then every review falls back to the rules)")
    if not (secrets.anthropic_api_key and secrets.anthropic_api_key.get_secret_value()):
        return Check(name, "fail", "ANTHROPIC_API_KEY is empty in .env (every review falls back to the rules)")
    return Check(name, "ok", f"on, model {cfg.llm.model} pinned, key set; no call made here - try: "
                             f"{cli_hint('llm test')}")


def check_pip_audit(paths: Paths) -> Check:
    reqs = [p for p in (paths.root / "requirements.txt", paths.root / "requirements-llm.txt") if p.exists()]
    try:
        importlib.import_module("pip_audit")
    except ImportError:
        return Check("pip-audit (dependency CVEs)", "warn", f"pip-audit not installed — run: {venv_bin('pip')} install -r requirements-dev.txt")
    out = subprocess.run(
        [sys.executable, "-m", "pip_audit", *[a for r in reqs for a in ("-r", str(r))], "--no-deps", "--disable-pip",
         "--progress-spinner", "off"],
        capture_output=True, text=True, timeout=600,
    )
    lines = [l for l in (out.stdout + out.stderr).splitlines() if l.strip() and not l.startswith("WARNING:")]
    if out.returncode == 0:
        return Check("pip-audit (dependency CVEs)", "ok", lines[-1] if lines else "no known vulnerabilities")
    return Check("pip-audit (dependency CVEs)", "fail", "\n      ".join(lines)[-2000:])


def run_doctor(paths: Paths, *, full: bool = False, send_test_alert: bool = False, network: bool = True) -> list[Check]:
    checks: list[Check] = [_safe("Python version", check_python), _safe("Packages import", check_packages)]
    cfg: AppConfig | None = None
    try:
        c, cfg = check_config(paths)
        checks.append(c)
    except Exception as exc:
        checks.append(Check("Config", "fail", str(exc)))
    checks.append(_safe(".env secrets file", lambda: check_env_file(paths)))
    checks.append(_safe("Directories writable", lambda: check_dirs(paths)))
    checks.append(_safe("Database writable", lambda: check_db(paths)))
    if cfg is not None:
        if network:
            checks.append(_safe("Exchange data reachable", lambda: check_exchange(cfg, paths)))
        checks.append(_safe("Price history cached", lambda: check_cache(cfg, paths)))
        checks.append(_safe("Dashboard port", lambda: check_port(cfg, paths)))
        if network:
            checks.append(_safe("Clock synced (±60 s)", lambda: check_clock(cfg)))
        secrets = load_secrets(paths)
        checks.append(_safe("Alert channel", lambda: check_alerts(cfg, secrets, send_test_alert)))
        checks.append(_safe("AI analyst (optional)", lambda: check_ai(cfg, secrets)))
    if full:
        checks.append(_safe("pip-audit (dependency CVEs)", lambda: check_pip_audit(paths)))
    return checks
