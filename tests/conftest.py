import shutil
from pathlib import Path

import pytest

from trader import timeutil
from trader.config import AppConfig
from trader.paths import Paths

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_clock():
    timeutil.set_now(None)
    yield
    timeutil.set_now(None)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Paths:
    """An isolated TRADER_HOME with example config/env copied in."""
    monkeypatch.setenv("TRADER_HOME", str(tmp_path))
    monkeypatch.delenv("TRADER_FAKE_NOW", raising=False)
    for name in ("config.example.yaml", ".env.example", "requirements.txt"):
        shutil.copyfile(REPO / name, tmp_path / name)
    paths = Paths(tmp_path)
    paths.ensure_dirs()
    return paths


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()
