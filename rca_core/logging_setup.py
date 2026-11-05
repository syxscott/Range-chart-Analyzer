"""One rotating log file, with credential redaction on every record.

WHY THIS EXISTS
===============
ARCH-REVIEW-2026-11-02 §5.1: the repository had six ``getLogger()`` callers
and ZERO handlers. Every ``log.info(...)`` -- including "numpy unavailable,
falling back to single pass" in ``rca_core/matrix.py`` -- went to Python's
last-resort handler at WARNING level, i.e. to stderr. The packaged app is a
**windowed** exe with no console, so its stderr goes nowhere. A user hitting
a silent degradation had no log, and therefore no way to report it.

A log is not telemetry: it stays on the user's machine, is never uploaded,
and the project's "local-first" promise is unchanged. See SECURITY.md.

SECURITY CONSTRAINT: ``rca_core/redact.py`` was written for ERROR BODIES, not
arbitrary log lines, but routing every record through ``redact_error_body``
is strictly better than nothing. The filter is mandatory, not optional --
a provider URL containing ``?api_key=...`` must never land in rca.log.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Exposed so the issue template and ``main.py doctor`` can name the file.
LOG_DIR = Path(os.path.expanduser("~")) / ".range_chart_analyzer"
LOG_PATH = LOG_DIR / "rca.log"

_HANDLER_NAME = "rca_rotating_file"


class _RedactingFilter(logging.Filter):
    """Scrub key-like tokens before a record reaches any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from .redact import redact_error_body
            # getMessage() applies %-args. Replacing msg with the fully
            # rendered text keeps the rest of logging's formatting intact.
            record.msg = redact_error_body(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - a logging filter must never raise
            pass
        return True


def setup_logging(level: int = logging.INFO) -> Path | None:
    """Install the file handler once. Returns the log path, or None on failure.

    Never raises: a read-only home directory, a full disk, or a locked file
    must not stop the application from starting. That is the whole reason to
    wrap this in try/except rather than letting logging configure at import.
    """
    root = logging.getLogger()
    if any(getattr(h, "name", "") == _HANDLER_NAME for h in root.handlers):
        return LOG_PATH
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.name = _HANDLER_NAME
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(_RedactingFilter())
        root.addHandler(handler)
        root.setLevel(level)
        return LOG_PATH
    except Exception:  # noqa: BLE001 - logging is best-effort by design
        return None


__all__ = ["setup_logging", "LOG_PATH", "LOG_DIR"]
