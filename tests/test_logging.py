from __future__ import annotations
import logging

from llm_archive.logging import (
    ComponentFormatter,
    get_logger,
    log,
    set_verbose,
)


class TestComponentFormatter:
    def test_indents_message(self):
        formatter = ComponentFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        result = formatter.format(record)
        assert result == "  hello"


class TestSetVerbose:
    def test_verbose_true(self):
        set_verbose(True)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_verbose_false(self):
        set_verbose(False)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_verbose_affects_httpx(self):
        set_verbose(True)
        assert logging.getLogger("httpx").level == logging.DEBUG
        set_verbose(False)
        assert logging.getLogger("httpx").level == logging.WARNING


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test_module")
        assert logger.name == "llm_archive.test_module"


class TestLog:
    def test_log_info(self):
        log("test message", level="info")

    def test_log_with_source(self):
        log("test message", level="warning", source="mysource")

    def test_log_unknown_level_defaults_to_info(self):
        log("test message", level="unknown_level")