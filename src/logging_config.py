"""Structured logging with secret scrubbing and rotating JSONL + console handlers."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import config

_SECRET_PATTERNS = [
    re.compile(r"(X-Goog-Api-Key|Authorization|api_key|password|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
]


def redact_secrets(text: str) -> str:
    """Scrub API keys, passwords, and bearer tokens before emitting logs."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


class SecretScrubbingFilter(logging.Filter):
    """Logging filter that redacts sensitive substrings from formatted records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            record.args = tuple(
                redact_secrets(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


class JsonLineFormatter(logging.Formatter):
    """Emit structured JSON log lines for observability and audit trails."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        if hasattr(record, "run_id"):
            payload["run_id"] = getattr(record, "run_id")
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: str | None = None,
    log_dir: Path | None = None,
    enable_file_log: bool = True,
) -> logging.Logger:
    """Configure root CRM logger with console and rotating JSONL handlers."""
    log_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logger = logging.getLogger("lead_crm")
    logger.setLevel(log_level)
    logger.propagate = False

    if logger.handlers:
        return logger

    scrub_filter = SecretScrubbingFilter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
    )
    console_handler.addFilter(scrub_filter)
    logger.addHandler(console_handler)

    if enable_file_log:
        target_dir = log_dir or config.LOGS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target_dir / "crm.jsonl",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonLineFormatter())
        file_handler.addFilter(scrub_filter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the `lead_crm` namespace."""
    configure_logging()
    return logging.getLogger(f"lead_crm.{name}")
