"""Database: migrations, WAL, constraints, atomic transactions, backups."""

from datetime import datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy import text

from trader.db import Database, available_migrations
from trader.timeutil import UTC

TABLES = {
    "runs", "signals", "decisions", "orders", "trades", "positions", "equity_snapshots",
    "risk_events", "job_runs", "run_state", "kv", "schema_version",
}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.dispose()


def _run(conn, key="bt:1"):
    conn.execute(text(
        "INSERT INTO runs (run_key, mode, strategy, created_at, app_version, starting_equity) "
        "VALUES (:k, 'backtest', 'S1', '2026-01-01T00:00:00+00:00', '0.1.0', 10000)"
    ), {"k": key})
    return conn.execute(text("SELECT id FROM runs WHERE run_key=:k"), {"k": key}).scalar()


def test_migrations_create_schema_and_are_idempotent(db):
    assert db.schema_version() == db.latest_version() == available_migrations()[-1].version
    assert db.migrate() == []  # second run: nothing to apply
    with db.read() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert TABLES <= names


def test_wal_and_foreign_keys_enabled(db):
    with db.read() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_transaction_rolls_back_completely_on_error(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            _run(c)
            c.execute(text(
                "INSERT INTO job_runs (bar_date, started_at, status) VALUES ('2026-01-01', '2026-01-02T00:10:00+00:00', 'running')"
            ))
            raise RuntimeError("simulated crash mid-run")
    with db.read() as c:
        assert c.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM job_runs")).scalar() == 0


def test_unique_constraints_prevent_duplicates(db):
    with db.tx() as c:
        run_id = _run(c)
        ins = text(
            "INSERT INTO signals (run_id, bar_date, symbol, strategy, signal, created_at) "
            "VALUES (:r, '2026-01-01', 'BTC/USD', 'S1', 'enter', '2026-01-02T00:10:00+00:00')"
        )
        c.execute(ins, {"r": run_id})
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db.tx() as c:
            c.execute(ins, {"r": run_id})
    with db.tx() as c:
        c.execute(text("INSERT INTO job_runs (bar_date, started_at, status) VALUES ('2026-01-01', 'x', 'ok')"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db.tx() as c:
            c.execute(text("INSERT INTO job_runs (bar_date, started_at, status) VALUES ('2026-01-01', 'y', 'ok')"))


def test_check_constraints(db):
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db.tx() as c:
            c.execute(text(
                "INSERT INTO runs (run_key, mode, strategy, created_at, app_version, starting_equity) "
                "VALUES ('x', 'live', 'S1', 'now', '0', 1)"  # 'live' mode does not exist
            ))


def test_kv_roundtrip(db):
    db.set_kv("heartbeat", "a")
    db.set_kv("heartbeat", "b")
    assert db.get_kv("heartbeat") == "b"
    assert db.get_kv("missing") is None


def test_backup_uses_online_api_and_keeps_last_14(db, tmp_path):
    with db.tx() as c:
        _run(c)
    dest_dir = tmp_path / "backups"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(20):
        db.backup(dest_dir, keep=14, when=start + timedelta(days=i))
    files = sorted(p.name for p in dest_dir.glob("trader-*.db"))
    assert len(files) == 14
    assert files[0] == "trader-20260107.db" and files[-1] == "trader-20260120.db"
    restored = Database(dest_dir / files[-1])
    with restored.read() as c:
        assert c.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 1
    restored.dispose()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import trader.db as dbmod

    bad_dir = tmp_path / "migs"
    bad_dir.mkdir()
    (bad_dir / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (bad_dir / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", bad_dir)
    d = Database(tmp_path / "m.db")
    with pytest.raises(Exception):
        d.migrate()
    assert d.schema_version() == 1
    with d.read() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "a" in names and "b" not in names  # 0002 fully rolled back
    d.dispose()
