"""Dashboard routes (read-only). Server-rendered Jinja + HTMX polling; no external requests.

Access: this computer (127.0.0.1) gets in directly. Any other device must sign in with the
login token printed once by the installer (only its SHA-256 hash is stored). Nothing on the
dashboard can change trades or settings; the only POST routes are sign-in and the
local-only shutdown endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trader.ai_report import ai_summary
from trader.charts import line_chart
from trader.dashboard import NEAR_STOP, RANGES, Dashboard
from trader.paths import PACKAGE_DIR
from trader.timeutil import now_utc

SESSION_COOKIE = "trader_session"
SESSION_TTL = 7 * 86400
LOGIN_LIMIT = (5, 300)  # attempts per window (seconds) per client address
REPORT_NAME = re.compile(r"^[A-Za-z0-9_.\-]+\.html$")

DASH_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"


# ------------------------------------------------------------------------------ formatting
def _neg(v) -> bool:
    return v is not None and v < -1e-12


def money(v, digits=2):
    if v is None:
        return "—"
    return f"{'−' if _neg(v) else ''}${abs(v):,.{digits}f}"


def smoney(v):
    if v is None:
        return "—"
    if abs(v) < 0.005:
        return "$0.00"
    return f"{'+' if v > 0 else '−'}${abs(v):,.2f}"


def pct(v, digits=2):
    if v is None:
        return "—"
    if abs(v) < 1e-12:
        v = 0.0
    return f"{v * 100:.{digits}f}%".replace("-", "−")


def spct(v):
    if v is None:
        return "—"
    if abs(v) < 0.00005:
        return "0.00%"
    return f"{'+' if v > 0 else '−'}{abs(v) * 100:.2f}%"


def price(v):
    if v is None:
        return "—"
    digits = 2 if v >= 100 else 4 if v >= 1 else 6
    return f"${v:,.{digits}f}"


def cls(v):
    return "" if v is None or abs(v) < 1e-9 else ("pos-t" if v > 0 else "neg-t")


def arrow(v):
    return "–" if v is None or abs(v) < 1e-9 else ("▲" if v > 0 else "▼")


def day(d):
    if not d:
        return "—"
    return date.fromisoformat(str(d)[:10]).strftime("%b %d").replace(" 0", " ")


def ago(ts):
    if not ts:
        return "never"
    secs = (now_utc() - datetime.fromisoformat(ts)).total_seconds()
    if secs < 90:
        return f"{max(0, int(secs))} s ago"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} d ago"


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    t.env.filters.update(money=money, money0=lambda v: money(v, 0), smoney=smoney, pct=pct,
                         pct0=lambda v: pct(v, 0), spct=spct, price=price, cls=cls, arrow=arrow, day=day, ago=ago)
    return t


# ------------------------------------------------------------------------------ auth helpers
def is_loopback(host: str | None) -> bool:
    import ipaddress

    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def token_matches(given: str, hash_file: Path) -> bool:
    try:
        stored = hash_file.read_text().strip()
    except FileNotFoundError:
        return False
    return bool(stored) and hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), stored)


def register_dashboard(app: FastAPI, dash: Dashboard) -> None:
    templates = make_templates()
    app.state.sessions = {}
    app.state.login_attempts = {}
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    cfg = dash.cfg

    def context(request: Request, acct_name: str | None, rng: str) -> dict:
        rng = rng if rng in RANGES else "all"
        acct = dash.account(acct_name)
        eqs = dash.equity_series(acct, rng) if acct else None
        chart = ddchart = None
        if eqs:
            series = [{"name": f"{acct['label']}", "short": acct["short"],
                       "color": "var(--pos)", "values": eqs["equity"]}]
            dds = [{"name": "Drawdown", "short": "Drawdown", "color": "var(--neg)", "values": eqs["dd"], "fill": True}]
            if "bh" in eqs:
                series.append({"name": "Buy & Hold basket", "short": "Buy & Hold", "color": "var(--muted)",
                               "values": eqs["bh"], "dash": True})
                dds.append({"name": "Buy & Hold drawdown", "short": "B&H", "color": "var(--muted)",
                            "values": eqs["bh_dd"], "dash": True})
            chart = line_chart(eqs["dates"], series, height=300, log=False, fmt="money", title="Equity vs Buy and Hold")
            ddchart = line_chart(eqs["dates"], dds, height=150, log=False, fmt="pct", title="Drawdown from peak",
                                 legend=False)
        return {
            "request": request, "acct": acct, "accounts": dash.accounts(), "rng": rng, "st": dash.status(acct),
            "head": dash.headline(acct), "chart": chart, "ddchart": ddchart, "positions": dash.positions(acct),
            "trades": dash.trades(acct), "events": dash.events(acct), "board": dash.scoreboard(),
            "alerts": dash.alerts_summary(), "max_positions": cfg.risk.max_positions,
            "heat_limit": cfg.risk.max_portfolio_heat, "near_stop": NEAR_STOP,
            "ai": ai_summary(dash.db, dash.md.source), "llm_on": cfg.llm.enabled,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, acct: str | None = None, range: str = "all"):  # noqa: A002
        return templates.TemplateResponse(request, "index.html", context(request, acct, range))

    @app.get("/p/live", response_class=HTMLResponse)
    def live(request: Request, acct: str | None = None, range: str = "all"):  # noqa: A002
        return templates.TemplateResponse(request, "partials/live.html", context(request, acct, range))

    @app.get("/research", response_class=HTMLResponse)
    def research(request: Request):
        return templates.TemplateResponse(request, "research.html", {"request": request, "reports": dash.reports()})

    @app.get("/reports/{name}")
    def report(name: str):
        p = (dash.paths.reports / name).resolve()
        if not REPORT_NAME.match(name) or p.parent != dash.paths.reports.resolve() or not p.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(p, media_type="text/html", headers={"Content-Security-Policy": REPORT_CSP})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})

    @app.post("/login")
    async def login(request: Request):
        host = request.client.host if request.client else "?"
        now = time.time()
        window = [t for t in app.state.login_attempts.get(host, []) if now - t < LOGIN_LIMIT[1]]
        if len(window) >= LOGIN_LIMIT[0]:
            return templates.TemplateResponse(request, "login.html", {"request": request,
                                              "error": "Too many attempts. Wait 5 minutes."}, status_code=429)
        window.append(now)
        app.state.login_attempts[host] = window
        form = parse_qs((await request.body()).decode(errors="replace"))
        given = (form.get("token") or [""])[0].strip()
        if not token_matches(given, dash.paths.dashboard_token_hash_file):
            return templates.TemplateResponse(request, "login.html", {"request": request,
                                              "error": "That token is not right."}, status_code=401)
        sid = secrets.token_urlsafe(32)
        app.state.sessions[sid] = now + SESSION_TTL
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="strict", path="/")
        return resp


def session_ok(app: FastAPI, request: Request) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    exp = app.state.sessions.get(sid) if sid and hasattr(app.state, "sessions") else None
    return bool(exp and exp > time.time())
