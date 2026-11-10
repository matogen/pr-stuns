#!/usr/bin/env python3
"""
PR Review Bot - Automated code review using a local LLM

This tool is now deprecated in favor of the modular package structure.
This script is kept for backward compatibility and will delegate to the new package.
"""

import sys
import logging

# Import the main function from the package
from pr_reviewer.__main__ import main as package_main
from pr_reviewer.utils.logging import setup_logging

def main():
    """Main entry point - delegates to the package implementation"""
    # Set up basic logging
    logger = setup_logging("pr_reviewer.log")
    logger.warning("Using the main.py entry point is deprecated. Use 'pr-reviewer' command instead.")
    
    # Forward all arguments to the package's main function
    return package_main()

if __name__ == "__main__":
    sys.exit(main())
