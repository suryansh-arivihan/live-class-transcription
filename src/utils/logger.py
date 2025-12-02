import logging
import sys
from pathlib import Path
from src.config import settings


def setup_logger(name: str = None) -> logging.Logger:
    """
    Setup and configure logger for the application.

    Args:
        name: Logger name (defaults to root logger if None)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Set log level from settings
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    if logger.hasHandlers():
        logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    # Create formatter
    formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)

    # Add handler to logger
    logger.addHandler(console_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


# Create default logger
logger = setup_logger(__name__)
