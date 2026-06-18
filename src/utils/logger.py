"""
Centralised logging.

A single rotating file handler (``trading.log``) plus a console handler capture
*everything*: TimesFM forecasts, Captain reasoning/decisions and order
execution. ``setup_logging`` is idempotent so it can be called from ``main`` and
safely re-imported by submodules.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_file: str = "trading.log",
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> logging.Logger:
    """Configure root logging once. Returns the root logger."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # Rotating file handler — the durable audit trail.
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Console handler — operator visibility. Force UTF-8 on Windows consoles.
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Tame noisy third-party libraries.
    for noisy in ("httpx", "websockets", "urllib3", "google", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    root.info("Logging initialised -> %s (level=%s)", log_file, level.upper())
    return root


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a named logger (configuring root with defaults if needed)."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
