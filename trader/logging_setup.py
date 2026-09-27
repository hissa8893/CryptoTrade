"""Structured JSON logs in logs/, rotated daily at UTC midnight. Secrets are redacted."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_REDACT: set[str] = set()


def register_secrets(values: list[str]) -> None:
    """Values that must never appear in any log line."""
    for v in values:
        if v and len(v) >= 4:
            _REDACT.add(v)


def _redact(text: str) -> str:
    for secret in _REDACT:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _redact(json.dumps(payload, default=str))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


_configured = False


def setup_logging(logs_dir: Path | None, *, level: str = "INFO", console: bool = True) -> None:
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            logs_dir / "trader.log", when="midnight", utc=True, backupCount=30, encoding="utf-8"
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(ConsoleFormatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(ch)
    for noisy in ("urllib3", "ccxt", "apscheduler.executors", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
