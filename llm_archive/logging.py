"""Logging utilities for llm-archive."""
from __future__ import annotations
import logging
import sys

# Global verbose flag
_verbose = False

# Custom formatter to show just [component] instead of full logger name
class ComponentFormatter(logging.Formatter):
    def format(self, record):
        # Extract component name from logger name (e.g., "deepseek" from "llm_archive.deepseek")
        name = record.name
        if "." in name:
            name = name.split(".")[-1]
        
        # Format with component prefix and level
        message = super().format(record)
        level = record.levelname
        return f"{level:8s} [{name}] {message}"

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
    
    # Add plain stream handler to stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ComponentFormatter("%(message)s"))
    root_logger.addHandler(handler)

# Initial setup
_setup_handler()

def set_console(console) -> None:
    """Set the console instance (no-op for plain handler)."""
    pass

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
