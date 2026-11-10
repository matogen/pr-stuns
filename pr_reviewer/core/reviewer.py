#!/usr/bin/env python3
"""
Core PR reviewer implementation.
"""

import os
import json
import time
import tempfile
import fcntl
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple, Union

from pr_reviewer.config import ConfigManager
from pr_reviewer.clients import create_client, BaseRepoClient
from pr_reviewer.clients.goby import GobyClient
from pr_reviewer.clients.jira import JiraClient, extract_issue_key
from pr_reviewer.llm import create_llm, BaseLLM
from pr_reviewer.models import PullRequest, FileChange
from pr_reviewer.utils.logging import logger
from pr_reviewer.context.context_provider import ContextProvider
from pr_reviewer.core.flow_diagram import generate_flow_diagram_section, resolve_flow_diagram_config

# Cap how much full-file context goes into a single review prompt. A very
# large file would bloat the prompt, slow the response, and risk a truncated
# review. The diff itself is always included separately and in full.
MAX_FILE_CONTENT_CHARS = 50000

# Cap how many changed filenames get listed in the PR-wide file summary
# (see _build_changed_files_summary). Filenames are cheap, but an
# enormous PR (a lockfile update touching hundreds of files) shouldn't
# blow up every single per-file prompt just to list them all.
MAX_CHANGED_FILES_LISTED = 100

# The exact answer the system prompt asks for when a file has nothing worth
# flagging. It is a complete verdict, not a truncated review.
NO_ISSUES_SENTINEL = "NO_ISSUES_FOUND"

# finish_reason (OpenRouter) / stop_reason (Anthropic) values meaning the
# model ran out of output budget mid-review.
TRUNCATED_FINISH_REASONS = ("length", "max_tokens")


class PRReviewer:
    """Main PR reviewer implementation"""
    
    def __init__(self, config_path: str):
        """Initialize PR reviewer
        
        Args:
            config_path: Path to configuration file
        """
        self.config = ConfigManager(config_path)
        self.llm: Optional[BaseLLM] = None
        self._jira_clients: Dict[str, JiraClient] = {}  # keyed by jira_instance name
        self._goby_clients: Dict[str, GobyClient] = {}  # keyed by goby_instance name

    def run(self):
        """Main run method to process all repositories in config
        
        Initializes the LLM and processes each repository defined in the config.
        """
        # Initialize LLM if not already done
        self.initialize_llm()
        
        # Get repositories from configuration
        repositories = self.config.get_repositories()
        
        if not repositories:
            logger.warning("No repositories configured.")
            return
            
        # Process each repository
        for repo_config in repositories:
            try:
                self.process_repository(repo_config)
            except Exception as e:
                logger.error(f"Error processing repository: {e}")
                # Continue with next repository
        
    def initialize_llm(self) -> None:
        """Initialize LLM model if not already done"""
        if self.llm is None:
            llm_config = self.config.get_llm_config()
            self.llm = create_llm(llm_config)
            
            # Check if we're using the dummy LLM
            try:
                from pr_reviewer.llm.dummy import DummyLLM
                if isinstance(self.llm, DummyLLM):
                    logger.warning("Using DummyLLM - this is for testing only and will not provide real code review")
            except ImportError:
                pass
    
    def filter_reviewable_changes(self, changes: List[FileChange], repo_config: Optional[Dict[str, Any]] = None) -> List[FileChange]:
        """Filter changes to only include files that should be reviewed
        
        Args:
            changes: List of file changes
            repo_config: Optional repository configuration to use for filtering
            
        Returns:
            Filtered list of file changes
        """
        return [
            change for change in changes
            if self.should_review_file(change.filename, repo_config)
        ]
    
    def should_review_file(self, filename: str, repo_config: Optional[Dict[str, Any]] = None) -> bool:
        """Determine if a file should be reviewed based on its extension and repository-specific patterns
        
        Args:
            filename: File path
            repo_config: Optional repository configuration to use for repo-specific patterns
            
        Returns:
            True if the file should be reviewed, False otherwise
        """
        # Skip binary files, images, etc.
        no_review_extensions = [
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico",
            ".pdf", ".zip", ".tar", ".gz", ".rar",
            ".min.js", ".min.css",
            ".lock", ".sum"
        ]
        
        # Skip temporary and generated files - global patterns
        no_review_patterns = [
            "__pycache__", "node_modules", ".git",
            "package-lock.json", "yarn.lock", "go.sum", 
            "claude_output"  # Ignore files in claude_output directory
        ]
        
        # Check repository-specific patterns if available
        if repo_config:
            # Check for repository-specific exclude patterns
            repo_exclude_patterns = repo_config.get("exclude_patterns", [])
            if repo_exclude_patterns:
                for pattern in repo_exclude_patterns:
                    if pattern in filename:
                        logger.debug(f"Skipping file {filename} due to repo-specific exclude pattern: {pattern}")
                        return False
            
            # Check for repository-specific exclude extensions
            repo_exclude_extensions = repo_config.get("exclude_extensions", [])
            if repo_exclude_extensions:
                for ext in repo_exclude_extensions:
                    # Ensure extension starts with a dot
                    ext_with_dot = ext if ext.startswith(".") else f".{ext}"
                    if filename.endswith(ext_with_dot):
                        logger.debug(f"Skipping file {filename} due to repo-specific exclude extension: {ext}")
                        return False
                        
            # Check for repository-specific exclude regex patterns
            repo_exclude_regex = repo_config.get("exclude_regex", [])
            if repo_exclude_regex:
                import re
                for regex_pattern in repo_exclude_regex:
                    try:
                        if re.search(regex_pattern, filename):
                            logger.debug(f"Skipping file {filename} due to repo-specific exclude regex: {regex_pattern}")
                            return False
                    except re.error:
                        logger.warning(f"Invalid regex pattern in repository config: {regex_pattern}")
        
        # Check file extensions
        for ext in no_review_extensions:
            if filename.endswith(ext):
                return False
        
        # Check patterns
        for pattern in no_review_patterns:
            if pattern in filename:
                return False
        
        return True
    
    def _get_repo_identifier(self, repo_config: Dict[str, Any], git_client: Optional[BaseRepoClient] = None) -> str:
        """Generate a unique identifier for a repository
        
        Args:
            repo_config: Repository configuration
            git_client: Optional repository client (for additional type information)
            
        Returns:
            Unique repository identifier
        """
        repo_type = repo_config.get("type", "unknown").lower()
        
        if repo_type == "github":
            owner = repo_config.get("owner", "")
            repo = repo_config.get("repo", "")
            return f"github:{owner}/{repo}"
            
        elif repo_type == "gitlab":
            url = repo_config.get("url", "")
            project_id = repo_config.get("project_id", "")
            return f"gitlab:{url}/{project_id}"
            
        elif repo_type == "bitbucket":
            workspace = repo_config.get("workspace", "")
            repo_slug = repo_config.get("repo_slug", "")
            return f"bitbucket:{workspace}/{repo_slug}"
            
        else:
            # Fallback for unknown types
            repo = repo_config.get("repo", "")
            owner = repo_config.get("owner", "")
            if owner and repo:
                return f"{repo_type}:{owner}/{repo}"
            else:
                # Last resort - use a hash of the config to make it unique
                import hashlib
                config_str = str(sorted([(k, v) for k, v in repo_config.items() if k != "token" and k != "app_password"]))
                config_hash = hashlib.md5(config_str.encode()).hexdigest()[:8]
                return f"{repo_type}:repo_{config_hash}"
    
    def process_repository(self, repo_config: Dict[str, Any]) -> None:
        """Process a single repository configuration
        
        Args:
            repo_config: Repository configuration
        """
        # Create git client for this repository
        git_client = create_client(repo_config)
        
        # Get unique repository identifier
        repo_id = self._get_repo_identifier(repo_config, git_client)
        
        # Log the repository ID for debugging
        logger.info(f"Using repository ID: {repo_id}")
        
        # Get displayable name for logs
        repo_type = repo_config.get("type", "unknown")
        if repo_type == "github":
            display_name = f"{repo_config.get('owner', '')}/{repo_config.get('repo', '')}"
            username = repo_config.get("username", "")
        elif repo_type == "gitlab":
            display_name = f"{repo_config.get('url', '')}/{repo_config.get('project_id', '')}"
            username = ""  # GitLab API token doesn't require explicit username
        elif repo_type == "bitbucket":
            display_name = f"{repo_config.get('workspace', '')}/{repo_config.get('repo_slug', '')}"
            username = repo_config.get("username", "")
        else:
            display_name = repo_id
            username = ""
            
        logger.info(f"Processing repository: {display_name} (ID: {repo_id})")
        
        # Get latest pull requests
        latest_pulls = self._get_pull_requests(git_client, repo_config)
        max_prs = repo_config.get("max_prs", 5)
        
        for pr_data in latest_pulls[:max_prs]:
            pr = self._convert_to_pull_request(git_client, pr_data)
            logger.info(f"Processing PR #{pr.id}: {pr.title}")
            
            # Log detailed PR metadata for debugging
            logger.info(f"PR #{pr.id} metadata - updated_at: {pr.updated_at}, head_commit: {pr.head_commit}")
            
            # Debug dump all PR details to help diagnose Bitbucket issues
            if repo_type == "bitbucket":
                logger.info(f"Bitbucket PR #{pr.id} full details: title={pr.title}, author={pr.author}, state={pr.state}, created_at={pr.created_at}, updated_at={pr.updated_at}, head_commit={pr.head_commit}, base_branch={pr.base_branch}, head_branch={pr.head_branch}")
            
            # Check if this PR has been processed before and if it needs updating
            is_processed, pr_metadata, needs_update = self.is_pr_already_processed(repo_id, pr.id, pr)
            
            if is_processed and not needs_update:
                logger.info(f"PR #{pr.id} already processed and no updates detected, skipping")
                continue
                
            if is_processed and needs_update:
                logger.info(f"PR #{pr.id} has been updated since last processed, reviewing changes")
            
            # Check if we should review this PR based on commit messages
            # For first-time reviews (not processed), always proceed
            # For updates, check for review trigger in commit messages
            is_first_review = not is_processed
            if not self.should_review_pr_based_on_commits(git_client, pr, is_first_review, repo_config):
                logger.info(f"PR #{pr.id} skipped due to commit message filtering")
                continue
            
            # Analyze existing comments to build conversation context
            authenticated_username = self.get_authenticated_username(git_client, repo_config)
            # Also get display name for comparison
            authenticated_display_name = self.get_authenticated_display_name(git_client, repo_config)
            comment_analysis = self.analyze_pr_comment_history(git_client, pr, authenticated_username, authenticated_display_name)
            
            # Log comment analysis summary
            total_ai_comments = comment_analysis.get('total_ai_comments', 0)
            total_resolved = comment_analysis.get('total_resolved', 0)
            total_threads = len(comment_analysis.get('conversation_threads', []))
            
            if total_ai_comments > 0:
                logger.info(f"PR #{pr.id} conversation analysis: {total_ai_comments} previous AI comments, "
                           f"{total_resolved} resolved, {total_ai_comments - total_resolved} unresolved, "
                           f"{total_threads} conversation threads")
                
                # Log file-specific conversation summary
                file_comment_map = comment_analysis.get('file_comment_map', {})
                files_with_comments = len(file_comment_map)
                if files_with_comments > 0:
                    logger.info(f"PR #{pr.id} files with previous comments: {files_with_comments} files")
                    for file_path, comments in list(file_comment_map.items())[:5]:  # Log first 5 files
                        ai_comments_count = len([c for c in comments if c.get('author') == authenticated_username])
                        if ai_comments_count > 0:
                            logger.debug(f"  - {file_path}: {ai_comments_count} AI comments")
            else:
                logger.info(f"PR #{pr.id} conversation analysis: No previous AI comments found - first-time review")
            
            # Get changes for this PR
            file_changes = self._get_pr_file_changes(git_client, pr)
            pr.file_changes = file_changes
            
            # Filter changes based on repository configuration
            reviewable_changes = self.filter_reviewable_changes(file_changes, repo_config)
            
            # Log excluded files for visibility
            excluded_files = set(change.filename for change in file_changes) - set(change.filename for change in reviewable_changes)
            if excluded_files:
                logger.info(f"Excluded {len(excluded_files)} files from review in PR #{pr.id}: {', '.join(excluded_files)}")
            
            if not reviewable_changes:
                logger.info(f"No reviewable changes found in PR #{pr.id}")
                # Still mark as processed even if no changes to review
                self.mark_pr_as_processed(repo_id, pr.id, pr)
                continue
            
            # Analyze and comment on the changes
            # Smart file selection: only review changed files on re-reviews to improve efficiency
            if is_processed and needs_update:
                # Get the previous head commit if available
                previous_head_commit = pr_metadata.get("head_commit") if pr_metadata else None
                
                # Filter to only review files that changed in the new commit(s)
                files_to_review = reviewable_changes
                if previous_head_commit and pr.head_commit and previous_head_commit != pr.head_commit:
                    try:
                        # Get the files changed in the new commit(s)
                        changed_files = git_client.get_commit_changed_files(pr.head_commit)
                        
                        if changed_files:
                            # Extract filenames of changed files
                            changed_filenames = set(file.get("filename") for file in changed_files)
                            logger.info(f"New commit detected. Only reviewing changed files: {', '.join(changed_filenames)}")
                            
                            # Filter reviewable_changes to only include changed files
                            files_to_review = [
                                change for change in reviewable_changes 
                                if change.filename in changed_filenames
                            ]
                            
                            logger.info(f"PR #{pr.id} re-review: {len(files_to_review)} changed files vs {len(reviewable_changes)} total files")
                        else:
                            logger.info(f"PR #{pr.id} re-review: No changed files detected, reviewing all files")
                    except Exception as e:
                        logger.warning(f"Error retrieving changed files for commit {pr.head_commit}: {e}")
                        logger.info(f"PR #{pr.id} re-review: Falling back to reviewing all files due to error")
                else:
                    logger.info(f"PR #{pr.id} re-review: No previous commit found, reviewing all files")
                
                # Review only changed files (or all if we couldn't determine changes)
                self._analyze_and_comment(
                    git_client,
                    pr,
                    files_to_review,
                    comment_analysis,
                    is_update=True,
                    previous_head_commit=previous_head_commit,
                    repo_config=repo_config
                )
            else:
                # First time reviewing this PR - review all files
                self._analyze_and_comment(git_client, pr, reviewable_changes, comment_analysis, repo_config=repo_config)
            
            # Remove user as watcher from the PR
            if username and repo_config.get("remove_watcher", True):
                try:
                    logger.info(f"Attempting to remove {username} as watcher from PR #{pr.id}")
                    result = git_client.remove_user_as_watcher(pr.id, username)
                    if result:
                        logger.info(f"Successfully removed {username} as watcher from PR #{pr.id}")
                    else:
                        logger.warning(f"Failed to remove {username} as watcher from PR #{pr.id}")
                except Exception as e:
                    logger.error(f"Error removing user as watcher from PR #{pr.id}: {e}")
            
            # Make sure the PR object has required metadata before storing it
            if not pr.updated_at and not pr.head_commit:
                logger.warning(f"PR #{pr.id} doesn't have updated_at or head_commit metadata - future update checks may not work properly")
            else:
                logger.info(f"Storing metadata for PR #{pr.id} - updated_at: {pr.updated_at}, head_commit: {pr.head_commit}")
                
            # Mark PR as processed with updated metadata
            self.mark_pr_as_processed(repo_id, pr.id, pr)
    
    def _convert_to_pull_request(self, git_client: BaseRepoClient, pr_data: Dict[str, Any]) -> PullRequest:
        """Convert API data to PullRequest model
        
        Args:
            git_client: Repository client
            pr_data: Raw pull request data from API
            
        Returns:
            PullRequest instance
        """
        if isinstance(git_client, self._get_client_class("github")):
            return PullRequest.from_github_data(pr_data)
        elif isinstance(git_client, self._get_client_class("gitlab")):
            return PullRequest.from_gitlab_data(pr_data)
        elif isinstance(git_client, self._get_client_class("bitbucket")):
            return PullRequest.from_bitbucket_data(pr_data)
        else:
            # Generic fallback
            return PullRequest(
                id=pr_data.get("id") or pr_data.get("number") or pr_data.get("iid"),
                title=pr_data.get("title", ""),
                author=pr_data.get("author", "Unknown"),
                description=pr_data.get("description") or pr_data.get("body"),
                state=pr_data.get("state", "open")
            )
    
    def _get_client_class(self, client_type: str) -> type:
        """Get the client class by type name
        
        Args:
            client_type: Client type name
            
        Returns:
            Client class
        """
        from pr_reviewer.clients.github import GitHubClient
        from pr_reviewer.clients.gitlab import GitLabClient
        from pr_reviewer.clients.bitbucket import BitbucketClient
        
        clients = {
            "github": GitHubClient,
            "gitlab": GitLabClient,
            "bitbucket": BitbucketClient
        }
        
        return clients.get(client_type.lower(), BaseRepoClient)
    
    def _get_pull_requests(self, git_client: BaseRepoClient, repo_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get pull requests from repository
        
        Args:
            git_client: Repository client
            repo_config: Repository configuration
            
        Returns:
            List of pull request data
        """
        # Map client-specific parameters
        params = {}
        
        if isinstance(git_client, self._get_client_class("github")):
            params = {
                "state": repo_config.get("pr_state", "open"),
                "sort": repo_config.get("pr_sort", "created"),
                "direction": repo_config.get("pr_direction", "desc")
            }
        elif isinstance(git_client, self._get_client_class("gitlab")):
            params = {
                "state": repo_config.get("pr_state", "opened"),
                "sort": repo_config.get("pr_sort", "created_at")
            }
        elif isinstance(git_client, self._get_client_class("bitbucket")):
            params = {
                "state": repo_config.get("pr_state", "OPEN"),
                "sort": repo_config.get("pr_sort", "created_on")
            }
        
        return git_client.get_pull_requests(**params)
    
    def _get_pr_file_changes(self, git_client: BaseRepoClient, pr: PullRequest) -> List[FileChange]:
        """Get file changes for a pull request
        
        Args:
            git_client: Repository client
            pr: Pull request
            
        Returns:
            List of file changes
        """
        raw_changes = git_client.get_pull_request_files(pr.id)
        file_changes = []
        
        for change_data in raw_changes:
            if isinstance(git_client, self._get_client_class("github")):
                file_change = FileChange.from_github_data(change_data)
            elif isinstance(git_client, self._get_client_class("gitlab")):
                file_change = FileChange.from_gitlab_data(change_data)
            elif isinstance(git_client, self._get_client_class("bitbucket")):
                file_change = FileChange.from_bitbucket_data(change_data)
            else:
                # Generic fallback
                file_change = FileChange(
                    filename=change_data.get("filename") or change_data.get("new_path", ""),
                    status=change_data.get("status", "modified"),
                    patch=change_data.get("patch") or change_data.get("diff", "")
                )
            
            file_changes.append(file_change)

        return file_changes

    def _get_ticket_context(self, pr: PullRequest, repo_config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Resolve the ticket linked from the PR's branch name, if any.

        Tickets can live in Goby or Jira (different projects use different
        tools), and both use the same "PREFIX-123" key shape, so the key is
        extracted once and each configured source is asked in turn. Goby
        goes first because a repo configuring `goby_instance:` is an
        explicit per-project opt-in, whereas the Jira "default" instance
        applies org-wide -- a Goby miss (key belongs to another team, or
        it's really a Jira ticket) is one cheap 404 and falls through.

        Which instance of each tool to use is looked up by name --
        `goby_instance:` / `jira_instance:` on the repo's config.yaml
        entry, defaulting to "default". See UserStore.get_goby_config /
        get_jira_config.

        Returns None whenever ticket context isn't available for any
        reason (nothing configured, no key in the branch name, fetch
        failure, ticket not found anywhere) -- all non-fatal, the review
        just proceeds without it.
        """
        issue_key = extract_issue_key(pr.head_branch)
        if not issue_key:
            return None

        repo_config = repo_config or {}
        return (
            self._get_goby_ticket_context(issue_key, repo_config.get("goby_instance") or "default")
            or self._get_jira_ticket_context(issue_key, repo_config.get("jira_instance") or "default")
        )

    def _get_goby_ticket_context(self, issue_key: str, instance_name: str) -> Optional[Dict[str, Any]]:
        """Ask the named Goby instance for the task, or None if that
        instance isn't configured / the task isn't there."""
        goby_config = self.config.get_goby_config(instance_name)
        if not (goby_config.get("base_url") and goby_config.get("api_key")):
            return None

        if instance_name not in self._goby_clients:
            self._goby_clients[instance_name] = GobyClient(goby_config["base_url"], goby_config["api_key"])

        return self._goby_clients[instance_name].get_task_context(issue_key)

    def _get_jira_ticket_context(self, issue_key: str, instance_name: str) -> Optional[Dict[str, Any]]:
        """Ask the named Jira instance for the issue, or None if that
        instance isn't configured / the issue isn't there."""
        jira_config = self.config.get_jira_config(instance_name)
        if not (jira_config.get("base_url") and jira_config.get("email") and jira_config.get("api_token")):
            return None

        if instance_name not in self._jira_clients:
            self._jira_clients[instance_name] = JiraClient(
                jira_config["base_url"], jira_config["email"], jira_config["api_token"]
            )

        context = self._jira_clients[instance_name].get_issue_context(issue_key)
        if context:
            context.setdefault("source", "jira")
        return context

    def _build_changed_files_summary(self, file_changes: List[FileChange]) -> str:
        """List every file changed in the PR, not just the one(s) actually
        being reviewed in this call.

        Per-file review calls only ever see one file's diff -- filtered by
        exclude_patterns/extensions/regex on top of that, and possibly
        narrowed further to just the latest commit's files on a re-review.
        That means a per-file prompt has no way to know, for example, that
        a PR adding a new endpoint never touched a test file, or that a
        change to a shared type has a dozen other files in the same PR
        that might depend on it. Filenames are cheap enough to always
        include in full (capped defensively for very large PRs), even
        though this method deliberately does NOT filter by the
        review-exclusion rules -- a PR-wide summary should show test files
        even in repos configured to skip reviewing test file *content*.
        """
        total = len(file_changes)
        listed = file_changes[:MAX_CHANGED_FILES_LISTED]

        section = f"\n\n### Files Changed in This PR ({total} total)\n"
        for change in listed:
            section += f"- {change.filename} ({change.status})\n"
        if total > len(listed):
            section += f"- ...and {total - len(listed)} more file(s) not listed\n"

        return section

    def _build_pr_context_block(self, pr: PullRequest, ticket_context: Optional[Dict[str, Any]]) -> str:
        """Build the PR/ticket context section prepended to every per-file prompt.

        Every file was previously reviewed with zero idea what the PR (or
        any linked ticket) was actually trying to accomplish, or even what
        else changed alongside it. This gives the model the PR title/
        description, the full list of changed files (see
        _build_changed_files_summary), and, when a Goby or Jira ticket is
        linked from the branch name, the ticket's own summary/description/status,
        plus an explicit instruction to flag a diff that doesn't seem to
        fulfill what the ticket asked for.
        """
        block = "\n\n### PR Context\n"
        if pr.title:
            block += f"**Title:** {pr.title}\n"
        if pr.description:
            block += f"**Description:** {pr.description}\n"

        if pr.file_changes:
            block += self._build_changed_files_summary(pr.file_changes)
            block += (
                "\nUse this file list for PR-wide awareness -- for example, "
                "note (as a general, non-line-specific finding) if this diff "
                "adds new logic or an API surface with no corresponding test "
                "file changed elsewhere in the PR. Don't demand tests for "
                "trivial or non-logic changes.\n"
            )

        if ticket_context:
            status = ticket_context.get("status") or "unknown status"
            source = {"goby": "Goby", "jira": "Jira"}.get(ticket_context.get("source", ""))
            heading = f"{status}, from {source}" if source else status
            block += f"\n**Linked ticket {ticket_context['key']}** ({heading}):\n"
            if ticket_context.get("summary"):
                block += f"- Summary: {ticket_context['summary']}\n"
            if ticket_context.get("description"):
                # Capped defensively -- a very long ticket description
                # shouldn't crowd out the actual code in the prompt.
                block += f"- Requirements: {ticket_context['description'][:2000]}\n"
            block += (
                "\nCompare the code changes against this ticket. If the diff "
                "clearly does not implement what the ticket asks for, or leaves "
                "an explicit requirement unaddressed, say so as a general "
                "(non-line-specific) finding. Do not speculate beyond what the "
                "ticket text actually says, and do not flag this if the PR is "
                "plausibly a partial/incremental step toward the ticket.\n"
            )

        return block

    def _analyze_and_comment(self, git_client: BaseRepoClient, pr: PullRequest,
                           file_changes: List[FileChange], comment_analysis: Dict[str, Any],
                           is_update=False, previous_head_commit=None,
                           repo_config: Optional[Dict[str, Any]] = None) -> None:
        """Analyze changes and post comments

        Args:
            git_client: Repository client
            pr: Pull request
            file_changes: List of file changes
            comment_analysis: Analysis of existing PR comments and conversations
            is_update: Whether this is an update to a previously reviewed PR
            previous_head_commit: Previous head commit hash for comparison
            repo_config: Repository configuration (used to pick the Goby/
                Jira instance via goby_instance: / jira_instance:, since
                different repos track tickets in different places)
        """
        files_requiring_attention = []

        # Resolved once per PR (not per file) -- title/description never
        # changes across files, and a linked ticket applies to the
        # whole PR, not any single file.
        try:
            ticket_context = self._get_ticket_context(pr, repo_config)
        except Exception as e:
            logger.warning(f"Error resolving ticket context for PR #{pr.id}: {e}")
            ticket_context = None
        pr_context_block = self._build_pr_context_block(pr, ticket_context)
        if ticket_context:
            logger.info(
                f"PR #{pr.id}: linked ticket {ticket_context['key']} "
                f"({ticket_context.get('status')}, source={ticket_context.get('source', 'unknown')})"
            )
        
        # First pass: analyze each file individually to collect initial findings
        # Track findings across files to avoid repetitive remarks
        all_findings = set()
        initial_findings = {}  # Store raw findings by file
        file_patterns = {}     # Track patterns of similar files
        
        # Group similar files to provide context
        for change in file_changes:
            if not change.patch:
                continue
                
            # Group files by extension and similar paths
            ext = os.path.splitext(change.filename)[1]
            components = change.filename.split('/')
            
            # Create a pattern key using extension and directory structure
            pattern_key = f"{ext}:{'/'.join(components[:-1])}"
            
            if pattern_key not in file_patterns:
                file_patterns[pattern_key] = []
            file_patterns[pattern_key].append(change)
            
            # Parse the diff to extract line numbers for the changed lines
            # This will be used to map comments to specific lines
            try:
                change.line_map = self._extract_line_mapping(change.patch)
            except Exception as e:
                logger.warning(f"Error extracting line mapping for {change.filename}: {e}")
                change.line_map = {}
        
        # Process each file for initial analysis
        for change in file_changes:
            if not change.patch:
                logger.warning(f"No diff found for file {change.filename}")
                continue
            
            logger.info(f"Analyzing changes in {change.filename}")
            try:
                logger.info(f"Starting analysis of {change.filename}")
                
                # Create context provider for enhanced context
                context_provider = ContextProvider(git_client)
                
                # Get enhanced context for the file
                try:
                    # Log more details about what we're doing
                    logger.info(f"Getting enhanced context for {change.filename} in PR #{pr.id}, head commit: {pr.head_commit}")
                    if isinstance(git_client, self._get_client_class("bitbucket")):
                        logger.info(f"Using Bitbucket client for {change.filename} in PR #{pr.id}")
                    
                    # Get the context
                    logger.info(f"About to get file context for {change.filename} with PR ID: {pr.id}")
                    file_context = context_provider.get_file_context(change.filename, pr.head_commit, pr.id)
                    
                    # Log success with some details about what we got
                    content_size = len(file_context.get("full_content", "")) if file_context.get("full_content") else 0
                    imports_count = len(file_context.get("imports", []))
                    logger.info(f"Retrieved enhanced context for {change.filename}: content size: {content_size}, imports: {imports_count}")
                except Exception as e:
                    logger.warning(f"Failed to get enhanced context for {change.filename}: {e}")
                    file_context = {"filename": change.filename}
                    
                # If we failed to get file content but it's not a binary file (which we don't expect content for),
                # log a detailed warning as this may indicate PR branch access issues
                if not file_context.get("full_content") and self.is_text_file(change.filename):
                    logger.warning(
                        f"No content retrieved for text file {change.filename} in PR #{pr.id}. "
                        f"This may indicate issues accessing files in the PR branch. "
                        f"PR details: head_commit={pr.head_commit}, head_branch={pr.head_branch}, base_branch={pr.base_branch}"
                    )
                
                # Create a prompt with enhanced context and explicit line number instructions
                analysis_prompt = "Review this code and provide detailed feedback with line numbers. "
                analysis_prompt += "For each issue you find, start with the specific line number that has the issue in the format: [Line X] "
                analysis_prompt += "If you don't find any issues, respond with 'NO_ISSUES_FOUND'. "
                analysis_prompt += "IMPORTANT: Be extremely careful about making definitive statements about code usage or behavior. "

                # PR title/description and (if linked) Goby/Jira ticket context,
                # resolved once above -- gives the model the "why" behind
                # this diff, not just the diff itself.
                analysis_prompt += pr_context_block

                # Now we have more context, so we can be more confident in our analysis
                analysis_prompt += "The enhanced context provided includes the full file and related code, "
                analysis_prompt += "which should help you provide more accurate feedback. "
                analysis_prompt += "However, still avoid absolute claims when uncertain. "
                
                # Add context about file status
                if change.is_recent_change and is_update:
                    analysis_prompt += "This file was recently modified in the latest commit. "
                    analysis_prompt += "While reviewing the entire file, pay special attention to recent changes."
                elif is_update:
                    analysis_prompt += "This file is part of the PR but hasn't changed in the most recent commit. "
                    analysis_prompt += "Review it for context and any issues that might affect the overall PR."
                
                # Add full file content if available (capped — see MAX_FILE_CONTENT_CHARS)
                full_content = file_context.get("full_content")
                if full_content:
                    analysis_prompt += "\n\n### Full File Content\n"
                    analysis_prompt += "```\n"
                    analysis_prompt += self._cap_file_content_for_prompt(
                        full_content, change.patch, change.filename
                    )
                    analysis_prompt += "\n```\n"
                
                # Add import information if available
                if file_context.get("imports") and len(file_context["imports"]) > 0:
                    analysis_prompt += "\n\n### Related Imports\n"
                    for imp in file_context["imports"]:
                        if imp.get("type") == "import":
                            analysis_prompt += f"- import {imp.get('module')}"
                            if imp.get("alias"):
                                analysis_prompt += f" as {imp.get('alias')}"
                            analysis_prompt += "\n"
                        elif imp.get("type") == "from_import":
                            analysis_prompt += f"- from {imp.get('module')} import {imp.get('name')}"
                            if imp.get("alias"):
                                analysis_prompt += f" as {imp.get('alias')}"
                            analysis_prompt += "\n"
                
                # Add class information if available
                if file_context.get("class_info") and file_context["class_info"].get("classes"):
                    analysis_prompt += "\n\n### Class Information\n"
                    for cls in file_context["class_info"].get("classes", []):
                        analysis_prompt += f"- Class: {cls.get('name')}"
                        if cls.get("parent_classes"):
                            parents = ", ".join(cls.get("parent_classes"))
                            analysis_prompt += f" (inherits from: {parents})"
                        analysis_prompt += "\n"
                    
                    # Add methods if available
                    if file_context["class_info"].get("methods"):
                        analysis_prompt += "\nMethods:\n"
                        for method in file_context["class_info"].get("methods", []):
                            analysis_prompt += f"- {method}\n"
                
                # Add related files if available
                if file_context.get("related_files") and len(file_context["related_files"]) > 0:
                    analysis_prompt += "\n\n### Related Files\n"
                    for related_file in file_context["related_files"][:5]:  # Limit to 5 related files
                        analysis_prompt += f"- {related_file}\n"
                
                # Add file history if available
                if file_context.get("history") and len(file_context["history"]) > 0:
                    analysis_prompt += "\n\n### Recent File History\n"
                    for commit in file_context["history"][:3]:  # Limit to 3 most recent commits
                        commit_msg = commit.get("message", "").split('\n')[0]  # First line of commit message
                        author = commit.get("author", "")
                        date = commit.get("date", "")
                        analysis_prompt += f"- {date[:10]} by {author}: {commit_msg}\n"
                
                # Analyze incremental changes and build focused conversation context
                incremental_analysis = self.analyze_incremental_changes(git_client, pr, comment_analysis, change.filename)
                newly_changed_lines = self.extract_changed_line_numbers(change.patch)
                
                # Log incremental analysis results
                if incremental_analysis['should_focus_review']:
                    logger.info(f"File {change.filename}: Incremental review mode - "
                               f"{len(newly_changed_lines)} newly changed lines, "
                               f"{len(incremental_analysis['previously_commented_lines'])} previously commented lines, "
                               f"{len(incremental_analysis['resolved_comment_lines'])} resolved")
                else:
                    logger.info(f"File {change.filename}: Standard review mode - no previous AI comments")
                
                # Use focused conversation context if we have previous comments
                if incremental_analysis['should_focus_review'] and newly_changed_lines:
                    conversation_context = self.build_focused_conversation_context(
                        comment_analysis, change.filename, newly_changed_lines)
                    
                    # Add information about what lines changed
                    if conversation_context:
                        analysis_prompt += f"\n\n{conversation_context}\n"
                        analysis_prompt += f"\n### Current Changes Summary:\n"
                        analysis_prompt += f"- Lines modified in this change: {sorted(list(newly_changed_lines))}\n"
                        analysis_prompt += f"- Previously commented lines: {sorted(list(incremental_analysis['previously_commented_lines']))}\n"
                        analysis_prompt += f"- Resolved comment lines: {sorted(list(incremental_analysis['resolved_comment_lines']))}\n"
                        
                        logger.debug(f"File {change.filename}: Using focused conversation context ({len(conversation_context)} chars)")
                else:
                    # Fallback to standard conversation context for first-time reviews
                    conversation_context = self.build_conversation_context_for_llm(comment_analysis, change.filename)
                    if conversation_context:
                        analysis_prompt += "\n\n" + conversation_context + "\n"
                        logger.debug(f"File {change.filename}: Using standard conversation context ({len(conversation_context)} chars)")
                    else:
                        logger.debug(f"File {change.filename}: No conversation context - clean review")
                
                # Add the patch last for analysis
                analysis_prompt += "\n\n### Changes to Review (Diff)\n"
                analysis_prompt += "```diff\n"
                analysis_prompt += change.patch
                analysis_prompt += "\n```\n"
                
                # Add specific instructions for the review with emphasis on line numbers
                analysis_prompt += "\nPlease analyze the code changes considering the full context provided. "
                analysis_prompt += "IMPORTANT: For each issue found, start your comment with the exact line number in [Line X] format. "
                analysis_prompt += "Your assessment should consider how these changes interact with the rest of the file and related code. "
                analysis_prompt += "Line numbers should refer to the new state of the file after changes."
                
                # Use the enhanced context for analysis
                context_obj = {"file_context": file_context}
                remarks = self.llm.analyze_code(analysis_prompt, change.filename, context=context_obj)

                # Only a truncated review benefits from a retry: the large
                # full-file context can exhaust the model's output budget, so a
                # leaner prompt may succeed. API errors are not prompt-related,
                # and NO_ISSUES_FOUND is a finished verdict -- retrying either
                # just multiplies latency and cost (and a context-free retry
                # can contradict the full-context "clean" verdict).
                status = self._classify_llm_response(
                    remarks, getattr(self.llm, "last_finish_reason", None))

                if status == "incomplete":
                    logger.warning(f"Incomplete response for {change.filename}. Retrying with a simpler prompt.")
                    simple_prompt = "Review this code diff and specify line numbers in format [Line X]:\n\n"
                    remarks = self.llm.analyze_code(simple_prompt + change.patch, change.filename)
                    status = self._classify_llm_response(remarks)

                if status == "clean":
                    logger.info(f"No issues found in {change.filename}")
                    continue

                # If the model still couldn't produce a usable review, skip the
                # file. A per-file "couldn't review" placeholder adds no value
                # and clutters the PR with identical noise comments. A retry
                # that is merely long-but-truncated is still kept: partial
                # line comments beat none.
                if status == "error" or len(remarks.strip()) < 20:
                    logger.error(
                        f"Skipping {change.filename}: no usable review from model "
                        f"({remarks.strip()[:200]})"
                    )
                    continue

                # Store the initial findings for this file
                initial_findings[change.filename] = remarks
                
            except Exception as e:
                logger.error(f"Error analyzing {change.filename}: {e}")
                initial_findings[change.filename] = f"Error analyzing file: {str(e)}"
        
        # Second pass: process findings and detect patterns across similar files
        file_comments = {}  # Store comments by file
        common_patterns = {}  # Track common issues across similar files
        
        # Identify common patterns across files in the same group
        for pattern_key, similar_files in file_patterns.items():
            if len(similar_files) < 2:
                continue  # Skip patterns with only one file
                
            # Extract all findings from files in this pattern group
            pattern_findings = {}
            for change in similar_files:
                if change.filename not in initial_findings:
                    continue
                    
                findings = initial_findings[change.filename]
                if not findings or "NO_ISSUES_FOUND" in findings:
                    continue
                    
                # Parse findings into discrete issues
                for paragraph in findings.split('\n\n'):
                    if not paragraph.strip():
                        continue
                    normalized = ' '.join(paragraph.lower().split())
                    if len(normalized) > 30:
                        if normalized not in pattern_findings:
                            pattern_findings[normalized] = []
                        pattern_findings[normalized].append(change.filename)
            
            # Identify findings that appear in multiple files
            for finding, files in pattern_findings.items():
                if len(files) >= 2:  # Finding appears in 2+ files
                    common_patterns[finding] = files
        
        # Process each file's findings to extract line-specific comments
        for change in file_changes:
            if change.filename not in initial_findings:
                continue
                
            remarks = initial_findings[change.filename]
            
            # Skip files where LLM found no issues
            if not remarks or "NO_ISSUES_FOUND" in remarks or any(phrase in remarks.lower() for phrase in [
                "no issues found", "no recommendations", "the code looks good", 
                "no issues or recommendations", "no problems", "looks fine",
                "no significant issues", "no significant changes", "no significant problems",
                "no significant concerns", "no significant improvements"
            ]):
                logger.info(f"No issues found in {change.filename}, skipping")
                continue
            
            # Initialize comments for this file
            file_comments[change.filename] = []
            files_requiring_attention.append(change.filename)
            
            # Parse line-specific comments
            comments = self._extract_line_comments(remarks, change.line_map, change.filename, common_patterns)
            
            # Add all extracted comments
            if comments:
                file_comments[change.filename].extend(comments)
                logger.info(f"Extracted {len(comments)} line-specific comments for {change.filename}")
            else:
                # If no line-specific comments were found, treat the whole content as a general comment
                logger.info(f"No line-specific comments found for {change.filename}, using as general comment")
                summary = remarks.split('\n\n')[0] if '\n\n' in remarks else remarks
                if len(summary) > 100:
                    summary = summary[:100] + "..."
                
                # Add as a general file comment (at first changed line or line 1)
                default_line = 1
                for line_num in sorted(change.line_map.values()):
                    if line_num > 0:
                        default_line = line_num
                        break
                
                file_comments[change.filename].append({
                    "line": default_line,
                    "body": remarks,
                    "is_general": True
                })
        
        # Get the head commit to attach comments to
        commit_id = pr.head_commit
        if not commit_id:
            logger.warning(f"No head commit found for PR #{pr.id}, using default reference")
            # Try to fetch a commit ID from the PR
            try:
                # This is a simplified attempt to get a commit ID
                commit_id = "HEAD"
            except Exception as e:
                logger.warning(f"Error getting commit ID: {e}")
                commit_id = "HEAD"
        
        # Post a summary comment with files requiring attention
        summary_remarks = []
        
        # Add header for updated PRs
        if is_update:
            update_header = "## 🔄 Updated PR Review\n\n"
            update_header += "This PR has been updated since the last review. "
            update_header += "This review examines ALL files in the PR, providing a comprehensive analysis. "
            update_header += "Files marked \"Recently modified\" have changes in the latest commit, while others are included for context.\n\n"
            summary_remarks.append(update_header)
        
        # Add disclaimer about accuracy
        accuracy_disclaimer = (
            "## ⚠️ Important Note\n\n"
            "This review analyzes the code with enhanced context, including full file content and related files. "
            "While this provides a more complete understanding than just the diff, there may still be broader "
            "codebase context not available for analysis. Please verify any observations about code usage or behavior "
            "that seem inconsistent with your knowledge of the codebase."
        )
        summary_remarks.append(accuracy_disclaimer)
        
        # Add summary of files needing attention
        if files_requiring_attention:
            files_summary = "## 📝 Files Requiring Attention\n\n"
            files_summary += "The following files have comments that you should review:\n\n"
            for filename in files_requiring_attention:
                comment_count = len(file_comments.get(filename, []))
                files_summary += f"- `{filename}` ({comment_count} comment{'s' if comment_count != 1 else ''})\n"
            files_summary += "\nPlease check the file-level comments for detailed feedback on each file."
            summary_remarks.append(files_summary)
        else:
            # For PRs with no issues found
            no_issues_message = "## ✅ No Issues Found\n\n"
            no_issues_message += "I've reviewed all changes in this PR and found no issues or concerns. "
            no_issues_message += "The code changes look good and can be merged. 👍"
            summary_remarks.append(no_issues_message)

        # Optional flow diagram of the changed code (off unless flow_diagram:
        # is enabled). Best effort: a failure here never blocks the review.
        try:
            diagram_config = resolve_flow_diagram_config(
                self.config.get_flow_diagram_config(), repo_config
            )
            diagram_section = generate_flow_diagram_section(
                self.llm, pr_context_block, file_changes, diagram_config
            )
            if diagram_section:
                summary_remarks.append(diagram_section)
                logger.info(f"Added flow diagram to summary comment for PR #{pr.id}")
        except Exception as e:
            logger.warning(f"Error adding flow diagram for PR #{pr.id}: {e}")

        # Post the summary comment
        summary_body = "\n\n".join(summary_remarks)
        logger.info(f"Posting summary comment to PR #{pr.id} (length: {len(summary_body)})")
        
        # Form the complete comment with template
        comment_template = self.config.get_comment_template()
        
        # For updates, adjust the template to indicate this is a follow-up review
        if is_update:
            comment_body = comment_template.format(remarks=summary_body).replace(
                "### 🤖 AI Code Review", 
                "### 🤖 AI Code Review (Updated PR)"
            )
        else:
            comment_body = comment_template.format(remarks=summary_body)
        
        # Post the main summary comment
        git_client.add_comment(pr.id, comment_body)
        logger.info(f"Posted summary comment to PR #{pr.id}")
        
        # Post file-level comments for each file
        comment_batch = []
        for filename, comments in file_comments.items():
            logger.info(f"Posting {len(comments)} comments for {filename}")
            
            for comment in comments:
                try:
                    # Check if this is a general file comment
                    if comment.get("is_general"):
                        # For general comments, use a consolidated comment
                        body = comment["body"]
                        if is_update:
                            if any(change.filename == filename and change.is_recent_change for change in file_changes):
                                body = f"*Recently modified in latest commit*\n\n{body}"
                            else:
                                body = f"*Part of PR, unchanged in latest commit*\n\n{body}"
                    else:
                        # For line-specific comments, use the body as is
                        body = comment["body"]
                    
                    # Add the comment to the batch
                    comment_batch.append({
                        "filename": filename,
                        "line": comment["line"],
                        "body": body
                    })
                    
                except Exception as e:
                    logger.error(f"Error preparing comment for {filename}: {e}")
        
        # Post all comments with rate limiting and duplicate checks
        # First, check for existing comments to avoid duplicates
        try:
            existing_comments = self._get_existing_comments(git_client, pr.id)
            logger.info(f"Found {len(existing_comments)} existing comments")
        except Exception as e:
            logger.warning(f"Failed to get existing comments, proceeding without duplicate checking: {e}")
            existing_comments = []
        
        # Group comments by filename for file-level duplicate detection
        comments_by_file = {}
        for comment in comment_batch:
            filename = comment["filename"]
            if filename not in comments_by_file:
                comments_by_file[filename] = []
            comments_by_file[filename].append(comment)
        
        # Process each file's comments through Claude for intelligent duplicate detection
        final_comments_to_post = []
        for filename, file_comments in comments_by_file.items():
            logger.info(f"Processing {len(file_comments)} comments for file {filename} through duplicate detection")
            non_duplicate_comments = self._filter_duplicate_comments_for_file(
                filename, file_comments, existing_comments
            )
            final_comments_to_post.extend(non_duplicate_comments)
            logger.info(f"After duplicate filtering: {len(non_duplicate_comments)}/{len(file_comments)} comments retained for {filename}")
        
        logger.info(f"Final comment count after duplicate detection: {len(final_comments_to_post)}/{len(comment_batch)} comments to post")
        
        # Keep track of which files/lines we've commented on to avoid duplicates
        commented_lines = set()
        
        # Post comments with proper rate limiting
        for i, comment in enumerate(final_comments_to_post):
            # Skip if we've already commented on this file/line combination (safety check)
            file_line_key = f"{comment['filename']}:{comment['line']}"
            if file_line_key in commented_lines:
                logger.info(f"Skipping duplicate comment for {file_line_key}")
                continue
                
            # Check if this comment would be a duplicate of an existing comment.
            # Note: exact-location duplicates are already handled by the
            # content-aware _filter_duplicate_comments_for_file() call above,
            # which reads the actual text rather than just file:line. A
            # second, location-only check used to live here and veto ANY new
            # comment at a line that already had an unresolved comment --
            # regardless of whether it was about the same issue -- which
            # could permanently blind the bot to new, unrelated issues at a
            # previously-flagged line.
            is_duplicate = False
            for existing in existing_comments:
                # Skip resolved comments when checking for duplicates
                if existing.get("resolved", False) or existing.get("state") == "resolved":
                    continue

                # Check for content similarity on the same file (even different lines)
                if (existing.get("path") == comment["filename"] and
                    self._is_similar_content(existing.get("body", ""), comment.get("body", ""))):
                    logger.info(f"Skipping similar content comment on {comment['filename']} (similarity detected)")
                    is_duplicate = True
                    break

            if is_duplicate:
                continue
                
            # Add exponential backoff for rate limiting with reduced delays
            max_retries = 3
            retry_count = 0
            retry_delay = 2  # Start with 2 seconds (reduced from 5)
            success = False
            
            while not success and retry_count < max_retries:
                try:
                    if retry_count > 0:
                        logger.info(f"Retry #{retry_count} for comment on {comment['filename']} at line {comment['line']}")
                        # Exponential backoff
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Double the delay for next retry
                    
                    git_client.add_review_comment(
                        pr.id,
                        commit_id,
                        comment["filename"],
                        comment["body"],
                        comment["line"]
                    )
                    logger.info(f"Posted comment on {comment['filename']} at line {comment['line']} ({i+1}/{len(final_comments_to_post)})")
                    success = True
                    commented_lines.add(file_line_key)
                    
                    # Smart rate limiting based on API limits
                    # Bitbucket allows up to 1000-10000 requests per hour for repository data
                    # This means we can be more aggressive with comment posting
                    if (i + 1) % 10 == 0:  # Check every 10 comments instead of 5
                        logger.info(f"Rate limiting pause for 2 seconds after posting {i+1} comments")
                        time.sleep(2)  # Reduced from 10 seconds to 2 seconds
                    else:
                        time.sleep(0.5)  # Reduced from 1 second to 0.5 seconds
                        
                except Exception as e:
                    if "429" in str(e):  # Rate limit error
                        logger.warning(f"Rate limit hit for comment on {comment['filename']}: {e}")
                        retry_count += 1
                        # Extended backoff for rate limits
                        if retry_count < max_retries:
                            logger.info(f"Waiting {retry_delay} seconds before retry #{retry_count+1}")
                        else:
                            logger.error(f"Max retries reached, falling back to regular comment")
                    else:
                        # Non-rate limit error, don't retry
                        logger.error(f"Error posting comment on {comment['filename']} at line {comment['line']}: {e}")
                        retry_count = max_retries
            
            # If all retries failed, use fallback
            if not success:
                # Try to post as a regular comment
                fallback_body = f"**Comment for {comment['filename']} (line {comment['line']}):**\n\n{comment['body']}"
                try:
                    git_client.add_comment(pr.id, fallback_body)
                    logger.info(f"Posted fallback comment for {comment['filename']}")
                    commented_lines.add(file_line_key)
                except Exception as e2:
                    logger.error(f"Error posting fallback comment: {e2}")
                    # If we hit rate limits on the fallback too, take a shorter break
                    if "429" in str(e2):
                        logger.warning(f"Rate limit hit on fallback comment, pausing for 30 seconds")
                        time.sleep(30)  # Reduced from 5 minutes to 30 seconds
    
    def _extract_line_mapping(self, patch: str) -> Dict[int, int]:
        """Extract a mapping from diff line numbers to actual file line numbers
        
        Args:
            patch: The diff patch string
            
        Returns:
            Dictionary mapping diff line numbers to actual file line numbers
        """
        line_map = {}
        current_line = 0
        file_line = 0
        
        # Parse the diff to extract line numbers
        for i, line in enumerate(patch.split('\n')):
            # Parse hunk headers to get line numbers
            if line.startswith('@@'):
                # Extract line numbers from hunk header
                # Format is like @@ -1,7 +1,7 @@
                try:
                    # Extract the new file line number
                    parts = line.split(' ')
                    if len(parts) >= 3:
                        new_file_part = parts[2]
                        line_info = new_file_part.split(',')[0].lstrip('+')
                        file_line = int(line_info)
                        current_line = i + 1  # +1 because line numbers start at 1
                except (ValueError, IndexError):
                    continue
            elif not line.startswith('---') and not line.startswith('+++'):
                # Map the current diff line to the file line
                line_map[current_line] = file_line
                current_line += 1
                
                # Update file line based on line type
                if not line.startswith('-'):  # Added or context line
                    file_line += 1
        
        return line_map
    
    def _extract_line_comments(self, remarks: str, line_map: Dict[int, int], filename: str,
                               common_patterns: Dict[str, List[str]]) -> List[Dict]:
        """Extract line-specific comments from analysis remarks
        
        Args:
            remarks: Analysis remarks text
            line_map: Mapping from diff lines to file lines
            filename: Current filename
            common_patterns: Common patterns detected across files
            
        Returns:
            List of comment dictionaries with line number and body
        """
        comments = []
        
        # Look for patterns like [Line X] or (Line X) or Line X:
        import re
        line_patterns = [
            r'\[Line\s+(\d+)\]',  # [Line X]
            r'\(Line\s+(\d+)\)',  # (Line X)
            r'Line\s+(\d+):',     # Line X:
            r'line\s+(\d+)[:\s]'  # line X: or line X (case insensitive)
        ]
        
        # Split remarks into sections separated by blank lines or headers
        sections = []
        current_section = []
        
        for line in remarks.split('\n'):
            # If blank line, end current section
            if not line.strip():
                if current_section:
                    sections.append('\n'.join(current_section))
                    current_section = []
            # If it's a markdown header, handle as a new section
            elif line.strip().startswith('#'):
                if current_section:
                    sections.append('\n'.join(current_section))
                    current_section = []
                current_section.append(line)
            else:
                current_section.append(line)
        
        # Don't forget the last section
        if current_section:
            sections.append('\n'.join(current_section))
        
        # Process each section
        for section in sections:
            found_line = False
            for pattern in line_patterns:
                matches = re.findall(pattern, section)
                if matches:
                    found_line = True
                    line_number = int(matches[0])
                    
                    # Get cleaned comment text
                    comment_body = section
                    for line_pat in line_patterns:
                        comment_body = re.sub(line_pat, '', comment_body, count=1)
                    comment_body = comment_body.strip()
                    
                    # Check if this is part of a common pattern
                    is_common_pattern = False
                    normalized_text = ' '.join(comment_body.lower().split())
                    
                    for pattern, files in common_patterns.items():
                        if normalized_text and len(normalized_text) > 30:
                            similarity = self._calculate_similarity(normalized_text, pattern)
                            if similarity > 0.75 and filename in files:
                                is_common_pattern = True
                                
                                # Only include the pattern in the first file
                                if files[0] == filename:
                                    # Add note about the pattern appearing in multiple files
                                    other_files = [f for f in files if f != filename]
                                    if other_files:
                                        comment_body += f"\n\n*Note: This issue also appears in: {', '.join(other_files)}*"
                                        comments.append({
                                            "line": line_number,
                                            "body": comment_body,
                                            "is_general": False
                                        })
                                break
                    
                    # If not a common pattern or it's the first file with the pattern
                    if not is_common_pattern or filename == common_patterns.get(normalized_text, [filename])[0]:
                        comments.append({
                            "line": line_number,
                            "body": comment_body,
                            "is_general": False
                        })
                    
                    break
            
            # If no line number found but section is meaningful, treat as general comment
            if not found_line and len(section.strip()) > 50:
                # Check if it contains [General] or similar marker
                if re.search(r'\b(general|overall|summary)\b', section.lower()):
                    comments.append({
                        "line": 1,  # Default to line 1 for general comments
                        "body": section,
                        "is_general": True
                    })
        
        return comments
    
    def _get_existing_comments(self, git_client: BaseRepoClient, pr_id: Any) -> List[Dict[str, Any]]:
        """Get existing comments on a pull request, normalized for duplicate-comment checks.

        Delegates to the client's own get_pull_request_comments(), which every
        client (github/gitlab/bitbucket) already implements with proper
        pagination. This used to hand-roll a single-page, per-platform HTTP
        call here instead: any PR with more than one page of comments lost
        the overflow silently, and any exception (a bare rate-limit hit
        included) was swallowed into an empty list -- indistinguishable from
        "no prior comments", which is exactly what drives reposting.

        Args:
            git_client: Repository client
            pr_id: Pull request ID

        Returns:
            List of existing comments with "path"/"line"/"body"/"resolved" keys
            (the shape callers here expect), normalized from the client's
            standardized "file_path"/"line_number" fields.
        """
        comments = git_client.get_pull_request_comments(pr_id)
        return [
            {
                "path": comment.get("file_path"),
                "line": comment.get("line_number"),
                "body": comment.get("body", ""),
                "resolved": comment.get("resolved", False),
                "state": "resolved" if comment.get("resolved") else "",
            }
            for comment in comments
        ]
        
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two text strings.
        
        Args:
            text1: First text string
            text2: Second text string
            
        Returns:
            Similarity score between 0 and 1
        """
        # Simple similarity calculation
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
            
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        # Jaccard similarity
        return len(intersection) / len(union)
        
    def _is_similar_content(self, text1: str, text2: str, threshold: float = 0.8) -> bool:
        """Check if two comment texts are similar enough to be considered duplicates.

        This is a cheap, non-LLM heuristic used as a safety net in the
        per-comment posting loop. The primary, context-aware duplicate
        detection already happens once per file in
        _filter_duplicate_comments_for_file (a single batched LLM call
        comparing all new comments against all existing comments on that
        file) — calling the LLM again here, per comment pair, would be a
        redundant O(existing * new) multiplier on top of that.

        Args:
            text1: First comment text
            text2: Second comment text
            threshold: Similarity threshold (0.0 to 1.0)

        Returns:
            True if texts are similar enough to be considered duplicates
        """
        if not text1.strip() or not text2.strip():
            return False

        if text1.strip() == text2.strip():
            return True

        similarity = self._calculate_similarity(text1, text2)
        return similarity >= threshold
        
    def _filter_duplicate_comments_for_file(self, filename: str, new_comments: List[Dict[str, Any]], existing_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Use Claude to intelligently filter duplicate comments for a specific file.
        
        Args:
            filename: The file these comments are for
            new_comments: List of new comments to be posted
            existing_comments: List of existing comments on the PR
            
        Returns:
            List of non-duplicate comments that should be posted
        """
        if not new_comments:
            return []
            
        # Get existing comments for this specific file
        file_existing_comments = [
            comment for comment in existing_comments 
            if comment.get("path") == filename and not comment.get("resolved", False)
        ]
        
        # If only one new comment and no existing comments for this file, no need to check
        if len(new_comments) == 1 and not file_existing_comments:
            return new_comments
            
        # If no LLM available, return all comments (fallback)
        if not self.llm:
            logger.warning(f"No LLM available for duplicate detection on {filename}, posting all {len(new_comments)} comments without filtering")
            return new_comments
            
        try:
            # Build the prompt for Claude
            prompt = f"""I need you to identify duplicate comments for the file: {filename}

I have {len(new_comments)} new comments to post and {len(file_existing_comments)} existing comments on this file.

EXISTING COMMENTS ON THIS FILE:
"""
            
            for i, existing in enumerate(file_existing_comments):
                line_info = f"Line {existing.get('line', '?')}" if existing.get('line') else "General comment"
                prompt += f"{i+1}. [{line_info}] {existing.get('body', '').strip()}\n"
            
            if not file_existing_comments:
                prompt += "(No existing comments on this file)\n"
                
            prompt += f"\nNEW COMMENTS TO POST:\n"
            
            for i, new_comment in enumerate(new_comments):
                line_info = f"Line {new_comment.get('line', '?')}" if new_comment.get('line') else "General comment"
                prompt += f"{i+1}. [{line_info}] {new_comment.get('body', '').strip()}\n"
                
            prompt += f"""

Please identify which NEW comments (if any) are duplicates and should NOT be posted. Consider:
- Comments with very similar content, even if on different lines
- Comments making the same point about the same issue
- Comments that are redundant given existing comments
- General comments that say essentially the same thing

Respond with ONLY the numbers of the NEW comments that should be REMOVED (duplicates).
If no duplicates, respond with "NONE".
For example: "2,4" means remove new comments 2 and 4.
"""

            # Send to Claude
            logger.info(f"Checking {len(new_comments)} new comments against {len(file_existing_comments)} existing comments for {filename}")
            response = self.llm.analyze_code(prompt, "duplicate_detection")
            logger.info(f"Claude duplicate detection response for {filename}: '{response.strip()}'")
            
            # Parse response
            response = response.strip().upper()
            if response == "NONE" or not response:
                # No duplicates found
                logger.info(f"Claude found no duplicates in {len(new_comments)} comments for {filename}")
                return new_comments
            
            # Parse comma-separated numbers
            try:
                duplicate_indices = []
                for num_str in response.split(','):
                    num_str = num_str.strip()
                    if num_str.isdigit():
                        # Convert to 0-based index
                        index = int(num_str) - 1
                        if 0 <= index < len(new_comments):
                            duplicate_indices.append(index)
                
                # Filter out duplicates
                filtered_comments = [
                    comment for i, comment in enumerate(new_comments) 
                    if i not in duplicate_indices
                ]
                
                if duplicate_indices:
                    logger.info(f"Claude identified {len(duplicate_indices)} duplicate comments for {filename}: {[i+1 for i in duplicate_indices]}")
                    logger.info(f"Retaining {len(filtered_comments)}/{len(new_comments)} comments for {filename}")
                
                return filtered_comments
                
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse Claude duplicate detection response for {filename}: '{response}' - {e}")
                return new_comments
                
        except Exception as e:
            logger.warning(f"Error in Claude duplicate detection for {filename}: {e}")
            return new_comments
        
    def is_text_file(self, filename: str) -> bool:
        """Determine if a file is a text file based on its extension
        
        Args:
            filename: File path
            
        Returns:
            True if the file is likely a text file, False otherwise
        """
        # Common text file extensions
        text_extensions = [
            # Code files
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".c", ".cpp", ".h", ".hpp",
            ".cs", ".php", ".rb", ".go", ".swift", ".m", ".scala", ".groovy", ".pl", ".pm",
            ".sh", ".bash", ".fish", ".zsh", ".rs", ".r", ".sql", ".lua", ".dart", ".elm",
            ".erl", ".ex", ".exs", ".hs", ".fs", ".fsx", ".clj", ".cljs", ".cljc", ".tcl",
            ".vb", ".ps1", ".psm1", ".psd1", ".tf", ".tfvars",
            
            # Web related
            ".html", ".htm", ".css", ".scss", ".sass", ".less", ".xml", ".xhtml", ".svg",
            ".json", ".yml", ".yaml", ".toml", ".ini", ".config", ".conf",
            
            # Documentation
            ".md", ".markdown", ".txt", ".rst", ".adoc", ".tex", ".rtf",
            
            # Generic data
            ".csv", ".tsv"
        ]
        
        # Check if file has a text extension
        file_ext = os.path.splitext(filename)[1].lower()
        return file_ext in text_extensions
    
    def _post_comment(self, git_client: BaseRepoClient, pr: PullRequest, remarks: str) -> None:
        """Post comments to a pull request
        
        Args:
            git_client: Repository client
            pr: Pull request
            remarks: Comment text
        """
        # This method is deprecated - we now use direct client calls with templates
        # Keep for backwards compatibility
        comment_template = self.config.get_comment_template()
        comment_body = comment_template.format(remarks=remarks)
        
        logger.warning("Using deprecated _post_comment method - consider updating code to use direct client calls")
        git_client.add_comment(pr.id, comment_body)
    
    def _get_processed_file_path(self, repo_name: str) -> str:
        """Get the file path for storing processed PRs for a specific repository
        
        Args:
            repo_name: Repository name
            
        Returns:
            Path to the processed PRs file for the repository
        """
        # Create a safe filename from the repo_name by replacing slashes and other unsafe chars
        safe_name = repo_name.replace('/', '_').replace('\\', '_').replace(':', '_')
        
        # Ensure processed_prs directory exists
        processed_dir = "processed_prs"
        if not os.path.exists(processed_dir):
            os.makedirs(processed_dir)
        
        file_path = os.path.join(processed_dir, f"{safe_name}.json")

        # Create an empty file if it doesn't exist
        if not os.path.exists(file_path):
            self._atomic_write_json(file_path, [])
            logger.debug(f"Created empty repository file: {file_path}")

        return file_path

    def _atomic_write_json(self, path: str, data: Any) -> None:
        """Write JSON to `path` atomically.

        Writes to a temp file in the same directory, then os.replace()s it
        into place. watchdog.sh does SIGKILL this process on occasion; a
        kill mid write previously left a truncated/corrupt JSON file, which
        the next read treated as "no processed PRs" and caused the whole
        PR (including its top-level summary comment, which has no dedup
        check) to be reprocessed from scratch. With this, the real path
        always holds either the old complete contents or the new complete
        contents, never a partial write -- a kill can only ever orphan the
        temp file.
        """
        directory = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked_processed_file(self, path: str):
        """Serialize read-modify-write access to a processed-PR file.

        Guards against two overlapping reviewer processes (e.g. a watchdog
        restart racing an in-flight run) reading the same file, appending
        different PR entries, and one write clobbering the other -- which
        silently loses the "already processed" record for a PR and causes
        it to be reprocessed/re-summarized on the next poll.
        """
        lock_path = path + ".lock"
        with open(lock_path, 'w') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def should_review_pr_based_on_commits(self, git_client: BaseRepoClient, pr: 'PullRequest', is_first_review: bool = False, repo_config: Optional[Dict[str, Any]] = None) -> bool:
        """Check if PR should be reviewed based on commit messages
        
        Args:
            git_client: Repository client
            pr: Pull request object
            is_first_review: True if this is the first time reviewing this PR
            repo_config: Repository configuration with optional review trigger settings
            
        Returns:
            True if PR should be reviewed, False to skip
        """
        # Always review on first time (when PR is newly created)
        if is_first_review:
            logger.info(f"PR #{pr.id} is being reviewed for the first time, proceeding without commit message filtering")
            return True
        
        # Get review trigger from config or use default
        review_trigger = "(REVIEW)"  # Default
        if repo_config and "commit_filtering" in repo_config:
            review_trigger = repo_config["commit_filtering"].get("review_trigger", review_trigger)
        
        try:
            # Get commits for this PR
            commits = git_client.get_pull_request_commits(pr.id)
            
            if not commits:
                logger.warning(f"No commits found for PR #{pr.id}, skipping review")
                return False
            
            # Check if any recent commit contains the review trigger
            for commit in commits:
                message = commit.get('message', '')
                if review_trigger in message:
                    logger.info(f"PR #{pr.id} has commit with review trigger '{review_trigger}': {commit.get('sha', '')[:8]} - {message[:100]}...")
                    return True
            
            # No review trigger found
            logger.info(f"PR #{pr.id} has no commits with review trigger '{review_trigger}', skipping review")
            return False
            
        except Exception as e:
            logger.warning(f"Error checking commit messages for PR #{pr.id}: {e}")
            # On error, default to reviewing to be safe
            return True
    
    def analyze_pr_comment_history(self, git_client: BaseRepoClient, pr: 'PullRequest', authenticated_username: str, authenticated_display_name: str = None) -> Dict[str, Any]:
        """Analyze existing comments in a PR to build conversation context
        
        Args:
            git_client: Repository client
            pr: Pull request object
            authenticated_username: Username of the bot account (to identify AI comments)
            
        Returns:
            Dictionary with conversation analysis:
            - ai_comments: List of previous AI comments
            - developer_responses: List of developer responses to AI comments
            - resolved_issues: List of issues that have been resolved
            - unresolved_issues: List of issues that remain unresolved
            - file_comment_map: Map of file paths to their comment threads
        """
        try:
            # Fetch all comments for this PR
            all_comments = git_client.get_pull_request_comments(pr.id)
            logger.info(f"Retrieved {len(all_comments)} total comments for PR #{pr.id}")
            
            ai_comments = []
            developer_responses = []
            resolved_issues = []
            unresolved_issues = []
            file_comment_map = {}
            
            # Log first few comments to debug author field format
            if all_comments:
                logger.debug(f"Sample comment structure: {all_comments[0]}")
                authors = set(comment.get('author', 'UNKNOWN') for comment in all_comments[:5])
                logger.info(f"Comment authors found: {authors}")
                logger.info(f"Looking for authenticated username: '{authenticated_username}'")
                if authenticated_display_name:
                    logger.info(f"Looking for authenticated display name: '{authenticated_display_name}'")
            
            # Separate AI comments from developer comments
            for comment in all_comments:
                comment_author = comment.get('author', '')
                
                # Try multiple ways to identify AI comments
                is_ai_comment = (
                    comment_author == authenticated_username or
                    comment_author.lower() == authenticated_username.lower() or
                    # Check display name if provided
                    (authenticated_display_name and comment_author == authenticated_display_name) or
                    (authenticated_display_name and comment_author.lower() == authenticated_display_name.lower()) or
                    # Check for AI-generated comment signatures
                    any(signature in comment.get('body', '').lower() for signature in [
                        'generated with claude code',
                        'ai code review',
                        'automated review',
                        'pr reviewer bot'
                    ])
                )
                
                if is_ai_comment:
                    # This is our AI comment
                    ai_comments.append(comment)
                    
                    # Check if it's resolved
                    if comment.get('resolved', False):
                        resolved_issues.append(comment)
                    else:
                        unresolved_issues.append(comment)
                else:
                    # This is a developer or other user comment
                    developer_responses.append(comment)
                
                # Build file-based comment map
                file_path = comment.get('file_path')
                if file_path:
                    if file_path not in file_comment_map:
                        file_comment_map[file_path] = []
                    file_comment_map[file_path].append(comment)
            
            # Analyze conversation threads (AI comment + subsequent developer responses)
            conversation_threads = self._build_conversation_threads(ai_comments, developer_responses)
            
            return {
                'ai_comments': ai_comments,
                'developer_responses': developer_responses,
                'resolved_issues': resolved_issues,
                'unresolved_issues': unresolved_issues,
                'file_comment_map': file_comment_map,
                'conversation_threads': conversation_threads,
                'total_comments': len(all_comments),
                'total_ai_comments': len(ai_comments),
                'total_resolved': len(resolved_issues)
            }
            
        except Exception as e:
            logger.warning(f"Error analyzing comment history for PR #{pr.id}: {e}")
            return {
                'ai_comments': [],
                'developer_responses': [],
                'resolved_issues': [],
                'unresolved_issues': [],
                'file_comment_map': {},
                'conversation_threads': [],
                'total_comments': 0,
                'total_ai_comments': 0,
                'total_resolved': 0
            }
    
    def _build_conversation_threads(self, ai_comments: List[Dict[str, Any]], developer_responses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build conversation threads by matching AI comments with developer responses
        
        Args:
            ai_comments: List of AI comments
            developer_responses: List of developer responses
            
        Returns:
            List of conversation threads with AI comment and related responses
        """
        threads = []
        
        for ai_comment in ai_comments:
            thread = {
                'ai_comment': ai_comment,
                'responses': [],
                'has_developer_response': False,
                'appears_resolved': False
            }
            
            ai_created_at = ai_comment.get('created_at', '')
            ai_file_path = ai_comment.get('file_path')
            ai_line_number = ai_comment.get('line_number')
            
            # Find developer responses that could be related to this AI comment
            for dev_response in developer_responses:
                dev_created_at = dev_response.get('created_at', '')
                dev_file_path = dev_response.get('file_path')
                dev_line_number = dev_response.get('line_number')
                
                # Consider it related if:
                # 1. It's after the AI comment
                # 2. It's on the same file/line (for line comments) or general comment
                is_related = False
                
                if dev_created_at > ai_created_at:  # Response came after AI comment
                    if ai_file_path and dev_file_path:
                        # Both are line comments - check if same file/line
                        if (ai_file_path == dev_file_path and 
                            ai_line_number == dev_line_number):
                            is_related = True
                    elif not ai_file_path and not dev_file_path:
                        # Both are general comments
                        is_related = True
                
                if is_related:
                    thread['responses'].append(dev_response)
                    thread['has_developer_response'] = True
                    
                    # Check if developer indicated the issue was fixed
                    response_body = dev_response.get('body', '').lower()
                    thread['appears_resolved'] = self._detect_resolution_from_response(response_body)
            
            threads.append(thread)
        
        return threads
    
    def _detect_resolution_from_response(self, response_body: str) -> bool:
        """Detect if a developer response indicates an issue was resolved

        This is a heuristic keyword/pattern match, not real NLP. Two things
        it deliberately guards against, both observed as real false
        positives in production:
        - Substring matches: a plain `'done' in text` check matched inside
          "abandoned", and `'implemented' in text` matched inside
          "unimplemented" -- both misread as resolutions. Matching is now
          whole-word only.
        - Negation: "won't be fixed", "still not addressed", "not fixed
          yet" all previously matched their indicator word with no regard
          for the "not"/"won't" immediately in front of it. A match is now
          only counted if there's no negation word in the ~25 chars right
          before it. (This only catches negation preceding the indicator,
          which covers the common developer-reply phrasing -- it's not a
          general negation parser.)

        Args:
            response_body: Developer response text (already lowercased by the caller)

        Returns:
            True if response indicates resolution, False otherwise
        """
        import re

        # Strong resolution indicators
        strong_indicators = [
            'fixed', 'done', 'resolved', 'addressed', 'corrected', 'updated',
            'implemented', 'changed', 'added', 'removed', 'refactored'
        ]

        # Contextual resolution phrases. Deliberately excludes forward-looking
        # acknowledgments like "will fix", "fixing this", or "good catch" —
        # those confirm the developer *saw* the issue, not that it's fixed,
        # and were previously misclassified as resolved (masking genuinely
        # unfixed issues from future review passes).
        resolution_phrases = [
            'updated in', 'changed in', 'addressed in', 'fixed in',
            'commit', 'pushed', 'merge', 'pr updated'
        ]

        negation_re = re.compile(r"\b(not|never|no|n't|won't|wont|cant|can't)\b")

        def has_unnegated_match(term: str) -> bool:
            for match in re.finditer(r'\b' + re.escape(term) + r'\b', response_body):
                preceding = response_body[max(0, match.start() - 25):match.start()]
                if not negation_re.search(preceding):
                    return True
            return False

        if any(has_unnegated_match(term) for term in strong_indicators + resolution_phrases):
            return True

        # Commit hash references. "commit <hex>" is unambiguous on its own;
        # a bare hex-looking token needs at least one digit to rule out
        # ordinary words spelled entirely from a-f (facade, decade, cafe,
        # dead, beef...) -- one of those previously misread "this is a
        # facade for..." as a commit reference via \b[a-f0-9]{6,}\b.
        if re.search(r'\bcommit\s+[a-f0-9]{4,40}\b', response_body):
            return True
        for token in re.findall(r'\b[a-f0-9]{7,40}\b', response_body):
            if any(ch.isdigit() for ch in token):
                return True

        return False
    
    def detect_implicit_resolution(self, ai_comment: Dict[str, Any], current_file_content: str, changed_lines: set) -> bool:
        """Detect if an AI comment was implicitly resolved by code changes
        
        Args:
            ai_comment: Previous AI comment
            current_file_content: Current file content after changes
            changed_lines: Set of line numbers that were changed
            
        Returns:
            True if the issue appears to have been resolved in the code
        """
        try:
            comment_line = ai_comment.get('line_number')
            comment_body = ai_comment.get('body', '').lower()
            
            if not comment_line or not current_file_content:
                return False
            
            # Check if the line was modified
            if comment_line not in changed_lines:
                return False
            
            # Split file content into lines
            file_lines = current_file_content.split('\n')
            if comment_line > len(file_lines):
                return False
            
            current_line = file_lines[comment_line - 1].lower()  # Convert to 0-based index
            
            # Common issue patterns and their resolution indicators
            resolution_patterns = {
                'null check': ['null', '!==', '!=', 'if (', 'if(', 'optional', '?.'],
                'const': ['const '],
                'let': ['const '],
                'var ': ['const ', 'let '],
                'unused': [],  # Hard to detect, skip
                'missing': ['import', 'require', 'from'],
                'async': ['async', 'await'],
                'try': ['try', 'catch'],
                'return': ['return'],
                'semicolon': [';'],
                'bracket': ['{', '}', '[', ']', '(', ')'],
            }
            
            # Check if any resolution patterns are present for the identified issue type
            for issue_type, indicators in resolution_patterns.items():
                if issue_type in comment_body:
                    if any(indicator in current_line for indicator in indicators):
                        return True
            
            return False
            
        except Exception as e:
            logger.warning(f"Error detecting implicit resolution: {e}")
            return False
    
    def build_conversation_context_for_llm(self, comment_analysis: Dict[str, Any], file_path: str = None) -> str:
        """Build conversation context string for LLM prompt
        
        Args:
            comment_analysis: Result from analyze_pr_comment_history
            file_path: Optional file path to focus context on specific file
            
        Returns:
            Formatted string with conversation context for LLM
        """
        if not comment_analysis.get('ai_comments'):
            return ""
        
        context_parts = []
        context_parts.append("## Previous AI Review Comments on this PR:")
        
        # Get relevant conversation threads
        conversation_threads = comment_analysis.get('conversation_threads', [])
        
        # Filter threads for specific file if requested
        if file_path:
            relevant_threads = [
                thread for thread in conversation_threads
                if thread['ai_comment'].get('file_path') == file_path
            ]
            context_parts.append(f"\n### Comments for {file_path}:")
        else:
            relevant_threads = conversation_threads
            context_parts.append("\n### All Previous Comments:")
        
        if not relevant_threads:
            if file_path:
                return f"## Previous AI Review Comments:\nNo previous AI comments found for {file_path}.\n"
            else:
                return ""
        
        for i, thread in enumerate(relevant_threads, 1):
            ai_comment = thread['ai_comment']
            responses = thread['responses']
            
            # Format the AI comment
            comment_location = ""
            if ai_comment.get('file_path'):
                location_info = f"{ai_comment['file_path']}"
                if ai_comment.get('line_number'):
                    location_info += f":{ai_comment['line_number']}"
                comment_location = f" ({location_info})"
            
            context_parts.append(f"\n**{i}. AI Comment{comment_location}:**")
            context_parts.append(f"```\n{ai_comment.get('body', '')}\n```")
            
            # Add developer responses if any
            if responses:
                context_parts.append("**Developer Response(s):**")
                for response in responses:
                    author = response.get('author', 'Developer')
                    context_parts.append(f"- {author}: {response.get('body', '')}")
            
            # Add resolution status
            status_indicators = []
            if ai_comment.get('resolved', False):
                status_indicators.append("✅ Marked as resolved")
            if thread.get('appears_resolved', False):
                status_indicators.append("✅ Developer indicated fix")
            if thread.get('has_developer_response', False) and not status_indicators:
                status_indicators.append("💬 Developer responded")
            if not thread.get('has_developer_response', False):
                status_indicators.append("⏳ No developer response")
            
            if status_indicators:
                context_parts.append(f"**Status:** {', '.join(status_indicators)}")
        
        # Add summary
        total_ai_comments = comment_analysis.get('total_ai_comments', 0)
        total_resolved = comment_analysis.get('total_resolved', 0)
        
        context_parts.append(f"\n### Summary:")
        context_parts.append(f"- Total previous AI comments: {total_ai_comments}")
        context_parts.append(f"- Resolved comments: {total_resolved}")
        context_parts.append(f"- Unresolved comments: {total_ai_comments - total_resolved}")
        
        context_parts.append("\n**Instructions:**")
        context_parts.append("- DO NOT repeat comments about issues that are marked as resolved or where developers have indicated fixes")
        context_parts.append("- Focus on NEW issues not previously mentioned")
        context_parts.append("- If you see that previous concerns have been addressed, you may acknowledge the improvement")
        context_parts.append("- Only comment on unresolved issues if they still exist in the current code")
        
        return "\n".join(context_parts)
    
    def get_authenticated_username(self, git_client: BaseRepoClient, repo_config: Dict[str, Any]) -> str:
        """Get the authenticated username for the current repository client
        
        Args:
            git_client: Repository client
            repo_config: Repository configuration
            
        Returns:
            Username of the authenticated user (bot account)
        """
        repo_type = repo_config.get('type', '').lower()
        
        try:
            # Try to get the actual authenticated user from the API
            if hasattr(git_client, 'get_authenticated_user'):
                user_info = git_client.get_authenticated_user()
                if user_info and 'login' in user_info:
                    return user_info['login']
                elif user_info and 'username' in user_info:
                    return user_info['username']
        except Exception as e:
            logger.warning(f"Could not get authenticated user from API: {e}")
        
        # Fallback to configuration
        if repo_type == 'github':
            return repo_config.get('username', '')
        elif repo_type == 'gitlab':
            return repo_config.get('username', '')
        elif repo_type == 'bitbucket':
            return repo_config.get('username', '')
        
        return ''
        
    def get_authenticated_display_name(self, git_client: BaseRepoClient, repo_config: Dict[str, Any]) -> str:
        """Get the authenticated user's display name for the current repository client
        
        Args:
            git_client: Repository client
            repo_config: Repository configuration
            
        Returns:
            Display name of the authenticated user (bot account)
        """
        try:
            # Try to get the actual authenticated user from the API
            if hasattr(git_client, 'get_authenticated_user'):
                user_info = git_client.get_authenticated_user()
                if user_info and 'display_name' in user_info:
                    return user_info['display_name']
                elif user_info and 'name' in user_info:
                    return user_info['name']
        except Exception as e:
            logger.warning(f"Could not get authenticated user display name from API: {e}")
        
        # Fallback to configuration - check if display_name is configured
        display_name = repo_config.get('display_name', '')
        if display_name:
            return display_name
            
        return ''
    
    def analyze_incremental_changes(self, git_client: BaseRepoClient, pr: 'PullRequest', comment_analysis: Dict[str, Any], file_path: str) -> Dict[str, Any]:
        """Analyze which lines in a file are newly changed vs previously commented on
        
        Args:
            git_client: Repository client
            pr: Pull request object
            comment_analysis: Previous comment analysis
            file_path: Path to the file being analyzed
            
        Returns:
            Dictionary with incremental change analysis:
            - newly_changed_lines: Set of line numbers that are new changes
            - previously_commented_lines: Set of line numbers with previous AI comments
            - resolved_comment_lines: Set of line numbers with resolved comments
            - should_focus_review: Whether to limit review to new changes only
        """
        result = {
            'newly_changed_lines': set(),
            'previously_commented_lines': set(),
            'resolved_comment_lines': set(),
            'should_focus_review': False
        }
        
        try:
            # Get previous AI comments for this specific file
            file_comments = [
                thread['ai_comment'] for thread in comment_analysis.get('conversation_threads', [])
                if thread['ai_comment'].get('file_path') == file_path
            ]
            
            if not file_comments:
                # No previous comments on this file, review normally
                return result
            
            # Extract line numbers from previous comments
            for comment in file_comments:
                line_num = comment.get('line_number')
                if line_num:
                    result['previously_commented_lines'].add(line_num)
                    
                    # Check if this comment was resolved
                    if comment.get('resolved', False):
                        result['resolved_comment_lines'].add(line_num)
            
            # If we have previous comments, we should focus the review
            if result['previously_commented_lines']:
                result['should_focus_review'] = True
                logger.info(f"File {file_path} has {len(result['previously_commented_lines'])} previously commented lines, "
                           f"{len(result['resolved_comment_lines'])} resolved")
            
            return result
            
        except Exception as e:
            logger.warning(f"Error analyzing incremental changes for {file_path}: {e}")
            return result
    
    @staticmethod
    def _classify_llm_response(remarks: str, finish_reason: Optional[str] = None) -> str:
        """Classify a per-file LLM response.

        Returns one of:
            "error"      -- API failure ("Error:" prefix from the LLM layer)
            "clean"      -- the model answered NO_ISSUES_FOUND
            "incomplete" -- hit the output limit, or too short / trails off
            "ok"         -- a usable review
        """
        text = (remarks or "").strip()
        if text.startswith("Error:"):
            return "error"
        if NO_ISSUES_SENTINEL in text:
            return "clean"
        if finish_reason in TRUNCATED_FINISH_REASONS or len(text) < 20 or "..." in text[-10:]:
            return "incomplete"
        return "ok"

    def _cap_file_content_for_prompt(self, full_content: str, patch: Optional[str], filename: str) -> str:
        """Cap file content to MAX_FILE_CONTENT_CHARS for the review prompt.

        For files under the cap, returns the content unchanged. For larger
        files, centers the shown window on the lines the diff actually
        touched (falling back to the start of the file if the patch can't
        be parsed) so the LLM sees the changed region instead of always
        getting truncated content unrelated to the diff.

        Args:
            full_content: Full file content
            patch: Git diff patch for this file, if available
            filename: File path, used only for logging

        Returns:
            Content string to embed in the prompt, capped to roughly
            MAX_FILE_CONTENT_CHARS characters
        """
        if len(full_content) <= MAX_FILE_CONTENT_CHARS:
            return full_content

        lines = full_content.split('\n')
        changed_lines = self.extract_changed_line_numbers(patch) if patch else set()

        if changed_lines:
            center_line = (min(changed_lines) + max(changed_lines)) // 2
        else:
            center_line = 1  # No patch info — fall back to showing the start of the file

        center_idx = max(0, min(len(lines) - 1, center_line - 1))

        # Grow the window outward from the center line, one line at a time,
        # until adding the next line would exceed the char budget.
        start_idx = end_idx = center_idx
        total_chars = len(lines[center_idx])
        grow_up, grow_down = True, True
        while total_chars < MAX_FILE_CONTENT_CHARS and (grow_up or grow_down):
            if grow_down and end_idx + 1 < len(lines):
                next_len = len(lines[end_idx + 1]) + 1
                if total_chars + next_len > MAX_FILE_CONTENT_CHARS:
                    grow_down = False
                else:
                    end_idx += 1
                    total_chars += next_len
            else:
                grow_down = False

            if grow_up and start_idx - 1 >= 0:
                next_len = len(lines[start_idx - 1]) + 1
                if total_chars + next_len > MAX_FILE_CONTENT_CHARS:
                    grow_up = False
                else:
                    start_idx -= 1
                    total_chars += next_len
            else:
                grow_up = False

        shown = '\n'.join(lines[start_idx:end_idx + 1])
        logger.info(
            f"Truncating {filename} content for prompt: {len(full_content)} -> {len(shown)} chars "
            f"(lines {start_idx + 1}-{end_idx + 1} of {len(lines)}, centered on diff)"
        )

        result = ""
        if start_idx > 0:
            result += f"... [truncated: lines 1-{start_idx} omitted]\n"
        result += shown
        if end_idx < len(lines) - 1:
            result += f"\n... [truncated: lines {end_idx + 2}-{len(lines)} omitted; review the diff below in full]"
        return result

    def extract_changed_line_numbers(self, patch: str) -> set:
        """Extract line numbers that were actually changed in a diff patch

        Args:
            patch: Git diff patch content
            
        Returns:
            Set of line numbers that were added or modified
        """
        changed_lines = set()
        
        try:
            current_line = 0
            for line in patch.split('\n'):
                if line.startswith('@@'):
                    # Parse hunk header like "@@ -1,7 +1,8 @@"
                    parts = line.split()
                    if len(parts) >= 3:
                        new_range = parts[2]  # +1,8
                        if ',' in new_range:
                            start_line = int(new_range.split(',')[0][1:])  # Remove + sign
                        else:
                            start_line = int(new_range[1:])  # Remove + sign
                        current_line = start_line
                elif line.startswith('+') and not line.startswith('+++'):
                    # This is an added line
                    changed_lines.add(current_line)
                    current_line += 1
                elif line.startswith('-') and not line.startswith('---'):
                    # This is a removed line, don't increment current_line
                    pass
                elif not line.startswith('-'):
                    # This is a context line
                    current_line += 1
            
            return changed_lines
            
        except Exception as e:
            logger.warning(f"Error extracting changed line numbers from patch: {e}")
            return set()
    
    def build_focused_conversation_context(self, comment_analysis: Dict[str, Any], file_path: str, newly_changed_lines: set) -> str:
        """Build conversation context focused on lines that intersect with new changes
        
        Args:
            comment_analysis: Comment analysis results
            file_path: File path being reviewed
            newly_changed_lines: Set of line numbers that were newly changed
            
        Returns:
            Focused conversation context string
        """
        if not comment_analysis.get('ai_comments'):
            return ""
        
        # Get threads for this file
        file_threads = [
            thread for thread in comment_analysis.get('conversation_threads', [])
            if thread['ai_comment'].get('file_path') == file_path
        ]
        
        if not file_threads:
            return ""
        
        context_parts = []
        context_parts.append(f"## Previous AI Review Comments for {file_path}:")
        
        relevant_threads = []
        resolved_away_from_changes = []
        
        for thread in file_threads:
            ai_comment = thread['ai_comment']
            comment_line = ai_comment.get('line_number')
            
            if comment_line:
                # Check if this comment is near newly changed lines (within 5 lines)
                is_near_changes = any(abs(comment_line - changed_line) <= 5 for changed_line in newly_changed_lines)
                
                if is_near_changes:
                    relevant_threads.append(thread)
                elif thread.get('appears_resolved') or ai_comment.get('resolved'):
                    resolved_away_from_changes.append(thread)
        
        if relevant_threads:
            context_parts.append("\n### Comments Near Current Changes:")
            for i, thread in enumerate(relevant_threads, 1):
                ai_comment = thread['ai_comment']
                
                location_info = f"line {ai_comment.get('line_number', 'unknown')}"
                context_parts.append(f"\n**{i}. Previous AI Comment ({location_info}):**")
                context_parts.append(f"```\n{ai_comment.get('body', '')}\n```")
                
                if thread.get('appears_resolved') or ai_comment.get('resolved'):
                    context_parts.append("**Status:** ✅ Resolved - Do NOT re-comment on this issue")
                elif thread.get('has_developer_response'):
                    context_parts.append("**Status:** 💬 Developer responded - Check if still relevant")
                else:
                    context_parts.append("**Status:** ⏳ Unresolved - Check if still exists")
        
        if resolved_away_from_changes:
            context_parts.append(f"\n### {len(resolved_away_from_changes)} Resolved Issues Away From Current Changes")
            context_parts.append("(Not shown to reduce noise - these issues were resolved)")
        
        if relevant_threads or resolved_away_from_changes:
            context_parts.append("\n**CRITICAL:** Focus your review on the newly changed lines. "
                                "Only comment on previous issues if they still exist and are relevant to current changes.")
        
        return "\n".join(context_parts)

    def _migrate_pr_to_repo_file(self, processed_file: str, pr_id_str: str, pr_item: Dict[str, Any], source_name: str) -> None:
        """Copy a PR record found in a legacy tracking file into the repository-specific file.

        Shared by the underscore-file, legacy-file, and legacy-direct-key
        migration paths in is_pr_already_processed, which all previously
        duplicated this same load/check/append/write sequence.

        Args:
            processed_file: Path to the repository-specific processed-PRs file
            pr_id_str: PR ID as a string
            pr_item: The record to migrate in (already normalized to dict form)
            source_name: Human-readable name of the legacy source, for logging
        """
        try:
            current_prs = []
            if os.path.exists(processed_file):
                with open(processed_file, 'r') as f:
                    current_prs = json.load(f)

            already_in_repo = any(
                (isinstance(item, dict) and item.get("id") == pr_id_str) or item == pr_id_str
                for item in current_prs
            )

            if not already_in_repo:
                current_prs.append(pr_item)
                self._atomic_write_json(processed_file, current_prs)
                logger.info(f"Migrated PR {pr_id_str} from {source_name} to repository file {processed_file}")
        except Exception as e:
            logger.warning(f"Error migrating PR {pr_id_str} from {source_name} to repository file: {e}")

    def is_pr_already_processed(self, repo_name: str, pr_id: Any, current_pr=None) -> tuple:
        """Check if a PR has already been processed and if it's been updated since
        
        Args:
            repo_name: Repository name
            pr_id: Pull request ID
            current_pr: Optional current PullRequest object to check for updates
            
        Returns:
            A tuple of (is_processed, pr_metadata, needs_update):
              - is_processed: True if the PR has been processed before
              - pr_metadata: The stored metadata for the PR if found, None otherwise
              - needs_update: True if the PR has been updated since last processed
        """
        # Convert PR ID to string to ensure consistent comparison
        pr_id_str = str(pr_id)
        pr_metadata = None
        needs_update = False
        
        # Check if PR is in repository-specific file
        processed_file = self._get_processed_file_path(repo_name)
        found_in_repo_file = False
        
        if os.path.exists(processed_file):
            try:
                with open(processed_file, 'r') as f:
                    processed = json.load(f)
                
                # Handle both the new format (with timestamps) and legacy format
                if processed and isinstance(processed[0], dict):
                    # New format - check if PR ID exists in any of the dicts
                    for item in processed:
                        if item.get("id") == pr_id_str:
                            logger.debug(f"PR {pr_id} found in repository-specific file for {repo_name}")
                            pr_metadata = item
                            found_in_repo_file = True
                            
                            # Check if PR has been updated since last processed
                            if current_pr:
                                # Log details for easier debugging
                                logger.debug(f"PR {pr_id} comparison - stored metadata: {item}")
                                logger.debug(f"PR {pr_id} comparison - current PR updated_at: {current_pr.updated_at}, head_commit: {current_pr.head_commit}")
                                
                                has_tracked_metadata = False
                                
                                # Check PR timestamp against our stored processing timestamp
                                if "timestamp" in item and item["timestamp"] and current_pr.updated_at:
                                    has_tracked_metadata = True
                                    try:
                                        # Get our processing timestamp
                                        last_processed_timestamp = item["timestamp"]
                                        
                                        # Convert PR's updated_at to timestamp
                                        import datetime
                                        from dateutil import parser
                                        updated_dt = parser.parse(current_pr.updated_at)
                                        pr_last_updated = int(updated_dt.timestamp())
                                        
                                        logger.info(f"PR {pr_id} with metadata - last processed at {last_processed_timestamp}, last updated at {pr_last_updated}")
                                        
                                        # Only process if PR was updated after our last processing
                                        if pr_last_updated > last_processed_timestamp:
                                            logger.info(f"PR {pr_id} was updated after last processing, will review")
                                            needs_update = True
                                        else:
                                            logger.info(f"PR {pr_id} was last updated before our last processing, skipping")
                                            needs_update = False
                                    except Exception as e:
                                        logger.warning(f"Error comparing timestamps for PR {pr_id}: {e}")
                                        # Fall back to checking metadata directly
                                        
                                        # If we have last updated timestamp, compare with current
                                        if "updated_at" in item and current_pr.updated_at:
                                            has_tracked_metadata = True
                                            # Convert to strings for consistent comparison
                                            stored_updated = str(item["updated_at"])
                                            current_updated = str(current_pr.updated_at)
                                            
                                            if stored_updated != current_updated:
                                                logger.info(f"PR {pr_id} has been updated since last processed - updated_at changed from {stored_updated} to {current_updated}")
                                                needs_update = True
                                                
                                        # If we have the head commit, compare with current
                                        if "head_commit" in item and current_pr.head_commit:
                                            has_tracked_metadata = True
                                            # Convert to strings for consistent comparison
                                            stored_commit = str(item["head_commit"])
                                            current_commit = str(current_pr.head_commit)
                                            
                                            if stored_commit != current_commit:
                                                logger.info(f"PR {pr_id} has a new head commit since last processed - head_commit changed from {stored_commit} to {current_commit}")
                                                needs_update = True
                                else:
                                    # Fall back to checking metadata directly if timestamp comparison not available
                                    
                                    # If we have last updated timestamp, compare with current
                                    if "updated_at" in item and current_pr.updated_at:
                                        has_tracked_metadata = True
                                        # Convert to strings for consistent comparison
                                        stored_updated = str(item["updated_at"])
                                        current_updated = str(current_pr.updated_at)
                                        
                                        if stored_updated != current_updated:
                                            logger.info(f"PR {pr_id} has been updated since last processed - updated_at changed from {stored_updated} to {current_updated}")
                                            needs_update = True
                                            
                                    # If we have the head commit, compare with current
                                    if "head_commit" in item and current_pr.head_commit:
                                        has_tracked_metadata = True
                                        # Convert to strings for consistent comparison
                                        stored_commit = str(item["head_commit"])
                                        current_commit = str(current_pr.head_commit)
                                        
                                        if stored_commit != current_commit:
                                            logger.info(f"PR {pr_id} has a new head commit since last processed - head_commit changed from {stored_commit} to {current_commit}")
                                            needs_update = True
                                
                                # For PRs that have no metadata stored but the current PR does,
                                # compare timestamps to determine if we need to review it
                                if not has_tracked_metadata and (current_pr.updated_at or current_pr.head_commit):
                                    # Get the last processing timestamp
                                    if "timestamp" in item:
                                        last_processed_timestamp = item["timestamp"]
                                        
                                        # Try to get the PR's last update timestamp
                                        pr_last_updated = None
                                        if current_pr.updated_at:
                                            try:
                                                # Try to convert updated_at string to timestamp
                                                import datetime
                                                from dateutil import parser
                                                
                                                # Parse the date string to a datetime object
                                                updated_dt = parser.parse(current_pr.updated_at)
                                                # Convert to timestamp (seconds since epoch)
                                                pr_last_updated = int(updated_dt.timestamp())
                                                
                                                logger.info(f"PR {pr_id} last processed at {last_processed_timestamp}, last updated at {pr_last_updated}")
                                                
                                                # Only process if PR was updated after our last processing
                                                if pr_last_updated > last_processed_timestamp:
                                                    logger.info(f"PR {pr_id} was updated after last processing, will review")
                                                    needs_update = True
                                                else:
                                                    logger.info(f"PR {pr_id} was last updated before our last processing, skipping")
                                                    needs_update = False
                                            except Exception as e:
                                                logger.warning(f"Error parsing PR updated_at timestamp: {e}")
                                                # Fall back to 24 hour rule if we can't parse the timestamp
                                                current_time = int(time.time())
                                                time_since_last_process = current_time - last_processed_timestamp
                                                # 86400 seconds = 24 hours
                                                if time_since_last_process < 86400:
                                                    logger.info(f"PR {pr_id} was processed recently ({time_since_last_process / 3600:.1f} hours ago) - not reprocessing")
                                                    needs_update = False
                                                else:
                                                    logger.info(f"PR {pr_id} was processed more than 24 hours ago - reprocessing")
                                                    needs_update = True
                                        else:
                                            # No updated_at timestamp available, use 24 hour rule
                                            current_time = int(time.time())
                                            time_since_last_process = current_time - last_processed_timestamp
                                            # 86400 seconds = 24 hours
                                            if time_since_last_process < 86400:
                                                logger.info(f"PR {pr_id} was processed recently ({time_since_last_process / 3600:.1f} hours ago) but lacks metadata - not reprocessing")
                                                needs_update = False
                                            else:
                                                logger.info(f"PR {pr_id} needs update - processed more than 24 hours ago and lacks proper metadata")
                                                needs_update = True
                                    else:
                                        # If no timestamp, assume it needs an update
                                        logger.info(f"PR {pr_id} needs update - no timestamp in stored record")
                                        needs_update = True
                            
                            break
                else:
                    # Legacy format - simple list of PR IDs
                    if pr_id_str in processed:
                        logger.debug(f"PR {pr_id} found in legacy format repository-specific file for {repo_name}")
                        pr_metadata = {"id": pr_id_str}
                        found_in_repo_file = True
                        
                        # For legacy format, assume update needed if we have current PR data
                        if current_pr:
                            needs_update = True
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error reading repository-specific file for {repo_name}: {str(e)}")
        
        # If found in repo file, return the status
        if found_in_repo_file:
            return (True, pr_metadata, needs_update)
        
        # Check the underscore file for historical PRs
        underscore_file = os.path.join("processed_prs", "_.json")
        if os.path.exists(underscore_file):
            try:
                with open(underscore_file, 'r') as f:
                    processed = json.load(f)
                
                # Handle both formats
                pr_item = None
                found_in_underscore = False
                
                if processed and isinstance(processed[0], dict):
                    for item in processed:
                        if item.get("id") == pr_id_str:
                            logger.debug(f"PR {pr_id} found in underscore file")
                            pr_item = item
                            found_in_underscore = True
                            break
                else:
                    if pr_id_str in processed:
                        logger.debug(f"PR {pr_id} found in underscore file (old format)")
                        pr_item = {"id": pr_id_str, "timestamp": int(time.time())}
                        found_in_underscore = True
                
                # If PR was found in underscore file, add it to repository file
                if found_in_underscore:
                    self._migrate_pr_to_repo_file(processed_file, pr_id_str, pr_item, "underscore file")

                    # For underscore file, assume update needed if we have current PR data
                    if current_pr:
                        needs_update = True

                    return (True, pr_item, needs_update)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error reading underscore file: {str(e)}")
        
        # Finally check the legacy file as fallback
        legacy_file = "processed_prs.json"
        if os.path.exists(legacy_file):
            try:
                with open(legacy_file, 'r') as f:
                    legacy_data = json.load(f)
                
                # Check if repo exists in legacy data
                if repo_name in legacy_data:
                    repo_processed = legacy_data[repo_name]
                    
                    # Handle both formats in legacy data
                    if repo_processed:
                        pr_item = None
                        found_in_legacy = False
                        
                        if isinstance(repo_processed[0], dict):
                            # New format in legacy file
                            for item in repo_processed:
                                if item.get("id") == pr_id_str:
                                    logger.debug(f"PR {pr_id} found in legacy file (new format) for {repo_name}")
                                    pr_item = item
                                    found_in_legacy = True
                                    break
                        else:
                            # Old format in legacy file - simple list of PR IDs
                            if pr_id_str in repo_processed:
                                logger.debug(f"PR {pr_id} found in legacy file (old format) for {repo_name}")
                                pr_item = {"id": pr_id_str, "timestamp": int(time.time())}
                                found_in_legacy = True
                        
                        # If PR was found in legacy file, add it to repository file
                        if found_in_legacy:
                            self._migrate_pr_to_repo_file(processed_file, pr_id_str, pr_item, "legacy file")

                            # For legacy file, assume update needed if we have current PR data
                            if current_pr:
                                needs_update = True
                                
                            return (True, pr_item, needs_update)
                                
                # It's also possible that legacy file directly uses PR IDs as keys for really old format
                if pr_id_str in legacy_data:
                    logger.debug(f"PR {pr_id} found in legacy file as direct key")

                    self._migrate_pr_to_repo_file(
                        processed_file, pr_id_str,
                        {"id": pr_id_str, "timestamp": int(time.time())},
                        "legacy file (direct key)"
                    )

                    # For legacy direct key, assume update needed if we have current PR data
                    if current_pr:
                        needs_update = True

                    return (True, {"id": pr_id_str}, needs_update)
                    
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error reading legacy file: {str(e)}")
        
        return (False, None, False)
    
    def mark_pr_as_processed(self, repo_name: str, pr_id: Any, pr=None) -> None:
        """Mark a PR as processed
        
        Args:
            repo_name: Repository name
            pr_id: Pull request ID
            pr: Optional PullRequest object with additional metadata
        """
        # Convert PR ID to string for consistent comparison
        pr_id_str = str(pr_id)
        processed_file = self._get_processed_file_path(repo_name)

        # Current time and PR metadata
        current_time = int(time.time())
        # Create PR entry with current timestamp and make sure it always contains the timestamp
        pr_entry = {
            "id": pr_id_str,
            "timestamp": current_time,
        }

        # Add PR metadata if available
        if pr is not None:
            # Log PR metadata for debugging
            logger.debug(f"PR {pr_id} metadata being stored - updated_at: {pr.updated_at}, head_commit: {pr.head_commit}")

            if pr.updated_at:
                pr_entry["updated_at"] = pr.updated_at
            if pr.head_commit:
                pr_entry["head_commit"] = pr.head_commit

            # Add check for other metadata
            if not pr.updated_at and not pr.head_commit:
                logger.warning(f"PR {pr_id} has no updated_at or head_commit metadata to track for updates")
        else:
            logger.warning(f"No PR object provided for {pr_id}, can't track update metadata")

        # Lock around the read-modify-write so two overlapping reviewer
        # processes (e.g. a watchdog restart racing an in-flight run) can't
        # clobber each other's update to this repo's processed-PR list.
        with self._locked_processed_file(processed_file):
            try:
                with open(processed_file, 'r') as f:
                    processed = json.load(f)
            except (json.JSONDecodeError, IOError):
                processed = []

            # Check if this PR already exists in the repository file
            pr_index = None
            for i, entry in enumerate(processed):
                if isinstance(entry, dict) and entry.get("id") == pr_id_str:
                    pr_index = i
                    break

            # Update existing entry or add new one
            if pr_index is not None:
                logger.debug(f"Updating PR {pr_id} metadata in repository file for {repo_name}")

                # Log the old and new values for debugging
                old_entry = processed[pr_index]
                logger.debug(f"PR {pr_id} metadata update - old: {old_entry}, new: {pr_entry}")

                processed[pr_index] = pr_entry
            else:
                logger.debug(f"Adding PR {pr_id} to repository file for {repo_name}")
                processed.append(pr_entry)

            self._atomic_write_json(processed_file, processed)


    def reset_repository_prs(self, repo_config: Dict[str, Any]) -> int:
        """Reset processed PRs for a specific repository
        
        Args:
            repo_config: Repository configuration
            
        Returns:
            Number of PRs reset
        """
        repo_id = self._get_repo_identifier(repo_config)
        processed_file = self._get_processed_file_path(repo_id)
        
        if not os.path.exists(processed_file):
            logger.info(f"No processed PRs found for repository {repo_id}")
            return 0
            
        try:
            with open(processed_file, 'r') as f:
                processed = json.load(f)
            
            count = len(processed)

            # Clear the file
            self._atomic_write_json(processed_file, [])

            logger.info(f"Reset {count} PRs for repository {repo_id}")
            return count
        except Exception as e:
            logger.warning(f"Error resetting PRs for repository {repo_id}: {e}")
            return 0
    
    def reset_processed_prs(self, older_than_hours: int = 24) -> None:
        """Reset processed PRs that were processed more than the specified hours ago
        
        Args:
            older_than_hours: Reset PRs processed more than this many hours ago
        """
        # Check if processed_prs directory exists
        processed_dir = "processed_prs"
        if not os.path.exists(processed_dir):
            # Check if legacy file exists as fallback
            legacy_file = "processed_prs.json"
            if os.path.exists(legacy_file):
                self._reset_legacy_processed_prs(legacy_file, older_than_hours)
            return
        
        # Get all repository files
        repo_files = [f for f in os.listdir(processed_dir) if f.endswith('.json')]
        
        current_time = int(time.time())
        reset_threshold = current_time - (older_than_hours * 3600)  # Convert hours to seconds
        
        for repo_file in repo_files:
            try:
                file_path = os.path.join(processed_dir, repo_file)
                with open(file_path, 'r') as f:
                    processed = json.load(f)
                
                updated_processed = []
                reset_count = 0
                
                # Handle both formats
                if processed and isinstance(processed[0], dict):
                    # New format - filter based on timestamp
                    for item in processed:
                        timestamp = item.get("timestamp")
                        if timestamp and timestamp >= reset_threshold:
                            # Keep recent PRs
                            updated_processed.append(item)
                        else:
                            # Reset older PRs
                            reset_count += 1
                else:
                    # Legacy format - reset all, no timestamp available
                    reset_count = len(processed)
                    updated_processed = []
                
                if reset_count > 0:
                    logger.info(f"Resetting {reset_count} PRs for repository file {repo_file}")
                    self._atomic_write_json(file_path, updated_processed)

            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error resetting processed PRs for {repo_file}: {e}")
    
    def _reset_legacy_processed_prs(self, legacy_file: str, older_than_hours: int) -> None:
        """Reset PRs in legacy processed_prs.json file
        
        Args:
            legacy_file: Path to legacy file
            older_than_hours: Reset PRs processed more than this many hours ago
        """
        try:
            with open(legacy_file, 'r') as f:
                legacy_data = json.load(f)
            
            current_time = int(time.time())
            reset_threshold = current_time - (older_than_hours * 3600)  # Convert hours to seconds
            
            # Create processed_prs directory if needed
            processed_dir = "processed_prs"
            if not os.path.exists(processed_dir):
                os.makedirs(processed_dir)
            
            total_reset = 0
            
            # Process each repository in the legacy file
            for repo_name, repo_processed in legacy_data.items():
                safe_name = repo_name.replace('/', '_').replace('\\', '_').replace(':', '_')
                repo_file = os.path.join(processed_dir, f"{safe_name}.json")
                
                migrated_prs = []
                reset_count = 0
                
                # Handle both formats in legacy data
                if repo_processed and isinstance(repo_processed[0], dict):
                    # New format in legacy file
                    for item in repo_processed:
                        timestamp = item.get("timestamp")
                        if timestamp and timestamp >= reset_threshold:
                            # Keep recent PRs
                            migrated_prs.append(item)
                        else:
                            # Reset older PRs
                            reset_count += 1
                else:
                    # Old format in legacy file - reset all
                    reset_count = len(repo_processed)
                
                # Write to new repository-specific file
                self._atomic_write_json(repo_file, migrated_prs)


                total_reset += reset_count
                logger.info(f"Migrated repository {repo_name} to new file format, reset {reset_count} PRs")
            
            logger.info(f"Migrated legacy file to new format, reset {total_reset} PRs")
            
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error processing legacy file {legacy_file}: {e}")
            
    def list_processed_prs(self) -> Dict[str, List]:
        """List all processed PRs organized by repository
        
        Returns:
            Dictionary mapping repository names to lists of processed PR data
        """
        result = {}
        processed_dir = "processed_prs"
        
        # Return empty result if the directory doesn't exist
        if not os.path.exists(processed_dir):
            return result
            
        # Get all repository files
        repo_files = [f for f in os.listdir(processed_dir) if f.endswith('.json')]
        
        for repo_file in repo_files:
            try:
                file_path = os.path.join(processed_dir, repo_file)
                with open(file_path, 'r') as f:
                    processed = json.load(f)
                
                # Use filename (without extension) as repository name
                repo_name = os.path.splitext(repo_file)[0]
                result[repo_name] = processed
            except Exception as e:
                logger.warning(f"Error reading repository file {repo_file}: {e}")
                
        return result
        
    def fix_repo_files(self) -> Dict[str, int]:
        """Fix repository files that might contain incorrect data
        
        Returns:
            Dictionary mapping repository names to number of removed incorrect PRs
        """
        results = {}
        processed_dir = "processed_prs"
        
        # Return empty result if the directory doesn't exist
        if not os.path.exists(processed_dir):
            return results
            
        # Get all repository files
        repo_files = [f for f in os.listdir(processed_dir) if f.endswith('.json')]
        
        for repo_file in repo_files:
            try:
                file_path = os.path.join(processed_dir, repo_file)
                with open(file_path, 'r') as f:
                    processed = json.load(f)
                
                original_count = len(processed)
                fixed_processed = []
                
                # Keep only entries that are properly formatted
                for item in processed:
                    if isinstance(item, dict) and "id" in item:
                        fixed_processed.append(item)
                    elif isinstance(item, str):
                        # Convert legacy string format to dict format
                        fixed_processed.append({"id": item, "timestamp": int(time.time())})
                
                # Save if any changes were made
                if len(fixed_processed) != original_count:
                    removed = original_count - len(fixed_processed)
                    self._atomic_write_json(file_path, fixed_processed)

                    repo_name = os.path.splitext(repo_file)[0]
                    results[repo_name] = removed
            except Exception as e:
                logger.warning(f"Error fixing repository file {repo_file}: {e}")
                
        return results
        
    def migrate_underscore_file(self) -> int:
        """Migrate data from the legacy underscore file to repository-specific files
        
        Returns:
            Number of repository files created or updated
        """
        underscore_file = os.path.join("processed_prs", "_.json")
        if not os.path.exists(underscore_file):
            return 0
            
        try:
            with open(underscore_file, 'r') as f:
                processed = json.load(f)
                
            # Get all repositories from configuration
            repositories = self.config.get_repositories()
            repo_ids = {}
            
            # Build a mapping of repository IDs
            for repo_config in repositories:
                repo_id = self._get_repo_identifier(repo_config)
                repo_ids[repo_id] = repo_config
                
            # Create a counter for updated repository files
            updated_count = 0
            
            # Attempt to determine repository for each PR
            for item in processed:
                if isinstance(item, dict) and "repo_id" in item:
                    repo_id = item["repo_id"]
                    pr_id = item.get("id")
                    
                    if repo_id in repo_ids and pr_id:
                        # Add to repository-specific file
                        file_path = self._get_processed_file_path(repo_id)
                        
                        try:
                            with open(file_path, 'r') as f:
                                repo_processed = json.load(f)
                                
                            # Check if PR already exists
                            exists = False
                            for existing in repo_processed:
                                if isinstance(existing, dict) and existing.get("id") == pr_id:
                                    exists = True
                                    break
                                    
                            if not exists:
                                repo_processed.append(item)
                                self._atomic_write_json(file_path, repo_processed)
                                updated_count += 1
                        except Exception as e:
                            logger.warning(f"Error updating repository file for {repo_id}: {e}")
                            
            return updated_count
            
        except Exception as e:
            logger.warning(f"Error migrating underscore file: {e}")
            return 0