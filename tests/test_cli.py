"""CLI: init idempotency, secrets file permissions, offline doctor, synthetic fetch."""

import os
import stat

import pytest
from typer.testing import CliRunner

from trader.cli import app

runner = CliRunner()


def test_init_is_idempotent_and_locks_down_secrets(home):
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output
    assert "created config.yaml" in r1.output and "created .env" in r1.output
    assert "Dashboard login token" in r1.output
    cfg_before = home.config_file.read_text()
    home.config_file.write_text(cfg_before + "\n# user edit\n")
    r2 = runner.invoke(app, ["init"])
    assert r2.exit_code == 0 and "nothing to do" in r2.output
    assert home.config_file.read_text().endswith("# user edit\n")  # never overwritten
    if os.name != "nt":
        for p in (home.env_file, home.shutdown_token_file, home.dashboard_token_hash_file):
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_non_paper_config_blocks_every_command(home):
    runner.invoke(app, ["init"])
    home.config_file.write_text("mode: live\n")
    r = runner.invoke(app, ["config", "check"])
    assert r.exit_code == 2
    assert "simulation-only" in r.output


def test_synthetic_fetch_then_offline_doctor(home):
    runner.invoke(app, ["init"])
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # a free port: the real trader may be running on 8765 while tests run
    home.config_file.write_text(home.config_file.read_text().replace("source: exchange", "source: synthetic")
                                .replace("port: 8765", f"port: {port}"))
    assert runner.invoke(app, ["db", "migrate"]).exit_code == 0
    r = runner.invoke(app, ["data", "fetch"])
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC" in r.output
    d = runner.invoke(app, ["doctor", "--offline"])
    assert "✅ Database writable" in d.output
    assert "SYNTHETIC Price history cached" in d.output
    assert d.exit_code == 0, d.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_init_tightens_a_loose_env_and_survives_a_read_only_one(home, monkeypatch):
    assert runner.invoke(app, ["init"]).exit_code == 0
    home.env_file.chmod(0o644)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert stat.S_IMODE(home.env_file.stat().st_mode) == 0o600  # re-tightened
    home.env_file.chmod(0o644)

    def refuse(*a, **k):
        raise PermissionError(30, "Read-only file system")

    monkeypatch.setattr(os, "chmod", refuse)  # like a read-only Docker bind mount
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "could not make .env owner-only" in r.output
