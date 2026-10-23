"""Structured logging with automatic redaction of sensitive data."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_URL_RE = re.compile(r"(https?://[^\s'\"<>?#]+)(\?[^\s'\"<>]*)?(#[^\s'\"<>]*)?")
_SECRET_RE = re.compile(
    r"(?i)(cookie|authorization|password|passwd|token|api[_-]?key|secret|sid)\s*[=:]\s*\S+"
)


def redact(text: str) -> str:
    """Strip URL query strings/fragments and obvious secrets from a log line."""
    text = _URL_RE.sub(lambda m: m.group(1), text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the formatted message in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # never let logging break the app
            pass
        return True


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Configure the root 'uvs' logger with a rotating file and console handler."""
    logger = logging.getLogger("uvs")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        logger.addHandler(fh)
    except OSError:
        pass  # logging to file is best-effort
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(RedactingFilter())
    logger.addHandler(ch)
    logger.propagate = False
    return logger
