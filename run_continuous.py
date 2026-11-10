#!/usr/bin/env python3
"""
Script to run the PR reviewer in continuous mode every 10 minutes.
"""

import sys
import os
import signal
import time
import datetime

# Import the package's main function
from pr_reviewer.__main__ import main as package_main
from pr_reviewer.core import PRReviewer
from pr_reviewer.utils.logging import logger

_shutdown_requested = False


def _handle_sigterm(signum, frame):
    """Request a graceful shutdown instead of the default abrupt SIGTERM kill.

    watchdog.sh's stop/restart send SIGTERM. Without a handler, Python's
    default disposition terminates the process immediately — including
    mid-write to processed_prs/*.json. Catching it lets the current
    operation finish and the run loop exit cleanly at its next safe point.
    """
    global _shutdown_requested
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    _shutdown_requested = True


def run_once():
    """Run the PR reviewer once"""
    # Set up arguments for a single run
    sys.argv = [sys.argv[0], "--config", "config.yaml"]
    
    # Call the package's main function
    return package_main()

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Write our own PID to prevent watchdog from spawning duplicates
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pid_file = os.path.join(script_dir, "run_continuous.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    # Execute the PR reviewer in continuous mode with 10-minute intervals
    INTERVAL_MINUTES = 10
    INTERVAL_SECONDS = INTERVAL_MINUTES * 60

    print(f"Starting PR reviewer in continuous mode (running every {INTERVAL_MINUTES} minutes)...")
    print("(Press Ctrl+C to stop)")
    print("==================================")

    run_count = 0

    try:
        while not _shutdown_requested:
            run_count += 1
            start_time = time.time()

            # Print timestamp for this run
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\nRun #{run_count} at {current_time}")
            print("----------------------------------")

            # Run the reviewer
            try:
                result = run_once()
                print(f"Completed run #{run_count} with exit code: {result}")
            except Exception as e:
                print(f"Error during run #{run_count}: {e}")

            if _shutdown_requested:
                break

            # Calculate time to wait
            elapsed = time.time() - start_time
            wait_time = max(1, INTERVAL_SECONDS - elapsed)  # Ensure at least 1 second wait

            # Print next run time
            next_run_time = datetime.datetime.fromtimestamp(time.time() + wait_time).strftime("%Y-%m-%d %H:%M:%S")
            print(f"Waiting for next run at {next_run_time} (in {wait_time:.1f} seconds)")

            # Sleep in short increments so a SIGTERM/SIGINT during the wait
            # is noticed within ~1 second instead of after the full interval.
            wait_deadline = time.time() + wait_time
            while not _shutdown_requested and time.time() < wait_deadline:
                time.sleep(min(1, wait_deadline - time.time()))

    except KeyboardInterrupt:
        print("\nPR reviewer stopped by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)

    print("\nPR reviewer stopped (SIGTERM).")
    sys.exit(0)