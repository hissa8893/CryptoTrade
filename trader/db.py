"""SQLite persistence: WAL mode, explicit transactions, file-based migrations, online backups."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from trader.paths import PACKAGE_DIR
from trader.timeutil import now_iso, now_utc

log = logging.getLogger(__name__)

MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def available_migrations() -> list[Migration]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise RuntimeError(f"bad migration filename: {p.name}")
        out.append(Migration(int(m.group(1)), m.group(2), p))
    versions = [m.version for m in out]
    if versions != sorted(set(versions)):
        raise RuntimeError("duplicate migration versions")
    return out


def _set_pragmas(dbapi_conn) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"timeout": 30, "check_same_thread": False},
        )

        # pysqlite's implicit transaction handling is unreliable (it defers BEGIN and
        # auto-commits before DDL). Take control: autocommit at the driver level and
        # emit our own BEGIN / BEGIN IMMEDIATE (SQLAlchemy's documented recipe).
        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None
            _set_pragmas(dbapi_conn)

        @event.listens_for(self.engine, "begin")
        def _on_begin(conn: Connection):
            if conn.get_execution_options().get("sqlite_immediate"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.exec_driver_sql("BEGIN")

    # -- transactions -------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """A write transaction (BEGIN IMMEDIATE). Commits on success, rolls back on error."""
        with self.engine.connect() as conn:
            conn.execution_options(sqlite_immediate=True)
            with conn.begin():
                yield conn

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    def dispose(self) -> None:
        self.engine.dispose()

    # -- migrations ---------------------------------------------------------------
    def _raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        _set_pragmas(conn)
        return conn

    def schema_version(self) -> int:
        conn = self._raw()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def migrate(self) -> list[int]:
        """Apply pending migrations, each atomically (SQLite DDL is transactional)."""
        applied: list[int] = []
        current = self.schema_version()
        conn = self._raw()
        try:
            for mig in available_migrations():
                if mig.version <= current:
                    continue
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{mig.sql}\n"
                    "INSERT INTO schema_version (version, name, applied_at) "
                    f"VALUES ({mig.version}, '{mig.name}', '{now_iso()}');\n"
                    "COMMIT;"
                )
                try:
                    conn.executescript(script)
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    log.exception("migration %04d_%s failed; rolled back", mig.version, mig.name)
                    raise
                log.info("applied migration %04d_%s", mig.version, mig.name)
                applied.append(mig.version)
        finally:
            conn.close()
        return applied

    def latest_version(self) -> int:
        migs = available_migrations()
        return migs[-1].version if migs else 0

    # -- key/value ----------------------------------------------------------------
    def set_kv(self, key: str, value: str, conn: Connection | None = None) -> None:
        stmt = text(
            "INSERT INTO kv (key, value, updated_at) VALUES (:k, :v, :t) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        )
        params = {"k": key, "v": value, "t": now_iso()}
        if conn is not None:
            conn.execute(stmt, params)
        else:
            with self.tx() as c:
                c.execute(stmt, params)

    def get_kv(self, key: str) -> str | None:
        with self.read() as c:
            row = c.execute(text("SELECT value FROM kv WHERE key = :k"), {"k": key}).fetchone()
        return row[0] if row else None

    # -- backups ------------------------------------------------------------------
    def backup(self, dest_dir: Path, keep: int = 14, when: datetime | None = None) -> Path:
        """Copy the live DB with SQLite's online backup API (safe while in use)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = (when or now_utc()).strftime("%Y%m%d")
        dest = dest_dir / f"trader-{stamp}.db"
        tmp = dest.with_suffix(".db.tmp")
        src = sqlite3.connect(self.path, timeout=30)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dest)
        backups = sorted(dest_dir.glob("trader-*.db"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return dest


def git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
