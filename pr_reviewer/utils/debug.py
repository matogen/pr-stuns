#!/usr/bin/env python3
"""
Debug utilities for PR reviewer.
"""

import sys
import traceback
import logging

logger = logging.getLogger(__name__)

def format_exception(e):
    """
    Format an exception with traceback for better debug information
    
    Args:
        e: Exception to format
        
    Returns:
        Formatted exception string with traceback
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    return ''.join(tb_lines)

def setup_exception_handler():
    """
    Set up global exception handler for better debug output
    """
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        logger.error("Uncaught exception", 
                     exc_info=(exc_type, exc_value, exc_traceback))
    
    sys.excepthook = global_exception_handler
    
    logger.info("Global exception handler registered")