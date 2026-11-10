#!/usr/bin/env python3
"""
Logging setup for PR Review Bot.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

# Keep the log file bounded — a plain FileHandler previously grew past 1GB.
# 10MB x 5 backups = 50MB ceiling.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def setup_logging(log_file: Optional[str] = "pr_reviewer.log", log_level: int = logging.INFO) -> logging.Logger:
    """Configure logging for the application
    
    Args:
        log_file: Path to log file, or None for no file logging
        log_level: Logging level (default: INFO)
        
    Returns:
        Configured logger instance
    """
    # Create logger
    logger = logging.getLogger("pr_reviewer")
    logger.setLevel(log_level)
    logger.handlers = []  # Clear any existing handlers
    
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # Create rotating file handler if specified
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# Default logger instance
logger = setup_logging()