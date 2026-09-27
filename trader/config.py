"""Configuration: `config.yaml` (non-secret settings) + `.env` (secrets only).

Every value is validated and bounded so a typo cannot create an absurd position
(e.g. risk per trade must be between 0.1% and 2%). Unknown keys are rejected.
"""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trader.paths import Paths, get_paths

log = logging.getLogger(__name__)

PAPER_ONLY_MESSAGE = (
    "This software is simulation-only. `mode` must be 'paper'. "
    "It has no code path that places real orders and will not start in any other mode."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CostsConfig(_Strict):
    """Trading costs. Always on; zero is rejected here (a zero-cost run exists only
    as an explicit, labelled research sanity check)."""

    fee_rate: float = Field(0.001, gt=0, le=0.01, description="Fee per side (0.001 = 0.10%)")
    slippage_major: float = Field(0.0005, gt=0, le=0.01, description="Slippage for major assets")
    slippage_other: float = Field(0.0015, gt=0, le=0.02, description="Slippage for other assets")
    major_assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])

    def slippage_for(self, symbol: str) -> float:
        base = symbol.split("/")[0].upper()
        return self.slippage_major if base in {a.upper() for a in self.major_assets} else self.slippage_other


class RiskConfig(_Strict):
    regime_filter_enabled: bool = True
    regime_asset: str = "BTC"
    regime_sma: int = Field(200, ge=20, le=400)

    initial_stop_atr_mult: float = Field(2.5, ge=0.5, le=10)
    atr_period: int = Field(14, ge=2, le=100)

    risk_per_trade: float = Field(0.01, ge=0.001, le=0.02, description="Fraction of equity risked per trade")
    max_position_pct: float = Field(0.25, gt=0, le=0.5)
    max_portfolio_heat: float = Field(0.04, ge=0.005, le=0.10)
    max_positions: int = Field(4, ge=1, le=20)

    daily_loss_cap: float = Field(0.03, ge=0.005, le=0.20)
    drawdown_breaker: float = Field(0.15, ge=0.02, le=0.50)
    drawdown_release: float = Field(0.10, ge=0.01, le=0.50)
    # Without this, a breaker that trips while flat can never release (equity cannot recover
    # with no positions), so trading stops forever. After this many days with the breaker on
    # AND no open positions, the peak resets to current equity and the breaker re-arms.
    # 0 = literal rule (may block forever).
    drawdown_rearm_days: int = Field(30, ge=0, le=365)

    losing_streak_limit: int = Field(4, ge=1, le=20)
    cooldown_days: int = Field(5, ge=0, le=60)

    @model_validator(mode="after")
    def _release_below_breaker(self) -> "RiskConfig":
        if self.drawdown_release >= self.drawdown_breaker:
            raise ValueError("drawdown_release must be smaller than drawdown_breaker")
        return self


class S1Config(_Strict):
    enabled: bool = True
    entry_lookback: int = Field(20, ge=5, le=200)
    exit_lookback: int = Field(10, ge=2, le=200)
    atr_period: int = Field(20, ge=2, le=100)
    chandelier_mult: float = Field(3.0, ge=0.5, le=10)


class S2Config(_Strict):
    enabled: bool = True
    atr_period: int = Field(10, ge=2, le=100)
    multiplier: float = Field(3.0, ge=0.5, le=10)
    trend_sma: int = Field(200, ge=20, le=400)


class S3Config(_Strict):
    enabled: bool = True
    short_lookback: int = Field(30, ge=5, le=365)
    long_lookback: int = Field(90, ge=10, le=365)
    trend_sma: int = Field(200, ge=20, le=400)
    target_vol: float = Field(0.40, ge=0.05, le=1.5, description="Annualized vol target for the whole book")
    vol_lookback: int = Field(30, ge=5, le=365)
    rebalance_threshold: float = Field(0.20, ge=0.0, le=1.0)
    annualization: int = Field(365, ge=252, le=366)


class StrategiesConfig(_Strict):
    s1: S1Config = Field(default_factory=S1Config)
    s2: S2Config = Field(default_factory=S2Config)
    s3: S3Config = Field(default_factory=S3Config)


class AccountsConfig(_Strict):
    starting_equity: float = Field(10_000.0, ge=100, le=10_000_000)


# Exchanges whose public OHLC endpoint only returns a capped recent window.
CAPPED_HISTORY_EXCHANGES = {"kraken": 720}


class DataConfig(_Strict):
    source: Literal["exchange", "synthetic"] = "exchange"
    exchange: str = "bitstamp"
    fallback_exchanges: list[str] = Field(default_factory=lambda: ["coinbaseexchange"])
    assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP"], min_length=1, max_length=20)
    quote_preference: list[str] = Field(default_factory=lambda: ["USD", "USDT"], min_length=1)
    timeframe: Literal["1d"] = "1d"
    history_start: str = "2015-01-01"
    max_fill_gap_days: int = Field(3, ge=0, le=10)
    request_retries: int = Field(5, ge=0, le=10)
    retry_base_delay: float = Field(2.0, ge=0.0, le=60)
    request_timeout_seconds: int = Field(30, ge=5, le=120)

    @field_validator("exchange", "fallback_exchanges")
    @classmethod
    def _no_capped(cls, v):
        names = [v] if isinstance(v, str) else v
        for name in names:
            if name.lower() in CAPPED_HISTORY_EXCHANGES:
                raise ValueError(
                    f"{name} caps public OHLC history (~{CAPPED_HISTORY_EXCHANGES[name.lower()]} candles); "
                    "it cannot be used for backtests"
                )
        return v

    @field_validator("assets", "quote_preference")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [x.strip().upper() for x in v]

    @field_validator("history_start")
    @classmethod
    def _date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class SchedulerConfig(_Strict):
    run_hour_utc: int = Field(0, ge=0, le=23)
    run_minute_utc: int = Field(10, ge=0, le=59)
    misfire_grace_seconds: int = Field(6 * 3600, ge=60, le=86_400)
    heartbeat_stale_hours: float = Field(26, ge=1, le=72)


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _no_wildcard(cls, v: str) -> str:
        v = v.strip()
        if v in ("", "0.0.0.0", "::", "[::]", "*"):
            raise ValueError("refusing to bind to all interfaces; use 127.0.0.1 or a specific private IP")
        if v == "localhost":
            return "127.0.0.1"
        ip = ipaddress.ip_address(v)
        if not (ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")):
            raise ValueError("dashboard may only bind to loopback, LAN, or Tailscale (100.64.0.0/10) addresses")
        return v


class AlertsConfig(_Strict):
    channel: Literal["none", "email", "telegram"] = "none"
    daily_summary: bool = True


class LLMConfig(_Strict):
    enabled: bool = False
    model: str = "claude-sonnet-5"
    timeout_seconds: float = Field(30, ge=1, le=120)
    max_bars: int = Field(60, ge=10, le=250)


class AppConfig(_Strict):
    mode: str = "paper"
    costs: CostsConfig = Field(default_factory=CostsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, v: str) -> str:
        if v != "paper":
            raise ValueError(PAPER_ONLY_MESSAGE)
        return v


class Secrets(BaseSettings):
    """Secrets read from `.env` / environment. Never logged; repr is redacted."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False, env_ignore_empty=True)

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True
    alert_email_from: str | None = None
    alert_email_to: str | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    anthropic_api_key: SecretStr | None = None

    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.alert_email_to and (self.alert_email_from or self.smtp_user))

    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def secret_values(self) -> list[str]:
        out = []
        for v in (self.smtp_password, self.telegram_bot_token, self.anthropic_api_key):
            if v is not None and v.get_secret_value():
                out.append(v.get_secret_value())
        return out


class ConfigError(RuntimeError):
    pass


def assert_paper_mode(cfg: AppConfig) -> None:
    """Defense in depth: called at every entry point, independent of validation."""
    if cfg.mode != "paper":
        raise ConfigError(PAPER_ONLY_MESSAGE)


def load_config(paths: Paths | None = None, path: Path | None = None) -> AppConfig:
    paths = paths or get_paths()
    cfg_path = path or paths.config_file
    if not cfg_path.exists():
        log.warning("config file %s not found; using built-in defaults", cfg_path)
        cfg = AppConfig()
    else:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{cfg_path} must contain a YAML mapping")
        try:
            cfg = AppConfig.model_validate(raw)
        except Exception as exc:  # pydantic.ValidationError
            raise ConfigError(f"invalid configuration in {cfg_path}:\n{exc}") from exc
    assert_paper_mode(cfg)
    return cfg


def load_secrets(paths: Paths | None = None) -> Secrets:
    paths = paths or get_paths()
    env_file = paths.env_file if paths.env_file.exists() else None
    return Secrets(_env_file=env_file)  # type: ignore[call-arg]
