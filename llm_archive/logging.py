"""Logging utilities for llm-archive."""

from __future__ import annotations
import logging
from rich.logging import RichHandler
from rich.console import Console

_verbose = False
_console: Console | None = None


class ComponentFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        return f"  {message}"


root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

for _name in ("httpx", "httpcore", "litellm"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _setup_handler():
    """Setup logging handler."""
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_to_use = _console if _console else Console(stderr=True)
    handler = RichHandler(
        console=console_to_use,
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
        omit_repeated_times=False,
        show_level=True,
    )
    handler.setFormatter(ComponentFormatter("%(message)s"))
    root_logger.addHandler(handler)


_setup_handler()


def set_console(console: Console) -> None:
    """Set the Rich console instance to use for logging."""
    global _console
    _console = console
    _setup_handler()


def set_verbose(verbose: bool) -> None:
    """Set global verbose flag."""
    global _verbose
    _verbose = verbose
    if verbose:
        root_logger.setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        logging.getLogger("litellm").setLevel(logging.INFO)
    else:
        root_logger.setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("litellm").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a specific component (e.g., 'deepseek', 'claude')."""
    logger = logging.getLogger(f"llm_archive.{name}")
    return logger


def log(message: str, level: str = "info", source: str | None = None) -> None:
    """Log a message with optional source prefix."""
    if source:
        message = f"[{source}] {message}"

    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }

    log_level = level_map.get(level.lower(), logging.INFO)
    root_logger.log(log_level, message)