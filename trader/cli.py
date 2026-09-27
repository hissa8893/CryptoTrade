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
    if os.name != "nt":
        os.chmod(paths.env_file, 0o600)
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


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"❌ {exc}", fg="red", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
