"""The `trader` command-line interface."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets as pysecrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

from trader import __version__
from trader.config import AppConfig, ConfigError, Secrets, assert_paper_mode, load_config, load_secrets
from trader.logging_setup import register_secrets, setup_logging
from trader.paths import Paths, cli_hint, get_paths

# pretty_exceptions_show_locals=False: a crash must never print local variables, which may hold secrets
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False,
                  help="Paper-trading-only daily crypto agent (SIMULATION ONLY).")
db_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Database commands.")
data_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Market data commands.")
config_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Configuration commands.")
app.add_typer(db_app, name="db")
app.add_typer(data_app, name="data")
app.add_typer(config_app, name="config")

log = logging.getLogger("trader.cli")


@dataclass
class Ctx:
    paths: Paths
    cfg: AppConfig
    secrets: Secrets


def _ctx(require_config: bool = True) -> Ctx:
    paths = get_paths()
    setup_logging(paths.logs if paths.logs.parent.exists() else None)
    try:
        cfg = load_config(paths) if (require_config or paths.config_file.exists()) else AppConfig()
    except ConfigError as exc:
        typer.secho(f"❌ {exc}", fg="red", err=True)
        raise typer.Exit(2)
    assert_paper_mode(cfg)
    sec = load_secrets(paths)
    register_secrets(sec.secret_values())
    from trader.timeutil import fake_now_env

    if fake_now_env() is not None:
        typer.secho(f"⚠️  TRADER_FAKE_NOW is set: clock pinned to {fake_now_env()} (test mode)", fg="yellow", err=True)
    return Ctx(paths, cfg, sec)


# ----------------------------------------------------------------------------- basics
@app.command()
def version() -> None:
    """Print the app version."""
    typer.echo(f"trader {__version__} (paper trading only)")


@app.command()
def init() -> None:
    """Create directories, copy example config/.env (only if missing), generate local tokens."""
    paths = get_paths()
    paths.ensure_dirs()
    created = []
    if not paths.config_file.exists():
        shutil.copyfile(paths.config_example, paths.config_file)
        created.append("config.yaml")
    if not paths.env_file.exists():
        shutil.copyfile(paths.env_example, paths.env_file)
        created.append(".env")
    if os.name != "nt" and paths.env_file.stat().st_mode & 0o077:
        try:
            os.chmod(paths.env_file, 0o600)
        except OSError as exc:  # e.g. a read-only mount in Docker; `doctor` keeps flagging it
            typer.secho(f"⚠️  could not make .env owner-only ({exc.strerror}); fix: chmod 600 .env", err=True)
    if not paths.shutdown_token_file.exists():
        _write_secret_file(paths.shutdown_token_file, pysecrets.token_urlsafe(32))
        created.append("run/shutdown.token")
    new_dashboard_token = None
    if not paths.dashboard_token_hash_file.exists():
        new_dashboard_token = pysecrets.token_urlsafe(24)
        _write_secret_file(paths.dashboard_token_hash_file, hashlib.sha256(new_dashboard_token.encode()).hexdigest())
        created.append("dashboard login token")
    typer.echo("init: " + (", ".join(f"created {c}" for c in created) if created else "nothing to do (already initialised)"))
    if new_dashboard_token:
        typer.echo(
            "Dashboard login token (only needed when viewing from another device; shown ONCE, stored hashed):\n"
            f"    {new_dashboard_token}"
        )


def _write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


@app.command()
def doctor(
    full: bool = typer.Option(False, "--full", help="Also run pip-audit against requirements.txt."),
    send_test_alert: bool = typer.Option(False, "--send-test-alert", help="Send a real test alert."),
    offline: bool = typer.Option(False, "--offline", help="Skip checks that need the internet."),
) -> None:
    """Health checklist (✅/❌)."""
    from trader.doctor import run_doctor

    paths = get_paths()
    setup_logging(paths.logs if paths.logs.exists() else None)
    typer.echo(f"trader doctor — {paths.root}")
    checks = run_doctor(paths, full=full, send_test_alert=send_test_alert, network=not offline)
    for c in checks:
        typer.echo("  " + c.line())
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    typer.echo(f"\n{len(checks) - len(fails) - len(warns)} ok, {len(warns)} warning(s), {len(fails)} failure(s)")
    raise typer.Exit(1 if fails else 0)


# ----------------------------------------------------------------------------- config
@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration (secrets redacted)."""
    import yaml

    ctx = _ctx()
    typer.echo(yaml.safe_dump(ctx.cfg.model_dump(), sort_keys=False))
    s = ctx.secrets
    typer.echo("secrets (.env): " + ", ".join(
        f"{k}={'set' if v else 'unset'}"
        for k, v in {
            "SMTP_HOST": s.smtp_host, "SMTP_PASSWORD": s.smtp_password, "ALERT_EMAIL_TO": s.alert_email_to,
            "TELEGRAM_BOT_TOKEN": s.telegram_bot_token, "ANTHROPIC_API_KEY": s.anthropic_api_key,
        }.items()
    ))


@config_app.command("check")
def config_check() -> None:
    """Validate config.yaml."""
    _ctx()
    typer.echo("✅ config.yaml is valid (mode=paper)")


# ----------------------------------------------------------------------------- db
@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending database migrations."""
    from trader.db import Database

    ctx = _ctx(require_config=False)
    ctx.paths.ensure_dirs()
    db = Database(ctx.paths.db_file)
    applied = db.migrate()
    typer.echo(f"db: schema v{db.schema_version()}" + (f" (applied {applied})" if applied else " (up to date)"))


@db_app.command("status")
def db_status() -> None:
    """Show schema version and row counts."""
    from sqlalchemy import text

    from trader.db import Database

    ctx = _ctx(require_config=False)
    db = Database(ctx.paths.db_file)
    typer.echo(f"db: {ctx.paths.db_file} schema v{db.schema_version()} / latest v{db.latest_version()}")
    with db.read() as c:
        for t in ("runs", "signals", "decisions", "orders", "trades", "positions", "equity_snapshots", "risk_events", "job_runs"):
            try:
                n = c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                typer.echo(f"  {t:<17} {n}")
            except Exception:
                typer.echo(f"  {t:<17} (missing)")


@db_app.command("backup")
def db_backup(keep: int = typer.Option(14, help="How many daily backups to keep.")) -> None:
    """Online backup of the DB to data/backups/ (keeps the last N)."""
    from trader.db import Database

    ctx = _ctx(require_config=False)
    dest = Database(ctx.paths.db_file).backup(ctx.paths.backups, keep=keep)
    typer.echo(f"backup written: {dest}")


# ----------------------------------------------------------------------------- data
@data_app.command("fetch")
def data_fetch(
    asset: Optional[list[str]] = typer.Option(None, "--asset", "-a", help="Only these assets (e.g. -a BTC)."),
    full_refresh: bool = typer.Option(False, "--full-refresh", help="Ignore the cache and re-download everything."),
) -> None:
    """Download/refresh daily candles, validate, and cache to Parquet."""
    from trader.data import DataError, MarketData

    ctx = _ctx()
    ctx.paths.ensure_dirs()
    md = MarketData(ctx.cfg, ctx.paths)
    if md.source == "synthetic":
        typer.secho("⚠️  data.source=synthetic — generating SYNTHETIC prices (not real market data)", fg="yellow")
    try:
        results = md.update([a.upper() for a in asset] if asset else None, full_refresh=full_refresh)
    except DataError as exc:
        typer.secho(f"❌ {exc}", fg="red", err=True)
        raise typer.Exit(1)
    bad = 0
    for r in results:
        rep = r.report
        icon = "✅" if rep.ok and not r.fresh_problem else ("⚠️ " if rep.ok else "❌")
        typer.echo(f"{icon} [{r.source}] {rep.summary()} | fetched {r.fetched}" + (f" | {r.fresh_problem}" if r.fresh_problem else ""))
        for w in rep.warnings:
            typer.echo(f"     warning: {w}")
        for g in rep.gaps:
            typer.echo(f"     gap: {g['missing_days']} day(s) between {g['after']} and {g['before']} → {g['action']}")
        bad += 0 if rep.ok else 1
    raise typer.Exit(1 if bad else 0)


@data_app.command("status")
def data_status() -> None:
    """Show what is cached for each symbol."""
    from trader.data import DataError, MarketData, freshness_problem, history_years

    ctx = _ctx()
    md = MarketData(ctx.cfg, ctx.paths)
    syms = md.symbols()
    if not syms:
        typer.echo(f"no cached data — run: {cli_hint('data fetch')}")
        raise typer.Exit(1)
    typer.echo(f"source: {md.source}" + ("  (SYNTHETIC — not real prices)" if md.source == "synthetic" else ""))
    bad = 0
    for s in syms:
        try:
            df, rep = md.load_checked(s)
        except DataError:
            typer.echo(f"  ❌ {s:<10} not cached")
            bad += 1
            continue
        meta = md.cache.meta(s)
        if not rep.ok:
            typer.echo(f"  ❌ {s:<10} NOT TRADABLE — " + "; ".join(rep.errors))
            bad += 1
            continue
        fresh = freshness_problem(df) or "fresh"
        icon = "✅" if fresh == "fresh" else "⚠️ "
        typer.echo(
            f"  {icon} {s:<10} {len(df):>5} bars  {df.index[0].date()} → {df.index[-1].date()}  "
            f"({history_years(df):.1f}y)  filled={rep.filled_days}  {fresh}  fetched_at={meta.get('fetched_at')}"
        )
        for w in rep.warnings:
            typer.echo(f"       note: {w}")
    raise typer.Exit(1 if bad else 0)


# ----------------------------------------------------------------------------- backtest
verify_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="Correctness proofs.")
app.add_typer(verify_app, name="verify")


def _load_frames(ctx: Ctx, assets: list[str]) -> tuple[dict, list[str]]:
    """Validated frames for the requested assets plus the regime asset. Exits on missing data."""
    from trader.data import DataError, MarketData

    md = MarketData(ctx.cfg, ctx.paths)
    by_asset = {s.split("/")[0]: s for s in md.symbols()}
    wanted = [a.upper() for a in assets]
    need = set(wanted) | ({ctx.cfg.risk.regime_asset} if ctx.cfg.risk.regime_filter_enabled else set())
    frames = {}
    for a in sorted(need):
        sym = by_asset.get(a)
        if sym is None:
            typer.secho(f"❌ no data for {a}; run: {cli_hint('data fetch')}", fg="red", err=True)
            raise typer.Exit(1)
        try:
            frames[sym] = md.load(sym)
        except DataError as exc:
            typer.secho(f"❌ {exc}", fg="red", err=True)
            raise typer.Exit(1)
    return frames, [by_asset[a] for a in wanted]


@app.command()
def backtest(
    strategy: list[str] = typer.Option(["S1"], "--strategy", "-s", help="Strategy (repeat for a combined portfolio)."),
    asset: list[str] = typer.Option(["BTC"], "--asset", "-a", help="Asset(s) to trade."),
    start: Optional[str] = typer.Option(None, help="First date (YYYY-MM-DD); default: first date indicators are valid."),
    end: Optional[str] = typer.Option(None, help="Last date (YYYY-MM-DD)."),
    zero_costs: bool = typer.Option(False, "--zero-costs", help="SANITY CHECK ONLY: disable fees and slippage."),
    save: bool = typer.Option(True, "--save/--no-save", help="Write the run to the database."),
) -> None:
    """Run an event-driven backtest and write an HTML report to reports/."""
    from trader.backtest import persist_backtest, run_backtest
    from trader.broker import CostModel
    from trader.data import MarketData
    from trader.db import Database
    from trader.reports import write_report

    ctx = _ctx()
    frames, symbols = _load_frames(ctx, asset)
    source = MarketData(ctx.cfg, ctx.paths).source
    costs = CostModel.zero() if zero_costs else CostModel.from_config(ctx.cfg.costs)
    res = run_backtest(ctx.cfg, frames, [x.upper() for x in strategy], symbols=symbols, data_source=source,
                       start=start, end=end, costs=costs)
    if save:
        db = Database(ctx.paths.db_file)
        db.migrate()
        persist_backtest(db, ctx.paths, res)
    path = write_report(res, ctx.paths.reports)
    m = res.metrics
    if source == "synthetic":
        typer.secho("⚠️  SYNTHETIC DATA — not real prices; results say nothing about real markets.", fg="yellow")
    if zero_costs:
        typer.secho("⚠️  ZERO-COST sanity run (fees and slippage disabled).", fg="yellow")
    typer.echo(f"{res.label}: {m.start} → {m.end}")
    for f in res.flags:
        typer.secho(f"🚩 {f}", fg="red")
    bm = res.benchmarks[0].metrics
    typer.echo(f"  total return {m.total_return * 100:+.2f}%  (B&H {bm.total_return * 100:+.2f}%)")
    typer.echo(f"  CAGR {m.cagr * 100:+.2f}%  max DD -{m.max_drawdown * 100:.2f}%  Sharpe "
               f"{m.sharpe if m.sharpe is None else round(m.sharpe, 2)}  trades {m.trades}  win rate "
               f"{'-' if m.win_rate is None else f'{m.win_rate * 100:.1f}%'}  fees ${m.fees:,.2f}")
    typer.echo(f"  report: {path}" + (f"  (run id {res.run_id})" if res.run_id else ""))


@app.command()
def research(
    strategy: list[str] = typer.Option(["S1", "S2", "S3"], "--strategy", "-s"),
    asset: list[str] = typer.Option(["BTC", "ETH", "SOL", "XRP"], "--asset", "-a"),
    heatmaps: bool = typer.Option(True, "--heatmaps/--no-heatmaps", help="Parameter-sensitivity grids (slower)."),
) -> None:
    """Walk-forward (out-of-sample) research for each strategy and the combined portfolio."""
    import time

    from trader.data import MarketData
    from trader.research import run_research
    from trader.research_report import write_research_report

    ctx = _ctx()
    frames, symbols = _load_frames(ctx, asset)
    source = MarketData(ctx.cfg, ctx.paths).source
    t0 = time.time()
    res = run_research(ctx.cfg, frames, [x.upper() for x in strategy], symbols, data_source=source,
                       sensitivity_grid=heatmaps, progress=lambda m: typer.echo(f"  … {m}"))
    path = write_research_report(res, ctx.paths.reports)
    if source == "synthetic":
        typer.secho("⚠️  SYNTHETIC DATA — not real prices; do not tune anything to these numbers.", fg="yellow")
    typer.echo(f"\nOut-of-sample results ({time.time() - t0:.0f}s):")
    rows = [(n, s.wf.oos_metrics, s.wf.flags) for n, s in res.strategies.items()]
    if res.combined:
        rows.append(("Combined", res.combined.metrics, res.combined.flags))
    for n, m in res.defaults.items():
        rows.append((("Comb" if "+" in n else n) + " def", m, []))
    rows.append(("Buy&Hold", res.benchmark_metrics, []))
    typer.echo(f"  {'':9} {'return':>9} {'CAGR':>8} {'max DD':>8} {'Sharpe':>7} {'trades':>7}")
    for n, m, flags in rows:
        sh = "—" if m.sharpe is None else f"{m.sharpe:.2f}"
        typer.echo(f"  {n:9} {m.total_return * 100:+8.1f}% {m.cagr * 100:+7.1f}% {-m.max_drawdown * 100:7.1f}% {sh:>7} {m.trades:7d}")
        for f in flags:
            typer.secho(f"      🚩 {f}", fg="red")
    zc = res.zero_cost_check
    typer.echo(f"  cost sanity: zero-cost {zc['zero_costs']:,.2f} vs with-cost {zc['with_costs']:,.2f} -> "
               + ("OK" if zc["ok"] else "PROBLEM"))
    typer.echo(f"  report: {path}")


@verify_app.command("lookahead")
def verify_lookahead(
    strategy: list[str] = typer.Option(["S1"], "--strategy", "-s"),
    asset: list[str] = typer.Option(["BTC"], "--asset", "-a"),
    samples: int = typer.Option(250, help="How many days to re-run on truncated data (>= 200)."),
) -> None:
    """Prove decisions at day t do not depend on data after t."""
    import time

    from trader.lookahead import lookahead_proof

    ctx = _ctx()
    frames, symbols = _load_frames(ctx, asset)
    t0 = time.time()
    rep = lookahead_proof(ctx.cfg, frames, [x.upper() for x in strategy], symbols=symbols, samples=samples)
    typer.echo(f"look-ahead proof: {rep.dates_checked} days re-run on truncated data "
               f"({rep.dates_with_signals} with signals; {rep.signals_compared} signals, {rep.orders_compared} orders "
               f"compared) in {time.time() - t0:.1f}s")
    if rep.passed:
        typer.secho("✅ PASS — every decision and the full engine state matched the full-history run exactly", fg="green")
        return
    typer.secho(f"❌ FAIL — {len(rep.mismatches)} mismatching day(s), first: {rep.mismatches[0]['date']} "
                f"({rep.mismatches[0]['kind']})", fg="red")
    raise typer.Exit(1)


# ----------------------------------------------------------------------------- runtime / control
@app.command()
def serve() -> None:
    """Run the trader in the foreground (scheduler + dashboard in one process). Use `start` for background."""
    from trader.server import serve as _serve

    ctx = _ctx()
    ctx.paths.ensure_dirs()
    _serve(ctx.cfg, ctx.paths)


@app.command()
def start(no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the dashboard."),
          wait: float = typer.Option(60.0, help="Seconds to wait for the server to answer.")) -> None:
    """Start the trader in the background (does nothing if it is already running)."""
    from trader import control

    ctx = _ctx()
    ctx.paths.ensure_dirs()
    raise typer.Exit(control.start(ctx.cfg, ctx.paths, open_browser=not no_browser, wait=wait))


@app.command()
def stop(timeout: float = typer.Option(30.0, help="Seconds to wait for a graceful stop before force-killing.")) -> None:
    """Stop the trader gracefully (finishes any in-progress day first)."""
    from trader import control

    ctx = _ctx()
    raise typer.Exit(control.stop(ctx.cfg, ctx.paths, timeout=timeout))


@app.command()
def status() -> None:
    """One line: running/stopped, PID, uptime, last successful daily run, next run."""
    from trader import control

    ctx = _ctx()
    raise typer.Exit(control.status(ctx.cfg, ctx.paths))


@app.command()
def restart(no_browser: bool = typer.Option(False, "--no-browser")) -> None:
    """Stop, then start."""
    from trader import control

    ctx = _ctx()
    raise typer.Exit(control.restart(ctx.cfg, ctx.paths, open_browser=not no_browser))


@app.command()
def logs(lines: int = typer.Option(40, "--lines", "-n"), follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    """Show the most recent log lines (-f to keep following)."""
    from trader import control

    ctx = _ctx()
    raise typer.Exit(control.logs(ctx.paths, lines=lines, follow=follow))


service_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                          help="Start automatically at login (launchd / systemd / Task Scheduler).")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install() -> None:
    """Start the trader at every login (and restart it after a crash) + an hourly heartbeat check."""
    from trader import service

    ctx = _ctx()
    try:
        msgs = service.install(service.make_ctx(ctx.paths))
    except ValueError as exc:
        typer.secho(f"❌ {exc}", fg="red", err=True)
        raise typer.Exit(1)
    for m in msgs:
        typer.echo(m)
    raise typer.Exit(1 if any(m.startswith("❌") for m in msgs) else 0)


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Remove the auto-start service (the trader keeps running until you stop it)."""
    from trader import service

    ctx = _ctx()
    for m in service.uninstall(service.make_ctx(ctx.paths)):
        typer.echo(m)


@service_app.command("status")
def service_status() -> None:
    """Is the auto-start service installed?"""
    from trader import service

    ctx = _ctx()
    ok = service.installed(service.make_ctx(ctx.paths))
    typer.echo(f"auto-start service ({service.platform_kind()}): {'installed' if ok else 'not installed'}")
    raise typer.Exit(0 if ok else 3)


@app.command("uninstall")
def uninstall_cmd(
    delete_data: Optional[bool] = typer.Option(None, "--delete-data/--keep-data",
                                               help="Delete data/ (trade history, database, price cache) without asking."),
) -> None:
    """Stop the trader, remove the auto-start service, and ask before deleting data/.
    (The uninstall script then removes .venv.)"""
    from trader import control, service

    paths = get_paths()
    try:  # uninstalling must work even with a broken config (it trades nothing; only the port is used)
        cfg = load_config(paths)
    except ConfigError as exc:
        typer.secho(f"⚠️  config.yaml unusable ({str(exc)[:80]}); using defaults to stop the trader", err=True)
        cfg = AppConfig()
    # remove the auto-start service FIRST, so nothing restarts the trader after we stop it
    for m in service.uninstall(service.make_ctx(paths)):
        typer.echo(m)
    control.stop(cfg, paths)
    shutil.rmtree(paths.run, ignore_errors=True)  # runtime state only: PID file, lock, private stop token
    if paths.data.exists():
        if delete_data is None:
            delete_data = typer.confirm(
                f"Delete {paths.data}? It holds your paper-trading history, the database and its backups. "
                "This cannot be undone", default=False)
        if delete_data:
            shutil.rmtree(paths.data)
            typer.echo(f"deleted {paths.data}")
        else:
            typer.echo(f"kept {paths.data} (your trade history). Delete it yourself later if you want.")
    typer.echo("Also left in place: config.yaml, .env, logs/ and reports/ (delete them yourself if you want).")


llm_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                      help="Optional AI analyst (live paper trading only; can only approve, shrink or veto).")
app.add_typer(llm_app, name="llm")


def _sample_ai_payload(ctx: Ctx) -> dict:
    """A made-up BTC entry built from the latest cached candles, shaped like a real review."""
    from trader import indicators as ind
    from trader.data import MarketData
    from trader.llm import STRATEGY_RULES, clean

    md = MarketData(ctx.cfg, ctx.paths)
    btc = next((s for s in md.symbols() if s.startswith(ctx.cfg.risk.regime_asset + "/")), None)
    df = md.load(btc) if btc else None
    if df is None or df.empty:
        raise typer.BadParameter("no cached price data yet; run: " + cli_hint("data fetch"))
    atr = float(ind.atr(df["high"], df["low"], df["close"], ctx.cfg.risk.atr_period).iloc[-1])
    px = float(df["close"].iloc[-1])
    eq = ctx.cfg.accounts.starting_equity
    dist = ctx.cfg.risk.initial_stop_atr_mult * atr
    qty = min(eq * ctx.cfg.risk.risk_per_trade / dist, eq * ctx.cfg.risk.max_position_pct / px)
    tail = df.tail(ctx.cfg.llm.max_bars)
    rows = [[d.date().isoformat(), r.open, r.high, r.low, r.close, r.volume] for d, r in tail.iterrows()]
    return clean({
        "decision_day": rows[-1][0], "symbol": btc, "strategy": "S1", "strategy_rules": STRATEGY_RULES["S1"],
        "note": "CONNECTION TEST: a made-up proposal; nothing will be traded",
        "proposal": {"side": "buy", "fills_at": "open of the next day", "qty": qty, "reference_price_close": px,
                     "stop_price": px - dist, "stop_distance": dist, "position_pct_of_equity": qty * px / eq,
                     "loss_if_stopped_pct_of_equity": qty * dist / eq},
        "atr": {"period": ctx.cfg.risk.atr_period, "value": atr},
        "daily_bars": {"columns": ["date", "open", "high", "low", "close", "volume"], "rows": rows},
        "portfolio": {"equity": eq, "cash": eq, "open_positions": []},
    })


@llm_app.command("test")
def llm_test() -> None:
    """Send ONE sample review (a made-up BTC entry) to check the key, model and cost. Nothing is traded or saved."""
    from trader.llm import Analyst

    ctx = _ctx()
    key = ctx.secrets.anthropic_api_key.get_secret_value() if ctx.secrets.anthropic_api_key else None
    typer.echo(f"asking {ctx.cfg.llm.model} about a made-up BTC entry (timeout {ctx.cfg.llm.timeout_seconds:.0f} s)...")
    r = Analyst(ctx.cfg.llm, key).review(_sample_ai_payload(ctx))
    if r.status != "ok":
        typer.secho(f"❌ no usable answer: {r.fallback_reason} - {r.error or ''}", fg="red")
        typer.echo("   In live trading this would fall back to the rule-based decision.")
        raise typer.Exit(1)
    typer.secho(f"✅ {r.decision} (size x{r.multiplier:.2f}, confidence {r.confidence:.2f})", fg="green")
    for reason in r.reasons:
        typer.echo(f"   - {reason}")
    cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "unknown"
    typer.echo(f"   answered by {r.served_model} in {r.latency_ms / 1000:.1f} s; tokens {r.input_tokens:,} in / "
               f"{r.output_tokens:,} out; cost {cost}")
    if not ctx.cfg.llm.enabled:
        typer.echo("   The analyst is still OFF for trading; set llm.enabled: true in config.yaml, then restart.")


@llm_app.command("report")
def llm_report() -> None:
    """Is the AI helping? AI-filtered account vs its rules-only shadow twin, verdicts, cost, and what the numbers can tell."""
    from trader.ai_report import ai_summary, summary_lines
    from trader.data import MarketData
    from trader.db import Database

    ctx = _ctx()
    s = ai_summary(Database(ctx.paths.db_file), MarketData(ctx.cfg, ctx.paths).source)
    if s is None:
        typer.echo("The AI analyst has not run yet. Set llm.enabled: true in config.yaml, put ANTHROPIC_API_KEY in .env, "
                   "check with `" + cli_hint("llm test") + "`, then restart the trader.")
        raise typer.Exit(0)
    typer.echo("\n".join(summary_lines(s)))


@app.command("check-heartbeat")
def check_heartbeat() -> None:
    """Hourly job installed by `service install`: urgent alert (once a day) after 26 h without a heartbeat."""
    from trader.control import running_pid
    from trader.runtime import Runtime

    ctx = _ctx()
    ok, msg = Runtime(ctx.cfg, ctx.paths).check_heartbeat(running=running_pid(ctx.paths) is not None)
    typer.echo(("✅ " if ok else "❌ ") + msg)
    raise typer.Exit(0 if ok else 1)


@app.command("run-once")
def run_once(force: bool = typer.Option(False, "--force", help="Run even though the background trader is running.")) -> None:
    """Process every closed day that has not been processed yet, now, in the foreground."""
    from trader.control import running_pid
    from trader.runtime import Runtime

    ctx = _ctx()
    ctx.paths.ensure_dirs()
    pid = running_pid(ctx.paths)
    if pid and not force:
        typer.echo(f"the trader is running (PID {pid}) and processes days itself; use --force to run anyway")
        raise typer.Exit(1)
    rt = Runtime(ctx.cfg, ctx.paths)
    results = rt.catch_up("manual")
    if not results:
        typer.echo("nothing to do: every closed day is already processed")
        return
    for r in results:
        icon = {"ok": "✅", "skipped": "⚠️ ", "failed": "❌"}[r.status]
        typer.echo(f"{icon} {r.bar_date} {r.status}" + (f": {r.error}" if r.error else ""))
        for key, v in r.accounts.items():
            typer.echo(f"     {key.split(':')[-1]:<10} equity ${v['equity']:,.2f}  positions {v['positions']}  "
                       f"trades closed today {v['trades_closed']}")
    raise typer.Exit(0 if all(r.status == "ok" for r in results) else 1)


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"❌ {exc}", fg="red", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
