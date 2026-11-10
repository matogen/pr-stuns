#!/usr/bin/env python3
"""
Main entry point for PR Review Bot command-line interface.
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time

from pr_reviewer.core import PRReviewer
from pr_reviewer.config import create_sample_config, ConfigManager
from pr_reviewer.utils.logging import setup_logging, logger
from pr_reviewer.utils.debug import setup_exception_handler


def main() -> int:
    """Main entry point
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parser = argparse.ArgumentParser(description="PR Review Bot with AI")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to configuration file")
    parser.add_argument("--create-sample-config", action="store_true", help="Create a sample configuration file")
    parser.add_argument("--log-file", default="pr_reviewer.log", help="Path to log file")
    parser.add_argument("--monitor", "-m", action="store_true", help="Enable continuous monitoring (overrides config)")
    parser.add_argument("--interval", "-i", type=int, help="Monitoring interval in minutes (overrides config)")
    parser.add_argument("--one-shot", action="store_true", help="Run once and exit (overrides monitoring config)")
    parser.add_argument("--continuous", "--cont", action="store_true", help="Run continuously with 1-minute interval, ignoring monitoring settings")
    parser.add_argument("--clean", action="store_true", help="Clean up processed PR tracking (forces re-review of all PRs)")
    parser.add_argument("--list-prs", action="store_true", help="List tracked PRs without running the reviewer")
    parser.add_argument("--reset-repo", type=str, help="Reset PRs for a specific repository (format: repo_slug)")
    parser.add_argument("--reset-all-repos", action="store_true", help="Reset PRs for all repositories but preserve tracking files")
    parser.add_argument("--list-repo-ids", action="store_true", help="List all repository identifiers used for PR tracking")
    parser.add_argument("--migrate-underscore", action="store_true", help="Migrate data from the legacy underscore file to repository-specific files")
    parser.add_argument("--fix-repo-files", action="store_true", help="Fix repository files that were created with incorrect data")
    
    args = parser.parse_args()
    
    # Configure logging and exception handling
    logger = setup_logging(args.log_file)
    setup_exception_handler()
    
    if args.create_sample_config:
        try:
            create_sample_config("config_sample.yaml")
            logger.info("Sample configuration created at: config_sample.yaml")
            return 0
        except Exception as e:
            logger.error(f"Error creating sample configuration: {e}")
            return 1
    
    try:
        reviewer = PRReviewer(args.config)
        
        # Handle special commands
        # Get the config first for repository operations
        config = ConfigManager(args.config)
        repositories = config.get_repositories()
        
        # Fix repository files if requested
        if args.fix_repo_files:
            results = reviewer.fix_repo_files()
            if results:
                print("\nRepository files fixed:")
                print("=====================")
                total_removed = sum(results.values())
                for repo_id, removed in results.items():
                    print(f"- {repo_id}: Removed {removed} incorrect PRs")
                print(f"\nTotal: Removed {total_removed} incorrect PRs across {len(results)} repositories")
            else:
                print("\nNo repository files needed fixing.")
                
            # If fix-repo-files is the only action, exit
            if not args.monitor and not args.list_prs and not args.clean and not args.reset_all_repos and not args.reset_repo and not args.list_repo_ids and not args.migrate_underscore and not args.continuous:
                return 0
        
        # Migrate underscore file if requested
        if args.migrate_underscore:
            count = reviewer.migrate_underscore_file()
            if count > 0:
                print(f"\nSuccessfully migrated underscore file to {count} repository-specific files.")
            else:
                print("\nNo data migrated from underscore file.")
                
            # If migrate-underscore is the only action, exit
            if not args.monitor and not args.list_prs and not args.clean and not args.reset_all_repos and not args.reset_repo and not args.list_repo_ids and not args.continuous:
                return 0
                
        # Print repository identifiers if requested
        if args.list_repo_ids:
            print("\nRepository Identifiers:")
            print("========================")
            for repo_config in repositories:
                repo_id = reviewer._get_repo_identifier(repo_config)
                repo_type = repo_config.get("type", "unknown")
                if repo_type == "github":
                    display_name = f"{repo_config.get('owner', '')}/{repo_config.get('repo', '')}"
                elif repo_type == "gitlab":
                    display_name = f"{repo_config.get('url', '')}:{repo_config.get('project_id', '')}"
                elif repo_type == "bitbucket":
                    display_name = f"{repo_config.get('workspace', '')}/{repo_config.get('repo_slug', '')}"
                else:
                    display_name = str(repo_config)
                
                print(f"Repository: {display_name}")
                print(f"Identifier: {repo_id}")
                
                # Show file path for storing processed PRs
                file_path = reviewer._get_processed_file_path(repo_id)
                print(f"Storage file: {file_path}")
                
                # Check if file exists and contains PRs
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            processed = json.load(f)
                        print(f"PRs stored: {len(processed)}")
                    except:
                        print("PRs stored: Error reading file")
                else:
                    print("PRs stored: File not found")
                    
                print()
            
            # If list-repo-ids is the only action, exit
            if not args.monitor and not args.list_prs and not args.clean and not args.reset_all_repos and not args.reset_repo:
                return 0
        
        if args.reset_repo:
            # Reset PRs for a specific repository
            repo_slug = args.reset_repo.lower()
            found = False
            
            for repo_config in repositories:
                if repo_config.get("repo_slug", "").lower() == repo_slug or repo_config.get("repo", "").lower() == repo_slug:
                    count = reviewer.reset_repository_prs(repo_config)
                    logger.info(f"Reset {count} PRs for repository {repo_slug}")
                    found = True
                    break
            
            if not found:
                logger.warning(f"No repository with slug or name '{repo_slug}' found in configuration")
            
            # If reset-repo is the only action, exit
            if not args.monitor and not args.list_prs and not args.clean and not args.reset_all_repos:
                return 0
                
        if args.reset_all_repos:
            # Reset PRs for all repositories but preserve tracking files
            total_reset = 0
            
            for repo_config in repositories:
                repo_slug = repo_config.get("repo_slug") or repo_config.get("repo", "unknown")
                count = reviewer.reset_repository_prs(repo_config)
                total_reset += count
                logger.info(f"Reset {count} PRs for repository {repo_slug}")
            
            logger.info(f"Reset a total of {total_reset} PRs across {len(repositories)} repositories")
            
            # If reset-all-repos is the only action, exit
            if not args.monitor and not args.list_prs and not args.clean:
                return 0
        
        if args.clean:
            # Clean up processed PR tracking completely
            processed_dir = "processed_prs"
            
            if os.path.exists(processed_dir):
                logger.info(f"Cleaning up processed PR tracking directory: {processed_dir}")
                shutil.rmtree(processed_dir)
                os.makedirs(processed_dir)
                
            legacy_file = "processed_prs.json"
            if os.path.exists(legacy_file):
                logger.info(f"Removing legacy PR tracking file: {legacy_file}")
                os.remove(legacy_file)
                
            logger.info("PR tracking has been completely reset. All PRs will be reviewed again.")
            
            # If clean is the only action, exit
            if not args.monitor and not args.list_prs:
                return 0
        
        # List tracked PRs if requested
        if args.list_prs:
            processed_prs = reviewer.list_processed_prs()
            print("\nCurrently tracked PRs:")
            print("=====================")
            
            if not processed_prs:
                print("No PRs are currently tracked.")
            else:
                for repo_name, prs in processed_prs.items():
                    print(f"\n{repo_name}: {len(prs)} PRs")
                    for pr in prs:
                        if isinstance(pr, dict):
                            pr_id = pr.get("id", "unknown")
                            timestamp = pr.get("timestamp", 0)
                            date_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                            print(f"  - PR #{pr_id} (processed on {date_str})")
                        else:
                            print(f"  - PR #{pr} (legacy format)")
            
            # If list-prs is the only action, exit
            if not args.monitor:
                return 0
        
        # Check for continuous mode (takes precedence over other settings)
        if args.continuous:
            logger.info("Starting in continuous mode (running every 1 minute)")
            
            # Run indefinitely with a 1-minute interval
            run_count = 0
            ONE_MINUTE = 60
            
            try:
                while True:
                    run_count += 1
                    start_time = time.time()
                    
                    # Run the reviewer
                    logger.info(f"Starting continuous run #{run_count}")
                    try:
                        reviewer.run()
                        logger.info(f"Completed continuous run #{run_count}")
                    except Exception as e:
                        logger.error(f"Error during continuous run #{run_count}: {e}")
                    
                    # Calculate time to wait
                    elapsed = time.time() - start_time
                    wait_time = max(1, ONE_MINUTE - elapsed)  # Ensure at least 1 second wait
                    
                    # Sleep until the next run
                    next_run_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + wait_time))
                    logger.info(f"Waiting for next run at {next_run_time} (in {wait_time:.1f} seconds)")
                    time.sleep(wait_time)
                    
            except KeyboardInterrupt:
                logger.info("Continuous mode interrupted by user")
                return 0
                
            except Exception as e:
                logger.error(f"Fatal error in continuous mode: {e}")
                return 1
        
        # Get monitoring configuration for normal monitoring mode
        monitoring_config = config.get_monitoring_config()
        
        # Override with command line arguments if provided
        if args.one_shot:
            monitoring_config['enabled'] = False
        elif args.monitor:
            monitoring_config['enabled'] = True
        
        if args.interval:
            monitoring_config['interval_minutes'] = args.interval
        
        # Run once or in continuous monitoring mode
        if not monitoring_config['enabled']:
            logger.info("Running in one-shot mode")
            reviewer.run()
            return 0
            
        # Run in continuous monitoring mode (using configuration)
        logger.info(f"Starting continuous monitoring (interval: {monitoring_config['interval_minutes']} minutes)")
        
        runs = 0
        max_runs = monitoring_config['max_runs']
        interval_seconds = monitoring_config['interval_minutes'] * 60
        last_reset_time = time.time()
        reset_seconds = monitoring_config['reset_processed_hours'] * 3600
        
        while True:
            try:
                # Run the reviewer
                logger.info(f"Starting monitoring run #{runs+1}" + (f" of {max_runs}" if max_runs > 0 else ""))
                reviewer.run()
                runs += 1
                
                # Check if we need to reset processed PRs
                current_time = time.time()
                if current_time - last_reset_time >= reset_seconds:
                    logger.info(f"Resetting processed PRs older than {monitoring_config['reset_processed_hours']} hours")
                    reviewer.reset_processed_prs(monitoring_config['reset_processed_hours'])
                    last_reset_time = current_time
                
                # Check if we've reached the maximum number of runs
                if max_runs > 0 and runs >= max_runs:
                    logger.info(f"Reached maximum number of runs ({max_runs}), exiting")
                    break
                    
                # Sleep until the next run
                next_run_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + interval_seconds))
                logger.info(f"Waiting for next run at {next_run_time} (in {monitoring_config['interval_minutes']} minutes)")
                time.sleep(interval_seconds)
                
            except KeyboardInterrupt:
                logger.info("Monitoring interrupted by user")
                break
                
            except Exception as e:
                logger.error(f"Error during monitoring run: {e}")
                # Continue with next run instead of exiting
                time.sleep(interval_seconds)
        
        return 0
        
    except Exception as e:
        logger.error(f"Error running PR reviewer: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())