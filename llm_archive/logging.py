"""Logging utilities for llm-archive."""
from __future__ import annotations
import asyncio
import logging
import sys
from functools import wraps
from rich.logging import RichHandler
from rich.console import Console

# Global verbose flag
_verbose = False
_console: Console | None = None

# Custom formatter to show just [component] instead of full logger name
class ComponentFormatter(logging.Formatter):
    def format(self, record):
        # Extract component name from logger name (e.g., "deepseek" from "llm_archive.deepseek")
        name = record.name
        if "." in name:
            name = name.split(".")[-1]
        
        # Format with component prefix (RichHandler will add colored level)
        message = super().format(record)
        return f"[{name}] {message}"

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

# Suppress httpx INFO logs
logging.getLogger("httpx").setLevel(logging.WARNING)

def _setup_handler():
    """Setup logging handler."""
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Use shared console if set, otherwise default to stderr
    console_to_use = _console if _console else Console(stderr=True)
    handler = RichHandler(
        console=console_to_use,
        rich_tracebacks=True,
        show_time=False,
        show_path=False,
        omit_repeated_times=False,
        show_level=False,  # Don't show level name, but RichHandler still uses colors
    )
    handler.setFormatter(ComponentFormatter("%(message)s"))
    root_logger.addHandler(handler)

# Initial setup
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
    else:
        root_logger.setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)

def is_verbose() -> bool:
    """Check if verbose logging is enabled."""
    return _verbose

def get_logger(name: str) -> logging.Logger:
    """Get a logger for a specific component (e.g., 'deepseek', 'claude')."""
    logger = logging.getLogger(f"llm_archive.{name}")
    return logger

def log(message: str, level: str = "info", source: str | None = None) -> None:
    """Log a message with optional source prefix."""
    if source:
        message = f"[{source}] {message}"
    
    # Map string level to logging level
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    
    log_level = level_map.get(level.lower(), logging.INFO)
    root_logger.log(log_level, message)

def retry_async(max_retries: int = 3, base_delay: float = 1.0):
    """Async retry decorator with exponential backoff."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger = get_logger("retry")
                    logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
            return None
        return wrapper
    return decorator
