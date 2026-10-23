"""Timestamped, redacted, per-run log files."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from .paths import ensure_private_dir, logs_dir
from .redaction import redact

LOGGER_NAME = "frappe_setup"
_current_log: Path | None = None


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        return True


class _Formatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [%(phase)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "phase"):
            record.phase = "-"
        return super().format(record)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def setup_logging(command: str = "run", directory: Path | None = None) -> Path:
    """Create a new log file for this invocation and attach it to the package logger."""
    global _current_log
    directory = ensure_private_dir(directory or logs_dir())
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"frappe-setup-{stamp}-{command}-{os.getpid()}.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_Formatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    _current_log = path
    return path


def current_log_file() -> Path | None:
    return _current_log


def get_logger(phase: str | None = None) -> logging.LoggerAdapter[logging.Logger]:
    return logging.LoggerAdapter(logging.getLogger(LOGGER_NAME), {"phase": phase or "-"})


def list_logs(directory: Path | None = None) -> list[Path]:
    directory = directory or logs_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("frappe-setup-*.log"), key=lambda p: p.stat().st_mtime)
