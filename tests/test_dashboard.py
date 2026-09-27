"""Dashboard: numbers match the DB, forced risk event shows + alerts, empty states, health
colours, access control, read-only, security headers, research page, speed."""

import hashlib
import os
import re
import shutil
import socketserver
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trader import timeutil
from trader.cli import app as cli
from trader.config import load_config
from trader.paths import Paths
from trader.runtime import Runtime
from trader.server import create_app
from trader.web import money, pct, smoney, spct

from tests.test_alerts import _SMTPHandler

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2020, 11, 2, 0, 12, tzinfo=timezone.utc)
DAYS = 66  # through 2021-01-05: includes trades, open positions, a cooldown and (with a 3% breaker) a breaker trip


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """An install that ran day by day for 66 days, with a tight breaker to force an urgent event
    and a local SMTP server to prove the alert is really delivered."""
    root = tmp_path_factory.mktemp("dash")
    old = {k: os.environ.get(k) for k in ("TRADER_HOME", "TRADER_FAKE_NOW")}
    os.environ["TRADER_HOME"] = str(root)
    os.environ.pop("TRADER_FAKE_NOW", None)
    for name in ("config.example.yaml", ".env.example", "requirements.txt"):
        shutil.copyfile(REPO / name, root / name)
    paths = Paths(root)
    paths.ensure_dirs()
    assert CliRunner().invoke(cli, ["init"]).exit_code == 0
    cfg_text = paths.config_file.read_text().replace("source: exchange", "source: synthetic")
    cfg_text = cfg_text.replace("drawdown_breaker: 0.15 ", "drawdown_breaker: 0.03 ").replace("drawdown_release: 0.10 ", "drawdown_release: 0.015")
    paths.config_file.write_text(cfg_text)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    srv.messages, srv.rcpt, srv.mail_from = [], [], None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    paths.env_file.write_text(f"SMTP_HOST=127.0.0.1\nSMTP_PORT={srv.server_address[1]}\nSMTP_STARTTLS=false\n"
                              "ALERT_EMAIL_FROM=bot@example.com\nALERT_EMAIL_TO=me@example.com\n")
    cfg = load_config(paths)
    rt = Runtime(cfg, paths)
    for k in range(DAYS):
        timeutil.set_now(T0 + timedelta(days=k))
        rt.catch_up("seed")
    timeutil.set_now(T0 + timedelta(days=DAYS - 1, hours=6))
    yield paths, cfg, srv
    srv.shutdown()
    srv.server_close()
    timeutil.set_now(None)
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def client(seeded):
    paths, cfg, _ = seeded
    timeutil.set_now(T0 + timedelta(days=DAYS - 1, hours=6))
    rt = Runtime(cfg, paths)
    rt.heartbeat()  # the running daemon writes one every minute; this test app has no scheduler
    application = create_app(cfg, paths, rt, start_scheduler=False)
    with TestClient(application, client=("127.0.0.1", 50000)) as c:
        yield c


def val(html: str, testid: str) -> float:
    m = re.search(rf'data-testid="{testid}" data-value="([^"]+)"', html)
    assert m, testid
    return float(m.group(1))


def db(paths):
    return sqlite3.connect(paths.db_file)


# ----------------------------------------------------------------------------- numbers == DB
@pytest.mark.parametrize("acct", ["PORTFOLIO", "S1", "S2", "S3"])
def test_headline_numbers_match_a_direct_db_query(seeded, client, acct):
    paths, cfg, _ = seeded
    html = client.get(f"/?acct={acct}").text
    c = db(paths)
    rid, start_eq = c.execute("SELECT id, starting_equity FROM runs WHERE run_key = ?", (f"paper:synthetic:{acct}",)).fetchone()
    (d1, e1, orisk, dd), (_, e0) = c.execute(
        "SELECT bar_date, equity, open_risk, drawdown_pct FROM equity_snapshots WHERE run_id = ? ORDER BY bar_date DESC LIMIT 1",
        (rid,)).fetchone(), c.execute(
        "SELECT bar_date, equity FROM equity_snapshots WHERE run_id = ? ORDER BY bar_date DESC LIMIT 1 OFFSET 1", (rid,)).fetchone()
    npos = c.execute("SELECT COUNT(*) FROM positions WHERE run_id = ?", (rid,)).fetchone()[0]
    assert val(html, "equity") == pytest.approx(e1, abs=1e-6)
    assert val(html, "day-pnl") == pytest.approx(e1 - e0, abs=1e-6)
    assert val(html, "total-return") == pytest.approx(e1 / start_eq - 1, abs=1e-12)
    assert val(html, "drawdown") == pytest.approx(dd, abs=1e-12)
    assert val(html, "positions") == npos
    assert val(html, "open-risk") == pytest.approx(orisk / e1, abs=1e-12)
    assert money(e1) in html  # and the visible text says the same thing


def test_positions_and_trades_match_db(seeded, client):
    paths, _, _ = seeded
    c = db(paths)
    rid = c.execute("SELECT id FROM runs WHERE run_key = 'paper:synthetic:PORTFOLIO'").fetchone()[0]
    html = client.get("/?acct=PORTFOLIO").text
    rows = c.execute("SELECT symbol, strategy, qty, avg_entry_px, current_stop, last_price FROM positions WHERE run_id = ?",
                     (rid,)).fetchall()
    assert html.count('class="pcard') == len(rows)
    for sym, strat, qty, entry, stop, last in rows:
        assert f'data-key="{strat}:{sym}"' in html
        assert smoney((last - entry) * qty) in html  # unrealized P&L
    trades = c.execute("SELECT id, pnl FROM trades WHERE run_id = ? AND exit_ts IS NOT NULL ORDER BY exit_ts DESC, id DESC "
                       "LIMIT 20", (rid,)).fetchall()
    assert trades, "seed should contain closed trades"
    assert html.count("<details data-trade=") == len(trades)
    for tid, pnl in trades:
        assert f'data-trade="{tid}"' in html and smoney(pnl) in html


def test_every_trade_has_a_full_decision_trail(seeded, client):
    html = client.get("/?acct=PORTFOLIO").text
    for block in re.findall(r"<details data-trade=.*?</details>", html, re.S):
        assert "Entry signal at the close of" in block
        assert "Risk check: pass" in block or "Risk check: shrunk" in block
        assert "LLM analyst" in block
        assert "Filled at the open of" in block
        assert ("Exit signal at the close of" in block) or ("Stop filled on" in block)


def test_scoreboard_matches_db(seeded, client):
    paths, _, _ = seeded
    html = client.get("/").text
    c = db(paths)
    for acct in ("S1", "S2", "S3", "PORTFOLIO"):
        rid, start = c.execute("SELECT id, starting_equity FROM runs WHERE run_key = ?",
                               (f"paper:synthetic:{acct}",)).fetchone()
        last = c.execute("SELECT equity FROM equity_snapshots WHERE run_id = ? ORDER BY bar_date DESC LIMIT 1", (rid,)).fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM trades WHERE run_id = ? AND exit_ts IS NOT NULL", (rid,)).fetchone()[0]
        row = re.search(rf"<td>[^<]*{'Portfolio' if acct == 'PORTFOLIO' else acct}[^<]*</td>(.*?)</tr>", html, re.S).group(1)
        assert spct(last / start - 1) in row
        assert re.findall(r'<td class="n">(\d+)</td>', row)[-1] == str(n)
    assert "Buy &amp; Hold basket" in html


# ----------------------------------------------------------------------------- forced risk event
def test_forced_risk_event_is_on_the_dashboard_and_was_emailed(seeded, client):
    paths, _, srv = seeded
    c = db(paths)
    ev = c.execute("SELECT bar_date, message FROM risk_events WHERE type = 'circuit_breaker_on'").fetchone()
    assert ev, "the 3% breaker should have tripped in the seed window"
    html = client.get("/?acct=PORTFOLIO").text
    assert "✕ URGENT" in html and "Circuit breaker ON" in html
    assert "Risk engine: Circuit breaker on — new entries blocked" in html or "circuit_breaker_off" in \
        {r[0] for r in c.execute("SELECT type FROM risk_events")}
    subjects = [m.split("Subject: ")[1].split("\n")[0] for m in srv.messages]
    assert any("circuit breaker on" in s for s in subjects)  # really delivered by SMTP
    assert c.execute("SELECT status FROM alerts WHERE subject LIKE '%circuit breaker on%'").fetchone()[0] == "sent"


# ----------------------------------------------------------------------------- health / status bar
def test_status_goes_red_after_26_hours_without_a_run(seeded, client):
    html = client.get("/").text
    assert 'data-level="ok"' in html and "Healthy" in html
    timeutil.set_now(T0 + timedelta(days=DAYS - 1, hours=30))  # last run finished ~30 h ago
    html = client.get("/").text
    assert 'data-level="bad"' in html and "Problem" in html and "last successful run is 30 h old" in html


def test_status_shows_amber_when_the_last_attempt_was_skipped(seeded):
    paths, cfg, _ = seeded
    tmp = paths.root / "data" / "trader-copy.db"
    shutil.copyfile(paths.db_file, tmp)
    try:
        c = db(paths)
        c.execute("INSERT INTO job_runs (bar_date, started_at, status, error, attempts) VALUES "
                  "('2099-01-01', 'x', 'skipped', 'data not ready', 1)")
        c.commit()
        timeutil.set_now(T0 + timedelta(days=DAYS - 1, hours=6))
        rt = Runtime(cfg, paths)
        rt.heartbeat()
        application = create_app(cfg, paths, rt, start_scheduler=False)
        with TestClient(application, client=("127.0.0.1", 50000)) as cl:
            html = cl.get("/").text
        assert 'data-level="warn"' in html and "last attempt skipped" in html
    finally:
        shutil.copyfile(tmp, paths.db_file)
        tmp.unlink()


def test_empty_states_on_a_fresh_install(home):
    CliRunner().invoke(cli, ["init"])
    cfg = load_config(home)
    application = create_app(cfg, home, Runtime(cfg, home), start_scheduler=False)
    with TestClient(application, client=("127.0.0.1", 50000)) as c:
        html = c.get("/").text
        assert "No paper account yet" in html and "No risk events yet" in html
        assert "The scoreboard fills in after the first daily run" in html
        assert "no successful run yet" in html and 'data-level="bad"' in html
        assert "No reports yet" in c.get("/research").text


# ----------------------------------------------------------------------------- live refresh
def test_htmx_polls_every_60s_and_partial_has_the_same_numbers(client):
    page = client.get("/?acct=S3&range=30d").text
    assert 'hx-trigger="every 60s"' in page and 'hx-get="/p/live?acct=S3&amp;range=30d"' in page
    part = client.get("/p/live?acct=S3&range=30d").text
    assert "<html" not in part and val(part, "equity") == val(page, "equity")


@pytest.mark.parametrize("rng,max_points", [("7d", 7), ("30d", 30), ("90d", 90)])
def test_range_toggle_limits_the_chart(client, rng, max_points):
    html = client.get(f"/?acct=PORTFOLIO&range={rng}").text
    data = re.search(r"data-series='([^']+)'", html).group(1)
    import html as h
    import json

    n = len(json.loads(h.unescape(data))["dates"])
    assert n <= max_points and n == min(max_points, DAYS)


# ----------------------------------------------------------------------------- security
def test_security_headers_and_no_external_resources(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
    assert not re.search(r"""(src|href)=["']https?://""", r.text)  # nothing loaded from the internet
    assert "<script>" not in r.text  # no inline scripts (CSP-safe)
    for asset in ("/static/vendor/htmx-2.0.11.min.js", "/static/dashboard.js", "/static/tokens.css", "/static/favicon.svg"):
        assert client.get(asset).status_code == 200


def test_dashboard_is_read_only(home):
    CliRunner().invoke(cli, ["init"])
    cfg = load_config(home)
    application = create_app(cfg, home, Runtime(cfg, home), start_scheduler=False)
    writes = sorted((r.path, sorted(r.methods)) for r in application.routes if getattr(r, "methods", None)
                    and r.methods & {"POST", "PUT", "PATCH", "DELETE"})
    assert writes == [("/api/shutdown", ["POST"]), ("/login", ["POST"])]


def test_other_devices_must_sign_in_with_the_token(seeded):
    paths, cfg, _ = seeded
    token = "correct-horse-battery"
    orig = paths.dashboard_token_hash_file.read_text()
    paths.dashboard_token_hash_file.write_text(hashlib.sha256(token.encode()).hexdigest())
    try:
        application = create_app(cfg, paths, Runtime(cfg, paths), start_scheduler=False)
        with TestClient(application, client=("100.101.102.103", 50000)) as c:  # e.g. a phone over Tailscale
            r = c.get("/", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login"
            assert c.get("/p/live").status_code == 401 and c.get("/health").status_code == 401
            assert c.get("/static/tokens.css").status_code == 200  # the login page must be styled
            assert c.post("/login", content="token=nope", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 401
            r = c.post("/login", content=f"token={token}", headers={"content-type": "application/x-www-form-urlencoded"},
                       follow_redirects=False)
            assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
            assert c.get("/").status_code == 200  # signed in
            assert c.post("/api/shutdown", headers={"X-Shutdown-Token": "x"}).status_code == 403  # never remotely
        with TestClient(application, client=("100.101.102.104", 50000)) as c:
            codes = [c.post("/login", content="token=bad", headers={"content-type": "application/x-www-form-urlencoded"}).status_code
                     for _ in range(6)]
            assert codes[:5] == [401] * 5 and codes[5] == 429  # brute force is rate-limited
    finally:
        paths.dashboard_token_hash_file.write_text(orig)


def test_research_page_lists_reports_and_blocks_path_tricks(seeded, client):
    paths, _, _ = seeded
    (paths.reports / "research_S1_20260101T000000.html").write_text(
        "<!doctype html><title>Research report</title><div>SYNTHETIC DATA</div>")
    page = client.get("/research").text
    assert "research_S1_20260101T000000.html" in page and "SYNTHETIC" in page
    r = client.get("/reports/research_S1_20260101T000000.html")
    assert r.status_code == 200 and "script-src 'unsafe-inline'" in r.headers["content-security-policy"]
    for bad in ("../config.yaml", "..%2Fconfig.yaml", "%2e%2e/.env", ".env", "x.py"):
        assert client.get(f"/reports/{bad}").status_code == 404


# ----------------------------------------------------------------------------- speed + formatting
def test_every_page_renders_under_one_second(client):
    for url in ("/", "/?acct=S1&range=7d", "/?acct=S3&range=90d", "/p/live", "/research"):
        t = time.perf_counter()
        assert client.get(url).status_code == 200
        assert time.perf_counter() - t < 1.0, url


def test_number_formatting():
    assert money(12345.678) == "$12,345.68" and money(-5) == "−$5.00"
    assert smoney(12.3) == "+$12.30" and smoney(-12.3) == "−$12.30" and smoney(0.001) == "$0.00"
    assert spct(0.0123) == "+1.23%" and spct(-0.0123) == "−1.23%" and pct(-0.05) == "−5.00%"


# ----------------------------------------------------------------------------- downtime alerts
def test_startup_after_downtime_raises_an_urgent_alert(live_home):
    paths = live_home
    rt = Runtime(load_config(paths), paths)
    timeutil.set_now(T0)
    rt.heartbeat()
    timeutil.set_now(T0 + timedelta(hours=40))
    assert rt.startup_check() == pytest.approx(40)
    c = db(paths)
    assert c.execute("SELECT severity FROM risk_events WHERE type = 'heartbeat_missing'").fetchone()[0] == "urgent"
    assert "no heartbeat for 40 h" in c.execute("SELECT subject FROM alerts").fetchone()[0]


def test_check_heartbeat_command_alerts_when_not_running(live_home):
    timeutil.set_now(T0)
    r = CliRunner().invoke(cli, ["check-heartbeat"])
    assert r.exit_code == 1 and "not running" in r.output
    c = db(live_home)
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE subject LIKE '%not running%'").fetchone()[0] == 1


@pytest.fixture
def live_home(home):
    CliRunner().invoke(cli, ["init"])
    home.config_file.write_text(home.config_file.read_text().replace("source: exchange", "source: synthetic"))
    return home
