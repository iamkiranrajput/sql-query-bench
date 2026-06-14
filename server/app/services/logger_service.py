"""
Logger Service

Centralized logging configuration with colored console output, pretty formatting,
and automatic file logging. All loggers write to both console (colored) and
a rotating log file at server/logs/app.log.
"""

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Optional

import colorlog

# ── Log directory & file path ──────────────────────────────────────────
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "app.log"

# ── Internal-IP redaction filter (Phase 3, item 17) ───────────────────
# Mask RFC-1918 10.x.x.x addresses in records that hit the log file so
# rotated logs + any SIEM forwarder never carry internal/private IPs in the
# clear. Console output is intentionally left alone — devs need real IPs
# while debugging locally. Word boundaries prevent matching inside larger
# numbers like build IDs.
_INTERNAL_IP_RE = re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


class _InternalIPRedactionFilter(logging.Filter):
    """Replace 10.x.x.x IP literals with `10.x.x.x` in log records.

    Honours the `REDACT_INTERNAL_IPS` setting (default True). Settings is
    imported lazily so the logger module can be used before the config
    layer is wired up (e.g. during early bootstrap).
    """

    _enabled: Optional[bool] = None

    @classmethod
    def _is_enabled(cls) -> bool:
        if cls._enabled is not None:
            return cls._enabled
        try:
            from app.config.settings import settings  # local import: avoid cycles
            cls._enabled = bool(getattr(settings, "redact_internal_ips", True))
        except Exception:
            # If settings can't load (very early startup), default to ON —
            # better to over-redact than to leak an internal IP into a log file.
            cls._enabled = True
        return cls._enabled

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._is_enabled():
            return True
        try:
            # Render lazily then scrub. `record.msg` may be a non-string
            # (e.g. an exception). Only touch strings to avoid surprises.
            if isinstance(record.msg, str) and "10." in record.msg:
                record.msg = _INTERNAL_IP_RE.sub("10.x.x.x", record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    new_args = []
                    for arg in record.args:
                        if isinstance(arg, str) and "10." in arg:
                            new_args.append(_INTERNAL_IP_RE.sub("10.x.x.x", arg))
                        else:
                            new_args.append(arg)
                    record.args = tuple(new_args)
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (_INTERNAL_IP_RE.sub("10.x.x.x", v) if isinstance(v, str) and "10." in v else v)
                        for k, v in record.args.items()
                    }
        except Exception:
            # Never let logging itself raise.
            return True
        return True


# ── Shared file handler (singleton — added to every logger) ───────────
_file_handler: Optional[logging.Handler] = None


def _get_file_handler() -> logging.Handler:
    """Return (and lazily create) a single RotatingFileHandler for all loggers."""
    global _file_handler
    if _file_handler is None:
        _file_handler = logging.handlers.RotatingFileHandler(
            str(_LOG_FILE),
            maxBytes=10 * 1024 * 1024,   # 10 MB per file
            backupCount=5,                # keep 5 rotated copies
            encoding="utf-8",
        )
        _file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-36s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _file_handler.setFormatter(file_fmt)
        # Scrub internal IPs from anything that lands in app.log.
        _file_handler.addFilter(_InternalIPRedactionFilter())
    return _file_handler


def setup_logger(
    name: str,
    level: int = logging.DEBUG,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """
    Set up a logger with colored console output and automatic file logging.

    Every logger writes to:
      • stdout  — colored via colorlog
      • server/logs/app.log — plain text, rotating 10 MB × 5

    Args:
        name: Logger name (usually __name__)
        level: Logging level (default: DEBUG)
        log_file: Optional *extra* file to log to (legacy param kept for compat)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # ── Console handler (colored) ──────────────────────────────────────
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    console_format = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s%(reset)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "red,bg_white",
        },
        secondary_log_colors={},
        style="%",
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # ── Shared rotating file handler ───────────────────────────────────
    logger.addHandler(_get_file_handler())

    # ── Optional extra file handler (legacy) ───────────────────────────
    if log_file:
        extra_fh = logging.FileHandler(log_file, encoding="utf-8")
        extra_fh.setLevel(level)
        extra_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-36s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        extra_fh.setFormatter(extra_fmt)
        logger.addHandler(extra_fh)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


def get_logger(name: str) -> logging.Logger:
    """Get or create a logger by name."""
    return setup_logger(name)
