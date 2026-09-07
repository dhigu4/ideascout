"""One place that configures logging for the whole application.

Log messages go to both the console and a rotating log file, so a problem
that happened during an unattended run can always be found later in
data/app.log.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "ideascout"

_MAX_BYTES = 2_000_000  # ~2 MB per log file
_BACKUP_COUNT = 3  # keep app.log plus 3 rotated copies

_configured = False


def setup_logging(log_path: Path) -> logging.Logger:
    """Configure the ideascout logger. Safe to call more than once.

    Everything goes to the log file only, including full error tracebacks.
    The console instead gets the short, human-readable summaries that
    cli.py prints directly (e.g. "AgentMail check FAILED"), so a
    non-technical user isn't confronted with a wall of traceback text --
    the full detail is always waiting in data/app.log if needed.
    """
    global _configured

    logger = logging.getLogger(LOGGER_NAME)

    if _configured:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
