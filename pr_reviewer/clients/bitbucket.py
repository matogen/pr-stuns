#!/usr/bin/env python3
"""
Bitbucket client implementation for repository interactions.
"""

import os
import base64
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
from pr_reviewer.clients.base import BaseRepoClient
from pr_reviewer.utils.logging import logger


# get_file_content()'s last-resort fallback phases search open PRs / branches
# one HTTP request at a time; cap how many we're willing to try so a large
# repo (hundreds of PRs or branches) can't turn one file lookup into hundreds
# of API calls.
_MAX_FALLBACK_SEARCH_ITEMS = 20


def _decode_utf8(response: requests.Response) -> str:
    """Decode a response body as UTF-8.

    ``requests`` falls back to ISO-8859-1 for any ``text/*`` Content-Type
    that omits an explicit charset (per old RFC 2616 behavior) — exactly
    what Bitbucket's diff and raw ``/src`` endpoints send. Left to
    ``response.text``, that silently mangles non-ASCII bytes (—, ©, …)
    before the content ever reaches the model. Decode explicitly as UTF-8
    and only fall back to detected encoding if the bytes genuinely aren't
    valid UTF-8.
    """
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode(response.apparent_encoding or "utf-8", errors="replace")


def _build_auth(api_token: Optional[str], email: Optional[str],
                username: Optional[str],
                app_password: Optional[str]) -> Tuple[str, str]:
    """Pick Atlassian API token (Basic email:token) over legacy app password.

    Atlassian's Bitbucket Cloud API expects Basic auth with the account email
    as the username when authenticating with an API token — Bearer auth is
    rejected with "Token is invalid, expired, or not supported for this
    endpoint". App passwords use the Bitbucket username instead.
    """
    if api_token:
        return (email or "", api_token)
    return (username or "", app_password or "")


class BitbucketClient(BaseRepoClient):
    """Client for interacting with Bitbucket API"""

    def __init__(self, workspace: str, repo_slug: str,
                 username: Optional[str] = None,
                 app_password: Optional[str] = None,
                 api_token: Optional[str] = None,
                 email: Optional[str] = None,
                 bitbucket_url: str = "https://api.bitbucket.org/2.0",
                 fallback_users: Optional[List[Dict[str, str]]] = None):
        """Initialize Bitbucket client

        Args:
            workspace: Workspace/team name
            repo_slug: Repository slug
            username: Bitbucket username (only required for legacy app_password auth)
            app_password: Bitbucket app password (legacy Basic auth)
            api_token: Atlassian API token. Preferred — Atlassian deprecated
                app passwords in 2025. Used as Basic auth with ``email``.
            email: Atlassian account email; required when ``api_token`` is set
                (Bitbucket's API token auth is Basic email:token, not Bearer).
            bitbucket_url: Base URL for Bitbucket API
            fallback_users: Optional list of fallback credentials used when rate
                limits are hit. Each entry may carry either ``api_token`` +
                ``email`` or ``username`` + ``app_password``.
        """
        if not api_token and not app_password:
            raise ValueError(
                "Bitbucket client requires either 'api_token' (with 'email') or "
                "'username' + 'app_password'"
            )
        if api_token and not email:
            raise ValueError(
                "Bitbucket api_token requires 'email' for Basic auth"
            )

        self.primary_auth = _build_auth(api_token, email, username, app_password)
        self.auth = self.primary_auth
        
        # Fallback credentials
        self.fallback_users = fallback_users or []
        self.current_user_index = -1  # Start with primary user (index -1)
        
        # Repository info
        self.workspace = workspace
        self.repo_slug = repo_slug
        self.bitbucket_url = bitbucket_url.rstrip('/')
        self.base_url = f"{self.bitbucket_url}/repositories/{workspace}/{repo_slug}"
        self.headers = {
            "Content-Type": "application/json"
        }
        
        # Rate limit tracking based on Bitbucket API documentation
        # Authenticated requests: 1,000-10,000 per hour for repository data
        # Git operations: 60,000 per hour
        # Raw files: 5,000 per hour
        # Webhooks: 1,000 per hour
        # Application properties: 2,000 per hour
        self.rate_limit_hits = 0
        self.last_rate_limit_time = 0
        self.requests_this_hour = 0
        self.hour_start_time = time.time()
        self.max_requests_per_hour = 1000  # Conservative limit for repository data
        self.requests_in_last_minute = []
        self.max_requests_per_minute = 17  # 1000/60 = ~17 requests per minute
        
    def get_authenticated_user(self) -> Dict[str, Any]:
        """Get information about the currently authenticated user
        
        Returns:
            Dictionary containing user information including display_name, username, etc.
        """
        try:
            # Bitbucket API endpoint for current user
            url = "https://api.bitbucket.org/2.0/user"
            response = requests.get(url, auth=self.auth, timeout=30)
            response.raise_for_status()
            user_data = response.json()
            
            return {
                'username': user_data.get('username', ''),
                'display_name': user_data.get('display_name', ''),
                'nickname': user_data.get('nickname', ''),
                'account_id': user_data.get('account_id', ''),
                'uuid': user_data.get('uuid', '')
            }
        except requests.RequestException as e:
            logger.warning(f"Error getting authenticated user from Bitbucket: {e}")
            return {}
        
    def _switch_to_next_user(self) -> bool:
        """Switch to the next available user when rate limits are hit
        
        Returns:
            bool: True if successfully switched to a new user, False if no more users are available
        """
        self.current_user_index += 1
        
        # Check if we've exhausted all fallback users
        if self.current_user_index >= len(self.fallback_users):
            # Reset to primary user
            self.auth = self.primary_auth
            self.current_user_index = -1
            logger.warning("All users have hit rate limits. Resetting to primary user and adding delay.")
            time.sleep(5)  # Reduced delay when cycling back to primary user
            return False
            
        # Switch to the next fallback user
        fallback_user = self.fallback_users[self.current_user_index]
        self.auth = _build_auth(
            fallback_user.get('api_token'),
            fallback_user.get('email'),
            fallback_user.get('username'),
            fallback_user.get('app_password'),
        )
        logger.info(f"Switched to fallback user {self.current_user_index+1}/{len(self.fallback_users)} due to rate limits")
        return True
        
    def _check_rate_limit_preemptive(self) -> bool:
        """Check if we're approaching rate limits and should slow down
        
        Returns:
            bool: True if we should slow down, False if we can proceed normally
        """
        current_time = time.time()
        
        # Reset hourly counter if needed
        if current_time - self.hour_start_time >= 3600:
            self.requests_this_hour = 0
            self.hour_start_time = current_time
        
        # Clean old minute requests
        minute_ago = current_time - 60
        self.requests_in_last_minute = [t for t in self.requests_in_last_minute if t > minute_ago]
        
        # Check if we're approaching limits
        approaching_hourly_limit = self.requests_this_hour >= self.max_requests_per_hour * 0.9
        approaching_minute_limit = len(self.requests_in_last_minute) >= self.max_requests_per_minute * 0.8
        
        if approaching_hourly_limit:
            logger.warning(f"Approaching hourly rate limit: {self.requests_this_hour}/{self.max_requests_per_hour}")
            return True
        
        if approaching_minute_limit:
            logger.warning(f"Approaching per-minute rate limit: {len(self.requests_in_last_minute)}/{self.max_requests_per_minute}")
            return True
        
        return False
    
    def _track_request(self):
        """Track a request for rate limiting purposes"""
        current_time = time.time()
        self.requests_this_hour += 1
        self.requests_in_last_minute.append(current_time)
    
    def _handle_rate_limit(self, response, context: str = "") -> bool:
        """Handle a rate limit response by switching users or adding delays
        
        Args:
            response: The HTTP response that triggered the rate limit
            context: Optional context string for logging
            
        Returns:
            bool: True if handled by switching users, False if delay was added
        """
        now = time.time()
        self.rate_limit_hits += 1
        self.last_rate_limit_time = now
        
        # Log the rate limit occurrence
        rate_limit_msg = f"Rate limit hit{' for ' + context if context else ''}"
        logger.warning(f"{rate_limit_msg} (#{self.rate_limit_hits}): {response.text if hasattr(response, 'text') else 'No response text'}")
        
        # Try to switch to a fallback user
        if self.fallback_users and self._switch_to_next_user():
            # Successfully switched to a fallback user
            return True
        else:
            # No fallback users available or all have hit rate limits
            # Add a smart delay based on rate limit type
            # For repository operations, we can be more aggressive
            delay = min(10, 2 * self.rate_limit_hits)  # Progressive delay, max 10 seconds
            logger.warning(f"No available fallback users, delaying for {delay} seconds before retry")
            time.sleep(delay)
            return False
    
    def get_pull_requests(self, state: str = "OPEN", sort: str = "created_on") -> List[Dict[str, Any]]:
        """Get pull requests for the repository
        
        Args:
            state: Pull request state filter ('OPEN', 'MERGED', 'DECLINED', 'SUPERSEDED')
            sort: Sort field ('created_on', 'updated_on')
            
        Returns:
            List of pull request data
        """
        # Check rate limits before making request
        if self._check_rate_limit_preemptive():
            time.sleep(1)  # Brief pause if approaching limits
        
        url = f"{self.base_url}/pullrequests"
        # Explicitly request all the fields we need for PR processing
        fields = "values.id,values.title,values.author,values.description,values.state,values.created_on,values.updated_on,values.source.commit.hash,values.source.branch.name,values.destination.branch.name"
        params = {"state": state, "sort": sort, "fields": fields}
        
        self._track_request()
        response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=30)
        
        if response.status_code == 429:
            self._handle_rate_limit(response, "get_pull_requests")
            # Retry once after handling rate limit
            self._track_request()
            response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=30)
        
        response.raise_for_status()
        data = response.json()
        
        # Log the response to debug data structure
        from pr_reviewer.utils.logging import logger
        logger.info(f"Bitbucket get_pull_requests response keys: {data.keys()}")
        if "values" in data and data["values"]:
            logger.info(f"First PR data keys: {data['values'][0].keys()}")
            logger.info(f"First PR source structure: {data['values'][0].get('source', {})}")
        
        return data.get("values", [])
    
    def get_pull_request_diff(self, pr_id: int) -> str:
        """Get the diff for a pull request
        
        Args:
            pr_id: Pull request ID
            
        Returns:
            Diff text
        """
        # Check rate limits before making request
        if self._check_rate_limit_preemptive():
            time.sleep(1)  # Brief pause if approaching limits
        
        url = f"{self.base_url}/pullrequests/{pr_id}/diff"
        self._track_request()
        response = requests.get(url, auth=self.auth, headers={"Accept": "text/plain"}, timeout=30)
        
        if response.status_code == 429:
            self._handle_rate_limit(response, "get_pull_request_diff")
            # Retry once after handling rate limit
            self._track_request()
            response = requests.get(url, auth=self.auth, headers={"Accept": "text/plain"}, timeout=30)
        
        response.raise_for_status()
        return _decode_utf8(response)
    
    def get_pull_request_files(self, pr_id: int) -> List[Dict[str, Any]]:
        """Extract files from a pull request diff
        
        Args:
            pr_id: Pull request ID
            
        Returns:
            List of file change information
        """
        diff_text = self.get_pull_request_diff(pr_id)
        
        # Parse the diff to extract file information
        files = []
        current_file = None
        
        for line in diff_text.split('\n'):
            if line.startswith('--- a/') or line.startswith('+++ b/'):
                # New file in diff
                if line.startswith('+++ b/'):
                    file_path = line[6:]
                    current_file = {
                        "filename": file_path,
                        "status": "modified",  # Default status
                        "patch": ""
                    }
                    files.append(current_file)
            elif current_file is not None:
                current_file["patch"] = current_file.get("patch", "") + line + "\n"
        
        return files
    
    def add_comment(self, pr_id: int, body: str) -> Dict[str, Any]:
        """Add a comment to a pull request
        
        Args:
            pr_id: Pull request ID
            body: Comment text
            
        Returns:
            Comment data from API response
        """
        # Bitbucket has a character limit of approximately 32,768 characters
        max_length = 30000  # Using a lower limit to be safe
        
        if len(body) <= max_length:
            # Check rate limits before making request
            if self._check_rate_limit_preemptive():
                time.sleep(1)  # Brief pause if approaching limits
            
            url = f"{self.base_url}/pullrequests/{pr_id}/comments"
            data = {"content": {"raw": body}}
            self._track_request()
            response = requests.post(url, auth=self.auth, headers=self.headers, json=data, timeout=30)
            
            if response.status_code == 429:
                self._handle_rate_limit(response, "add_comment")
                # Retry once after handling rate limit
                self._track_request()
                response = requests.post(url, auth=self.auth, headers=self.headers, json=data, timeout=30)
            
            response.raise_for_status()
            return response.json()
        else:
            # Split the comment into smaller parts
            parts = []
            remaining = body
            
            while remaining:
                # Find a good break point (preferably at the end of a paragraph)
                if len(remaining) <= max_length:
                    parts.append(remaining)
                    break
                    
                # Try to find a paragraph break
                break_point = remaining[:max_length].rfind("\n\n")
                if break_point == -1 or break_point < max_length // 2:
                    # If no good paragraph break, try line break
                    break_point = remaining[:max_length].rfind("\n")
                
                if break_point == -1 or break_point < max_length // 2:
                    # If still no good break, just split at the max length
                    break_point = max_length
                
                parts.append(remaining[:break_point])
                remaining = remaining[break_point:].lstrip()
            
            # Post each part as a separate comment
            results = []
            for i, part in enumerate(parts):
                prefix = f"## Part {i+1}/{len(parts)}\n\n" if len(parts) > 1 else ""
                url = f"{self.base_url}/pullrequests/{pr_id}/comments"
                data = {"content": {"raw": prefix + part}}
                response = requests.post(url, auth=self.auth, headers=self.headers, json=data, timeout=30)
                response.raise_for_status()
                results.append(response.json())
            
            return results[-1]  # Return the last part's response
    
    def add_review_comment(self, pr_id: int, commit_id: str, path: str, 
                           body: str, line: int) -> Dict[str, Any]:
        """Add a review comment to a specific line in a PR
        
        Args:
            pr_id: Pull request ID
            commit_id: Commit SHA
            path: File path
            body: Comment text
            line: Line number
            
        Returns:
            Comment data from API response
        """
        # Bitbucket requires more information for inline comments
        # We need to get the file's current diff to determine the correct line context
        try:
            # First try standard inline comment API
            url = f"{self.base_url}/pullrequests/{pr_id}/comments"
            data = {
                "content": {"raw": body},
                "inline": {
                    "path": path,
                    "to": line
                }
            }
            response = requests.post(url, auth=self.auth, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            # If that fails, fall back to regular PR comment
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Failed to add inline comment, falling back to regular comment: {e}")
            
            # Create a comment that references the file and line
            file_reference_body = f"**Comment for {path} (line {line}):**\n\n{body}"
            return self.add_comment(pr_id, file_reference_body)
        
    def remove_user_as_watcher(self, pr_id: int, username: str) -> bool:
        """Remove a user as a watcher from a pull request in Bitbucket
        
        Args:
            pr_id: Pull request ID
            username: Username to remove as watcher
            
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.base_url}/pullrequests/{pr_id}/watch"
        
        try:
            # First, check if the user is a watcher
            response = requests.get(url, auth=self.auth, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            # If the user is watching, the endpoint returns a 204 status with empty response
            # If user is not watching, we don't need to do anything
            if response.status_code == 204:
                # User is a watcher, so delete the watch
                response = requests.delete(url, auth=self.auth, headers=self.headers, timeout=30)
                response.raise_for_status()
                
                if response.status_code in (204, 200):
                    return True
                    
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error removing user as watcher from PR #{pr_id}: {str(e)}")
            
        return False
        
    def get_commit_changed_files(self, commit_id: str) -> List[Dict[str, Any]]:
        """Get the files changed in a specific commit
        
        Args:
            commit_id: Commit SHA
            
        Returns:
            List of file changes in the commit
        """
        url = f"{self.base_url}/commit/{commit_id}/diff"
        
        try:
            # Get the diff for this commit
            response = requests.get(url, auth=self.auth, headers={"Accept": "text/plain"}, timeout=30)
            response.raise_for_status()
            diff_text = _decode_utf8(response)
            
            # Parse the diff to extract file information
            files = []
            current_file = None
            previous_line = ""
            
            for line in diff_text.split('\n'):
                if line.startswith('--- a/') or line.startswith('+++ b/'):
                    # New file in diff
                    if line.startswith('+++ b/'):
                        file_path = line[6:]
                        # Determine status based on diff content
                        status = "modified"
                        if line.startswith('+++ b/dev/null'):
                            status = "removed"
                        elif previous_line and previous_line.startswith('--- a/dev/null'):
                            status = "added"
                            
                        current_file = {
                            "filename": file_path,
                            "status": status,
                            "patch": ""
                        }
                        files.append(current_file)
                    previous_line = line
                elif current_file is not None:
                    current_file["patch"] = current_file.get("patch", "") + line + "\n"
            
            return files
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching changed files for commit {commit_id}: {str(e)}")
            return []
            
    def get_file_content(self, path: str, ref: Optional[str] = None, pr_id: Optional[int] = None) -> str:
        """Get full content of a file at a specific reference (branch/commit)
        
        Args:
            path: Path to the file
            ref: Optional reference (branch/commit/tag), defaults to default branch
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            String content of the file
        """
        # For Bitbucket, we need to prioritize the PR branch content for new files
        # This is especially important for files that only exist in the PR branch
        logger.info(f"Bitbucket get_file_content called: path={path}, ref={ref}, pr_id={pr_id}")
        errors = []

        # Store the PR context information we find for logging and debugging
        pr_context = {}
        
        # Initialize flag to track if we're processing a specific PR
        current_pr_id = pr_id  # Use provided PR ID if available
        
        # If no PR ID was provided, try to extract it from ref if it looks like a commit hash
        if current_pr_id is None and ref and len(ref) >= 7:  # Minimum git SHA length
            try:
                # Get open PRs to see if this commit is related to any
                prs_url = f"{self.base_url}/pullrequests"
                params = {"state": "OPEN", "fields": "values.id,values.source.commit.hash"}
                response = requests.get(prs_url, auth=self.auth, headers=self.headers, params=params, timeout=30)
                if response.status_code == 200:
                    prs_data = response.json()
                    for pr in prs_data.get("values", []):
                        commit_hash = pr.get("source", {}).get("commit", {}).get("hash", "")
                        if commit_hash.startswith(ref) or ref.startswith(commit_hash):
                            current_pr_id = pr.get("id")
                            logger.info(f"Auto-detected file {path} is being accessed as part of PR #{current_pr_id}")
                            pr_context["pr_id"] = current_pr_id
                            break
                elif response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, "initial PR lookup")
                    errors.append(f"Rate limited during PR lookup: {response.status_code}")
            except Exception as e:
                logger.debug(f"Failed to match ref {ref} to PRs: {e}")
        elif current_pr_id is not None:
            logger.info(f"Using provided PR ID #{current_pr_id} for file {path}")
            pr_context["pr_id"] = current_pr_id

        # PHASE 1: Try to get content directly from the PR context if we're in a review
        # This is the most reliable approach for files only in PR branches
        if current_pr_id:
            try:
                # Get detailed PR information to access source branch
                pr_detail_url = f"{self.base_url}/pullrequests/{current_pr_id}"
                response = requests.get(pr_detail_url, auth=self.auth, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    pr_detail = response.json()
                    
                    # Extract source branch and source repository information
                    source_branch = pr_detail.get("source", {}).get("branch", {}).get("name")
                    source_commit = pr_detail.get("source", {}).get("commit", {}).get("hash")
                    pr_context["source_branch"] = source_branch
                    pr_context["source_commit"] = source_commit
                    
                    logger.info(f"PR #{current_pr_id} context - source branch: {source_branch}, source commit: {source_commit}")
                    
                    # First try the specific commit from the PR
                    if source_commit:
                        url = f"{self.base_url}/src/{source_commit}/{path}"
                        try:
                            response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                            if response.status_code == 200:
                                logger.info(f"Found file in PR source commit: {source_commit}")
                                return _decode_utf8(response)
                            elif response.status_code == 429:  # Rate limit hit
                                self._handle_rate_limit(response, "source commit access")
                                errors.append(f"Rate limited during source commit access: {response.status_code}")
                        except Exception as e:
                            errors.append(f"Failed with PR source commit {source_commit}: {str(e)}")
                    
                    # Then try the source branch
                    if source_branch:
                        url = f"{self.base_url}/src/{source_branch}/{path}"
                        try:
                            response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                            if response.status_code == 200:
                                logger.info(f"Found file in PR source branch: {source_branch}")
                                return _decode_utf8(response)
                            elif response.status_code == 429:  # Rate limit hit
                                self._handle_rate_limit(response, "source branch access")
                                errors.append(f"Rate limited during source branch access: {response.status_code}")
                        except Exception as e:
                            errors.append(f"Failed with PR source branch {source_branch}: {str(e)}")
                elif response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, "PR details lookup")
                    errors.append(f"Rate limited during PR details lookup: {response.status_code}")
            except Exception as e:
                errors.append(f"Failed to get PR context for PR #{current_pr_id}: {str(e)}")

        # Early exit: If we have errors already and this is for import resolution, don't retry
        # This prevents excessive API calls for missing import files
        if errors and not pr_id:
            logger.warning(f"File {path} not found in primary attempt, skipping further searches for import resolution")
            return ""

        # PHASE 2: If we weren't able to determine PR context or file wasn't found,
        # try the provided reference (usually the PR head commit)
        if ref:
            url = f"{self.base_url}/src/{ref}/{path}"
            try:
                response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                if response.status_code == 200:
                    return _decode_utf8(response)
                elif response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, f"ref {ref} access")
                    errors.append(f"Rate limited during ref access: {response.status_code}")
                else:
                    response.raise_for_status()
            except requests.RequestException as e:
                errors.append(f"Failed with ref '{ref}': {str(e)}")
                # For import resolution (no pr_id), exit early on 404
                if not pr_id and "404" in str(e):
                    logger.warning(f"File {path} not found with ref '{ref}', skipping further searches for import resolution")
                    return ""

        # PHASE 3: Try with "HEAD" (the default branch) - this is a fallback, not priority
        url = f"{self.base_url}/src/HEAD/{path}"
        try:
            response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
            if response.status_code == 200:
                logger.info(f"Found file in HEAD branch (note: this is not preferred for PR-only files)")
                return _decode_utf8(response)
            elif response.status_code == 429:  # Rate limit hit
                self._handle_rate_limit(response, "HEAD access")
                errors.append(f"Rate limited during HEAD access: {response.status_code}")
            else:
                response.raise_for_status()
        except requests.RequestException as e:
            errors.append(f"Failed with 'HEAD': {str(e)}")
            # For import resolution (no pr_id), exit early on 404
            if not pr_id and "404" in str(e):
                logger.warning(f"File {path} not found in HEAD branch, skipping further searches for import resolution")
                return ""
        
        # PHASE 4: More thorough approach to find file in source branches of all open PRs
        try:
            # Get all open PRs (if we haven't already determined the current PR)
            if not current_pr_id:
                prs_url = f"{self.base_url}/pullrequests"
                params = {"state": "OPEN", "fields": "values.id,values.source.branch.name,values.source.commit.hash,values.destination.branch.name"}
                response = requests.get(prs_url, auth=self.auth, headers=self.headers, params=params, timeout=30)
                if response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, "PR list for file search")
                    errors.append(f"Rate limited during PR detailed lookup: {response.status_code}")
                else:
                    response.raise_for_status()
                    prs_data = response.json()
                    
                    # Process each PR (capped — see _MAX_FALLBACK_SEARCH_ITEMS)
                    for pr in prs_data.get("values", [])[:_MAX_FALLBACK_SEARCH_ITEMS]:
                        pr_id = pr.get("id")
                        source_branch = pr.get("source", {}).get("branch", {}).get("name")
                        source_commit = pr.get("source", {}).get("commit", {}).get("hash")
                        destination_branch = pr.get("destination", {}).get("branch", {}).get("name")
                        
                        logger.debug(f"Checking PR #{pr_id} - source: {source_branch}, destination: {destination_branch}")
                        
                        # Try source branch name
                        if source_branch:
                            url = f"{self.base_url}/src/{source_branch}/{path}"
                            try:
                                branch_response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                                if branch_response.status_code == 200:
                                    logger.info(f"Found file in PR #{pr_id} source branch: {source_branch}")
                                    return _decode_utf8(branch_response)
                                elif branch_response.status_code == 429:  # Rate limit hit
                                    self._handle_rate_limit(branch_response, f"branch {source_branch} access in PR")
                                    if len(self.fallback_users) == 0:
                                        break  # Break loop only if we have no fallback users
                            except Exception as e:
                                logger.debug(f"Failed to get file from branch {source_branch} in PR #{pr_id}: {e}")
                        
                        # Try source commit hash
                        if source_commit:
                            url = f"{self.base_url}/src/{source_commit}/{path}"
                            try:
                                commit_response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                                if commit_response.status_code == 200:
                                    logger.info(f"Found file in PR #{pr_id} source commit: {source_commit}")
                                    return _decode_utf8(commit_response)
                                elif commit_response.status_code == 429:  # Rate limit hit
                                    self._handle_rate_limit(commit_response, f"commit {source_commit} access in PR")
                                    if len(self.fallback_users) == 0:
                                        break  # Break loop only if we have no fallback users
                            except Exception as e:
                                logger.debug(f"Failed to get file from commit {source_commit} in PR #{pr_id}: {e}")
        except Exception as e:
            errors.append(f"Failed PR detailed lookup: {str(e)}")
        
        # PHASE 5: Try all common branch names as a last resort
        common_branches = ["develop", "development", "feature", "release", "bugfix", "hotfix", "master", "main"]
        for branch in common_branches:
            url = f"{self.base_url}/src/{branch}/{path}"
            try:
                response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                if response.status_code == 200:
                    logger.info(f"Found file in common branch: {branch}")
                    return _decode_utf8(response)
                elif response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, f"common branch {branch} access")
                    if len(self.fallback_users) == 0:
                        break  # Break loop only if we have no fallback users
            except Exception:
                pass
        
        # PHASE 6: If all attempts failed, try to search all branches
        try:
            # Get repository info to determine default branch
            repo_url = f"{self.bitbucket_url}/repositories/{self.workspace}/{self.repo_slug}"
            response = requests.get(repo_url, auth=self.auth, headers=self.headers, timeout=30)
            if response.status_code == 200:
                repo_info = response.json()
                default_branch = repo_info.get("mainbranch", {}).get("name", "master")
                logger.info(f"Repository default branch: {default_branch}")
                
                # Try to get branches
                branches_url = f"{self.base_url}/refs/branches"
                response = requests.get(branches_url, auth=self.auth, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    branches_data = response.json()
                    # Capped — see _MAX_FALLBACK_SEARCH_ITEMS
                    for branch in branches_data.get("values", [])[:_MAX_FALLBACK_SEARCH_ITEMS]:
                        branch_name = branch.get("name")
                        if branch_name and branch_name != default_branch:
                            # Skip the default branch as we've already tried it
                            url = f"{self.base_url}/src/{branch_name}/{path}"
                            try:
                                response = requests.get(url, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
                                if response.status_code == 200:
                                    logger.info(f"Found file in branch: {branch_name}")
                                    return _decode_utf8(response)
                                elif response.status_code == 429:  # Rate limit hit
                                    self._handle_rate_limit(response, f"branch {branch_name} access")
                                    if len(self.fallback_users) == 0:
                                        break  # Break loop only if we have no fallback users
                            except Exception:
                                pass
                elif response.status_code == 429:  # Rate limit hit
                    self._handle_rate_limit(response, "branches lookup")
                    errors.append(f"Rate limited during branches lookup: {response.status_code}")
            elif response.status_code == 429:  # Rate limit hit
                self._handle_rate_limit(response, "repository info lookup")
                errors.append(f"Rate limited during repository info lookup: {response.status_code}")
        except Exception as e:
            errors.append(f"Failed branches fallback: {str(e)}")
                
        # If we get here, all attempts failed
        error_message = "; ".join(errors)
        
        # Include PR context in the log if available
        if pr_context:
            context_str = ", ".join(f"{k}: {v}" for k, v in pr_context.items())
            logger.warning(f"All attempts to get file content for {path} failed: {error_message}. PR context: {context_str}")
        else:
            logger.warning(f"All attempts to get file content for {path} failed: {error_message}")
        
        # Return empty content instead of raising error to prevent blocking the review process
        logger.warning(f"Returning empty content for {path} to continue with review process")
        return ""  # Return empty string instead of raising exception
            
    def list_directory_contents(self, path: str, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """List contents of a directory
        
        Args:
            path: Directory path
            ref: Optional reference (branch/commit/tag)
            
        Returns:
            List of files and directories in the specified path
        """
        url = f"{self.base_url}/src"
        
        # Format the path properly
        if path and not path.endswith('/'):
            path = f"{path}/"
            
        # Add reference if provided
        if ref:
            url = f"{url}/{ref}/{path}"
        else:
            # Use default branch or main branch
            url = f"{url}/HEAD/{path}"
            
        params = {}
        if not path or path == "/":
            params = {"path": ""}
            
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            # Parse the response to get directory contents
            data = response.json()
            contents = []
            
            # Process values from Bitbucket response
            if "values" in data:
                for item in data["values"]:
                    contents.append({
                        "name": item.get("path", "").split("/")[-1],
                        "path": item.get("path", ""),
                        "type": "file" if item.get("type") == "commit_file" else "dir",
                        "size": item.get("size", 0)
                    })
                    
            return contents
        except requests.RequestException as e:
            logger.warning(f"Error listing directory contents for {path}: {str(e)}")
            return []
            
    def get_file_history(self, path: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get commit history for a specific file
        
        Args:
            path: File path
            limit: Maximum number of commits to retrieve
            
        Returns:
            List of commits affecting the file
        """
        url = f"{self.base_url}/commits"
        params = {
            "path": path,
            "pagelen": limit
        }
        
        try:
            response = requests.get(url, auth=self.auth, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            # Parse the response to get commit history
            data = response.json()
            history = []
            
            if "values" in data:
                for commit in data["values"]:
                    history.append({
                        "sha": commit.get("hash", ""),
                        "message": commit.get("message", ""),
                        "author": commit.get("author", {}).get("raw", ""),
                        "date": commit.get("date", "")
                    })
                    
            return history
        except requests.RequestException as e:
            logger.warning(f"Error getting file history for {path}: {str(e)}")
            return []
            
    def find_related_files(self, path: str, ref: Optional[str] = None) -> List[str]:
        """Find files related to the specified file
        
        This can include:
        - Files in the same directory
        - Files with similar names
        - Files with similar functionality
        
        Args:
            path: Path to the file
            ref: Optional reference (branch/commit/tag)
            
        Returns:
            List of related file paths
        """
        # Get the directory of the file
        directory = os.path.dirname(path)
        if not directory:
            directory = ""
            
        related_files = []
        
        try:
            # List contents of the directory
            directory_contents = self.list_directory_contents(directory, ref)
            file_name = os.path.basename(path)
            
            # Get file extension for finding files with similar types
            _, file_ext = os.path.splitext(file_name)
            
            for item in directory_contents:
                item_path = item.get("path", "")
                item_name = os.path.basename(item_path)
                
                # Skip the original file
                if item_path == path:
                    continue
                    
                # Add all files with the same extension
                if item.get("type") == "file" and os.path.splitext(item_name)[1] == file_ext:
                    related_files.append(item_path)
                
                # Add files with similar name (using simple heuristic)
                if item.get("type") == "file" and self._is_similar_filename(file_name, item_name):
                    related_files.append(item_path)
                    
            return related_files
        except Exception as e:
            logger.warning(f"Error finding related files for {path}: {str(e)}")
            return []
            
    def _is_similar_filename(self, name1: str, name2: str) -> bool:
        """Check if two file names are similar
        
        Simple heuristic to detect related files.
        
        Args:
            name1: First file name
            name2: Second file name
            
        Returns:
            True if files appear to be related, False otherwise
        """
        # Remove extensions for comparison
        base1, ext1 = os.path.splitext(name1)
        base2, ext2 = os.path.splitext(name2)
        
        # Case 1: Same base name, different extensions (e.g., file.h and file.cpp)
        if base1 == base2 and ext1 != ext2:
            return True
            
        # Case 2: Test files
        if base1 == f"test_{base2}" or base2 == f"test_{base1}":
            return True
            
        if base1 == f"{base2}_test" or base2 == f"{base1}_test":
            return True
            
        # Case 3: Implementation/interface pairs
        # E.g., "interface.ts" and "interface.impl.ts"
        if base2.startswith(f"{base1}.") or base1.startswith(f"{base2}."):
            return True
        
        return False
    
    def batch_add_comments(self, pr_id: int, comments: List[Dict[str, Any]], max_batch_size: int = 5) -> List[Dict[str, Any]]:
        """Add multiple comments efficiently with smart batching
        
        Args:
            pr_id: Pull request ID
            comments: List of comment dictionaries with 'body', 'filename', 'line' keys
            max_batch_size: Maximum number of comments to post in quick succession
            
        Returns:
            List of comment responses from API
        """
        results = []
        
        for i, comment in enumerate(comments):
            # Check rate limits before each batch
            if i % max_batch_size == 0 and i > 0:
                if self._check_rate_limit_preemptive():
                    logger.info(f"Rate limit check: pausing for 2 seconds after {i} comments")
                    time.sleep(2)
            
            try:
                if 'filename' in comment and 'line' in comment:
                    # Try inline comment first
                    result = self.add_review_comment(
                        pr_id, 
                        comment.get('commit_id', ''), 
                        comment['filename'], 
                        comment['body'], 
                        comment['line']
                    )
                else:
                    # Regular comment
                    result = self.add_comment(pr_id, comment['body'])
                
                results.append(result)
                
                # Small delay between individual comments (but much shorter than before)
                if i < len(comments) - 1:  # Don't sleep after the last comment
                    time.sleep(0.2)  # Very brief pause
                    
            except Exception as e:
                logger.error(f"Failed to post comment {i+1}/{len(comments)}: {e}")
                # Continue with other comments even if one fails
                continue
        
        return results
        
    def get_pull_request_commits(self, pr_id: int) -> List[Dict[str, Any]]:
        """Get commits in a pull request with their messages
        
        Args:
            pr_id: Pull request number
            
        Returns:
            List of commits with 'message' and 'sha' fields
        """
        url = f"{self.base_url}/pullrequests/{pr_id}/commits"
        
        try:
            response = requests.get(url, auth=self.auth, timeout=30)
            response.raise_for_status()
            commits_data = response.json()
            
            # Extract commit messages and SHAs from Bitbucket response
            commits = []
            for commit in commits_data.get('values', []):
                commits.append({
                    'sha': commit.get('hash', ''),
                    'message': commit.get('message', ''),
                    'author': commit.get('author', {}).get('display_name', ''),
                    'date': commit.get('date', '')
                })
            
            return commits
            
        except requests.RequestException as e:
            logger.warning(f"Error fetching commits for PR #{pr_id}: {str(e)}")
            return []
            
    def get_pull_request_comments(self, pr_id: int) -> List[Dict[str, Any]]:
        """Get all comments in a pull request
        
        Args:
            pr_id: Pull request number
            
        Returns:
            List of comments with standardized fields
        """
        comments = []
        
        try:
            # Bitbucket has a single endpoint for all PR comments - handle pagination
            url = f"{self.base_url}/pullrequests/{pr_id}/comments"
            page_len = 100  # Increase page size to get more comments per request
            
            while url:
                # Add pagination parameters
                params = {'pagelen': page_len}
                response = requests.get(url, auth=self.auth, params=params, timeout=30)
                response.raise_for_status()
                comments_data = response.json()
                
                # Process comments from this page
                page_comments = comments_data.get('values', [])
                logger.debug(f"Retrieved {len(page_comments)} comments from page")
                
                # Get next page URL if it exists
                url = comments_data.get('next')
                
                # Process each comment on this page
                for comment in page_comments:
                    # Extract file path and line number for inline comments
                    file_path = None
                    line_number = None
                    if 'inline' in comment and comment['inline']:
                        file_path = comment['inline'].get('path')
                        line_number = comment['inline'].get('to')  # Line number the comment refers to
                    
                    # Debug and extract author field - try multiple possible locations
                    user_data = comment.get('user', {})
                    author_data = comment.get('author', {})
                    
                    author = (
                        user_data.get('username', '') or
                        user_data.get('nickname', '') or 
                        user_data.get('display_name', '') or
                        author_data.get('username', '') or
                        author_data.get('username', '') or
                        author_data.get('nickname', '') or
                        author_data.get('display_name', '') or
                        # Try other common Bitbucket fields
                        comment.get('creator', {}).get('username', '') or
                        comment.get('creator', {}).get('nickname', '') or
                        comment.get('creator', {}).get('display_name', '') or
                        str(comment.get('created_by', ''))
                    )
                    
                    # Log debug info for first comment
                    if not comments:  # Only log for first comment to avoid spam
                        logger.debug(f"Bitbucket comment structure: {comment}")
                        logger.debug(f"User data: {user_data}")
                        logger.debug(f"Extracted author: '{author}'")
                    
                    comments.append({
                        'id': comment.get('id'),
                        'body': comment.get('content', {}).get('raw', ''),
                        'author': author,
                        'created_at': comment.get('created_on', ''),
                        'updated_at': comment.get('updated_on', ''),
                        'file_path': file_path,
                        'line_number': line_number,
                        'resolved': comment.get('resolved', False)  # Bitbucket supports resolved status
                    })
            
            logger.info(f"Retrieved {len(comments)} total comments from {pr_id} across all pages")
            
            return comments
            
        except requests.RequestException as e:
            logger.warning(f"Error fetching comments for PR #{pr_id}: {str(e)}")
            return []