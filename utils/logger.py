"""
utils/logger.py — Structured logging setup.

Writes JSON-formatted logs to a rotating file plus human-readable output
to console. Import `get_logger` everywhere instead of using the root logger.
"""

import logging
import logging.handlers
import json
from datetime import datetime, timezone
from pathlib import Path

import config


class _JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON for easy parsing."""

    def format(self, record: logging.LogRecord) -> str:
        log = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log.update(record.extra)
        return json.dumps(log)


def _setup_root_logger() -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Console handler: human-readable
    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(console)

    # File handler: JSON, rotating at 10 MB, keep 7 files
    log_file = config.LOG_DIR / "bot.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(_JSONFormatter())
    root.addHandler(file_handler)


_setup_root_logger()


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call once per module: logger = get_logger(__name__)"""
    return logging.getLogger(name)
