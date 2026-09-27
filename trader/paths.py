"""Filesystem layout. Everything lives under one project root (TRADER_HOME)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def default_root() -> Path:
    env = os.environ.get("TRADER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return PACKAGE_DIR.parent


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config.yaml"

    @property
    def config_example(self) -> Path:
        return self.root / "config.example.yaml"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def env_example(self) -> Path:
        return self.root / ".env.example"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def db_file(self) -> Path:
        return self.data / "trader.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def pid_file(self) -> Path:
        return self.run / "trader.pid"

    @property
    def shutdown_token_file(self) -> Path:
        return self.run / "shutdown.token"

    @property
    def dashboard_token_hash_file(self) -> Path:
        return self.data / "dashboard_token.sha256"

    def ensure_dirs(self) -> None:
        for d in (self.data, self.cache, self.backups, self.logs, self.run, self.reports):
            d.mkdir(parents=True, exist_ok=True)


def get_paths(root: Path | None = None) -> Paths:
    return Paths(root=(root or default_root()))
