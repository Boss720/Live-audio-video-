"""Smoke tests for observability/logging setup."""

import json
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

import obs.logging_setup
from obs.logging_setup import _JSONFormatter, get_logger, get_run_id, setup_logging


def test_run_id_is_uuid() -> None:
    """The run_id should be a hyphenated lowercase UUID."""
    rid = get_run_id()
    parts = rid.split("-")
    assert len(parts) == 5
    assert all(p.isalnum() for p in parts)
    assert rid == rid.lower()


def test_json_formatter_output() -> None:
    """The JSON formatter emits a single-line JSON object."""
    formatter = _JSONFormatter(run_id="test-run-id")
    record = logging.LogRecord(
        name="gemini_live_agent.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "gemini_live_agent.test"
    assert payload["run_id"] == "test-run-id"
    assert "timestamp" in payload


def test_setup_logging_writes_json(tmp_path: Path) -> None:
    """setup_logging writes JSON records to the configured log file."""
    log_file = tmp_path / "agent.log"
    logger = setup_logging(run_id="setup-test", debug=False)
    # Replace the file handler with one pointing at our temp file.
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(_JSONFormatter(run_id="setup-test"))
    logger.addHandler(handler)

    get_logger("test").info("smoke test", extra={"extra_key": "extra_value"})

    # Force flush and close
    handler.close()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "smoke test"
    assert payload["extra_key"] == "extra_value"


def test_setup_logging_without_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under pythonw (sys.stderr is None) only the file handler is installed."""
    monkeypatch.setattr(obs.logging_setup, "_log_directory", lambda: tmp_path)
    monkeypatch.setattr(sys, "stderr", None)
    logger = setup_logging(run_id="no-stderr", debug=False)
    try:
        assert [type(h) for h in logger.handlers] == [logging.handlers.RotatingFileHandler]
        get_logger("test").info("still logged")
        logger.handlers[0].flush()
        assert "still logged" in (tmp_path / "agent.log").read_text(encoding="utf-8")
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
