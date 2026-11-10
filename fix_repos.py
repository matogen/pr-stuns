#!/usr/bin/env python3
"""
Script to fix repository files in the PR reviewer tool.
"""

import sys
import os

# Import the package's main function
from pr_reviewer.__main__ import main as package_main

def main():
    """Set up arguments and call the package's main function"""
    # Set up arguments for fixing repository files
    sys.argv = [sys.argv[0], "--config", "config.yaml", "--fix-repo-files"]
    
    # Call the package's main function
    return package_main()

if __name__ == "__main__":
    # Execute the fix
    print("Running repository fix utility...")
    result = main()
    
    # Exit with the result code
    if result == 0:
        print("\nRepository fix completed successfully.")
    else:
        print("\nRepository fix failed with error code:", result)
    
    # Wait for user input before exiting
    input("\nPress Enter to exit...")
    
    sys.exit(result)