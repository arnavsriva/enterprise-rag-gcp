import json
import logging

import pytest

from common.logging import configure_logging


def test_json_line_with_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("t").info("served", extra={"latency_ms": 12, "backend": "pgvector"})

    record = json.loads(capsys.readouterr().out.strip())
    assert record["severity"] == "INFO"
    assert record["message"] == "served"
    assert record["latency_ms"] == 12
    assert record["backend"] == "pgvector"
    assert "args" not in record  # LogRecord internals are not leaked


def test_exceptions_are_serialised(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")

    record = json.loads(capsys.readouterr().out.strip())
    assert record["severity"] == "ERROR"
    assert "ValueError: boom" in record["exception"]
