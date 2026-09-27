"""Configuration bounds, paper-only enforcement, secret handling."""

import json
import logging

import pytest
import yaml
from pydantic import ValidationError

from trader.config import (
    AppConfig,
    ConfigError,
    CostsConfig,
    RiskConfig,
    ServerConfig,
    assert_paper_mode,
    load_config,
    load_secrets,
)
from trader.logging_setup import JsonFormatter, register_secrets


def test_defaults_match_spec():
    c = AppConfig()
    assert c.mode == "paper"
    assert c.costs.fee_rate == 0.001
    assert c.costs.slippage_for("BTC/USD") == 0.0005 and c.costs.slippage_for("ETH/USD") == 0.0005
    assert c.costs.slippage_for("SOL/USD") == 0.0015 and c.costs.slippage_for("XRP/USD") == 0.0015
    r = c.risk
    assert (r.risk_per_trade, r.max_position_pct, r.max_portfolio_heat, r.max_positions) == (0.01, 0.25, 0.04, 4)
    assert (r.daily_loss_cap, r.drawdown_breaker, r.drawdown_release) == (0.03, 0.15, 0.10)
    assert (r.losing_streak_limit, r.cooldown_days, r.initial_stop_atr_mult, r.atr_period) == (4, 5, 2.5, 14)
    assert c.server.host == "127.0.0.1"
    assert c.data.exchange == "bitstamp"


def test_example_config_file_is_valid(home):
    import shutil

    shutil.copyfile(home.config_example, home.config_file)
    cfg = load_config(home)
    assert cfg.alerts.channel == "email"  # user's kickoff answer
    assert cfg == AppConfig.model_validate(yaml.safe_load(home.config_example.read_text()))


@pytest.mark.parametrize("mode", ["live", "real", "LIVE", ""])
def test_non_paper_mode_refused(mode):
    with pytest.raises(ValidationError, match="simulation-only"):
        AppConfig(mode=mode)


def test_non_paper_mode_refused_from_file(home):
    home.config_file.write_text("mode: live\n")
    with pytest.raises(ConfigError, match="simulation-only"):
        load_config(home)


def test_assert_paper_mode_defense_in_depth():
    c = AppConfig()
    object.__setattr__(c, "mode", "live")  # bypass validation deliberately
    with pytest.raises(ConfigError):
        assert_paper_mode(c)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"risk_per_trade": 0.5},        # 50% per trade: absurd
        {"risk_per_trade": 0.0},
        {"risk_per_trade": 0.021},      # just over the 2% cap
        {"max_position_pct": 1.5},
        {"max_positions": 0},
        {"max_portfolio_heat": 0.5},
        {"drawdown_breaker": 0.10, "drawdown_release": 0.15},  # release must be below breaker
    ],
)
def test_risk_bounds(kwargs):
    with pytest.raises(ValidationError):
        RiskConfig(**kwargs)


def test_zero_costs_rejected():
    with pytest.raises(ValidationError):
        CostsConfig(fee_rate=0)
    with pytest.raises(ValidationError):
        CostsConfig(slippage_major=0)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "8.8.8.8"])
def test_never_bind_all_interfaces_or_public(host):
    with pytest.raises(ValidationError):
        ServerConfig(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "100.101.102.103", "192.168.1.20"])
def test_allowed_bind_hosts(host):
    assert ServerConfig(host=host).host in (host, "127.0.0.1")


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"risk_per_trad": 0.01}})  # typo
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"data": {"apiKey": "x"}})  # nowhere to put exchange keys


def test_secrets_loaded_from_env_file_and_redacted(home):
    home.env_file.write_text("SMTP_HOST=smtp.example.com\nSMTP_PASSWORD=hunter2-very-secret\nALERT_EMAIL_TO=a@b.c\nALERT_EMAIL_FROM=bot@b.c\nSMTP_PORT=\n")
    s = load_secrets(home)
    assert s.smtp_host == "smtp.example.com" and s.smtp_port == 587
    assert "hunter2" not in repr(s)
    assert s.email_configured()
    register_secrets(s.secret_values())
    rec = logging.makeLogRecord({"msg": "connecting with hunter2-very-secret", "levelname": "INFO", "name": "t"})
    line = JsonFormatter().format(rec)
    assert "hunter2" not in line and "REDACTED" in line
    json.loads(line)
