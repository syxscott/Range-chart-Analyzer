"""The rotating log must exist, rotate, and never contain an API key."""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core import logging_setup  # noqa: E402


def test_redacting_filter_scrubs_a_key_from_a_log_record():
    f = logging_setup._RedactingFilter()
    record = logging.LogRecord(
        name="rca.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="calling https://api.example.com?api_key=sk-SECRET12345678 failed",
        args=(), exc_info=None)
    assert f.filter(record) is True
    assert "sk-SECRET12345678" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_redacting_filter_handles_percent_args():
    f = logging_setup._RedactingFilter()
    record = logging.LogRecord(
        name="rca.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="Bearer %s", args=("abcdefghijklmnop",), exc_info=None)
    f.filter(record)
    assert record.args == ()
    assert "[REDACTED]" in record.getMessage()


def test_setup_logging_writes_a_file_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "data")
    monkeypatch.setattr(logging_setup, "LOG_PATH", tmp_path / "data" / "rca.log")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        # Remove any handler a previous test in this process installed.
        for h in list(root.handlers):
            if getattr(h, "name", "") == logging_setup._HANDLER_NAME:
                root.removeHandler(h)
                h.close()
        first = logging_setup.setup_logging()
        assert first is not None and first.is_file()
        n = len(root.handlers)
        logging_setup.setup_logging()
        assert len(root.handlers) == n, "setup_logging installed a second handler"

        logging.getLogger("rca.test").info("hello from the test")
        for h in root.handlers:
            if getattr(h, "name", "") == logging_setup._HANDLER_NAME:
                h.flush()
        assert "hello from the test" in first.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if getattr(h, "name", "") == logging_setup._HANDLER_NAME:
                root.removeHandler(h)
                h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)


def test_setup_logging_never_raises_on_an_unwritable_path(tmp_path, monkeypatch):
    # A file where a DIRECTORY should be forces mkdir/OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(logging_setup, "LOG_DIR", blocker / "nested")
    monkeypatch.setattr(logging_setup, "LOG_PATH", blocker / "nested" / "rca.log")
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "name", "") == logging_setup._HANDLER_NAME:
            root.removeHandler(h)
            h.close()
    assert logging_setup.setup_logging() is None
