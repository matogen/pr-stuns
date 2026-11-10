#!/usr/bin/env python3
"""
GitHub client implementation for repository interactions.
"""

import os
import time
from typing import List, Dict, Any, Optional
import requests
from pr_reviewer.clients.base import BaseRepoClient

# GitHub returns at most 100 items per page; loop until a short/empty page,
# capped to avoid runaway pagination against a misbehaving API.
_MAX_PAGES = 30


class GitHubClient(BaseRepoClient):
    """Client for interacting with GitHub API"""

    def __init__(self, token: str, owner: str, repo: str):
        """Initialize GitHub client

        Args:
            token: GitHub API token
            owner: Repository owner (username or organization)
            repo: Repository name
        """
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{owner}/{repo}"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }

    def _request(self, method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
        """Make an HTTP request with retry on rate limits (429) and transient server errors.

        Args:
            method: HTTP method ('get', 'post', 'put', 'delete', 'patch')
            url: Request URL
            max_retries: Maximum number of attempts before giving up
            **kwargs: Passed through to requests.request

        Returns:
            The final Response object (may still carry an error status after retries)
        """
        from pr_reviewer.utils.logging import logger
        kwargs.setdefault("timeout", 30)
        delay = 1
        response = None
        for attempt in range(max_retries):
            response = requests.request(method, url, **kwargs)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < max_retries - 1:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                logger.warning(
                    f"GitHub API {response.status_code} on {method.upper()} {url}; "
                    f"retrying in {wait}s (attempt {attempt+1}/{max_retries})"
                )
                time.sleep(wait)
                delay *= 2
                continue
            return response
        return response

    def get_pull_requests(self, state: str = "open", sort: str = "created",
                          direction: str = "desc") -> List[Dict[str, Any]]:
        """Get pull requests for the repository

        Args:
            state: Pull request state filter ('open', 'closed', 'all')
            sort: Sort field ('created', 'updated', 'popularity', 'long-running')
            direction: Sort direction ('asc' or 'desc')

        Returns:
            List of pull request data
        """
        url = f"{self.base_url}/pulls"
        pulls = []
        for page in range(1, _MAX_PAGES + 1):
            params = {"state": state, "sort": sort, "direction": direction, "page": page, "per_page": 100}
            response = self._request('get', url, headers=self.headers, params=params)
            response.raise_for_status()
            page_data = response.json()
            if not page_data:
                break
            pulls.extend(page_data)
            if len(page_data) < 100:
                break
        return pulls

    def get_pull_request_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Get the files changed in a pull request

        Args:
            pr_number: Pull request number

        Returns:
            List of file change information
        """
        url = f"{self.base_url}/pulls/{pr_number}/files"
        files = []
        for page in range(1, _MAX_PAGES + 1):
            params = {"page": page, "per_page": 100}
            response = self._request('get', url, headers=self.headers, params=params)
            response.raise_for_status()
            page_data = response.json()
            if not page_data:
                break
            files.extend(page_data)
            if len(page_data) < 100:
                break
        return files
    
    def get_file_content(self, path: str, ref: Optional[str] = None, pr_id: Optional[int] = None) -> str:
        """Get full content of a file at a specific reference (branch/commit)
        
        Args:
            path: Path to the file
            ref: Optional reference (branch/commit/tag), defaults to default branch
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            String content of the file
        """
        # For GitHub, we need to prioritize the PR branch content for new files
        # Try multiple approaches in case the first one fails
        errors = []
        
        # Try with the specified reference first (PR head commit)
        url = f"{self.base_url}/contents/{path}"
        params = {}
        if ref:
            params["ref"] = ref
            
        try:
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            content_data = response.json()
            
            # GitHub returns content as base64 encoded
            import base64
            if content_data.get("encoding") == "base64" and "content" in content_data:
                content = base64.b64decode(content_data["content"]).decode('utf-8')
                return content
            else:
                errors.append(f"Unexpected content format for file {path}")
        except Exception as e:
            errors.append(f"Error with specified ref {ref!r}: {str(e)}")
            
        # If the first attempt failed and ref was provided, try without a specific ref
        # This handles files on the default branch
        if ref:
            try:
                url = f"{self.base_url}/contents/{path}"
                response = self._request('get', url, headers=self.headers, timeout=30)
                response.raise_for_status()
                
                content_data = response.json()
                
                # GitHub returns content as base64 encoded
                import base64
                if content_data.get("encoding") == "base64" and "content" in content_data:
                    content = base64.b64decode(content_data["content"]).decode('utf-8')
                    return content
                else:
                    errors.append("Unexpected content format on second attempt")
            except Exception as e:
                errors.append(f"Error with default ref: {str(e)}")
                
        # Last attempt: Try to find the file in open PRs
        # This is a more complex approach that requires additional API calls
        try:
            # Get open PRs
            url = f"{self.base_url}/pulls"
            params = {"state": "open"}
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            prs = response.json()
            
            # Look for PRs that might have this file
            for pr in prs:
                head_sha = pr.get("head", {}).get("sha")
                if head_sha:
                    try:
                        # Check if the file exists in this PR's head
                        url = f"{self.base_url}/contents/{path}"
                        params = {"ref": head_sha}
                        response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                        if response.status_code == 200:
                            content_data = response.json()
                            if content_data.get("encoding") == "base64" and "content" in content_data:
                                content = base64.b64decode(content_data["content"]).decode('utf-8')
                                return content
                    except:
                        # Continue to next PR
                        pass
        except Exception as e:
            errors.append(f"Error checking PRs: {str(e)}")
            
        # If all attempts failed, raise an error with details
        error_message = "; ".join(errors)
        from pr_reviewer.utils.logging import logger
        logger.warning(f"All attempts to get file content for {path} failed: {error_message}")
        raise ValueError(f"Could not retrieve file content: {error_message}")
    
    def get_file_by_sha(self, sha: str) -> str:
        """Get content of a file by its SHA
        
        Args:
            sha: File SHA
            
        Returns:
            File content
        """
        url = f"{self.base_url}/git/blobs/{sha}"
        response = self._request('get', url, headers=self.headers, timeout=30)
        response.raise_for_status()
        
        # GitHub returns content as base64 encoded
        import base64
        content_data = response.json()
        if content_data.get("encoding") == "base64" and "content" in content_data:
            content = base64.b64decode(content_data["content"]).decode('utf-8')
            return content
        else:
            raise ValueError(f"Unexpected content format for blob {sha}")
    
    def add_comment(self, pr_number: int, body: str) -> Dict[str, Any]:
        """Add a comment to a pull request
        
        Args:
            pr_number: Pull request number
            body: Comment text
            
        Returns:
            Comment data from API response
        """
        url = f"{self.base_url}/issues/{pr_number}/comments"
        data = {"body": body}
        response = self._request('post', url, headers=self.headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def add_review_comment(self, pr_number: int, commit_id: str, path: str, 
                           body: str, line: int) -> Dict[str, Any]:
        """Add a review comment to a specific line in a PR
        
        Args:
            pr_number: Pull request number
            commit_id: Commit SHA
            path: File path
            body: Comment text
            line: Line number
            
        Returns:
            Comment data from API response
        """
        try:
            url = f"{self.base_url}/pulls/{pr_number}/comments"
            data = {
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": "RIGHT"  # Comment on the new version of the file
            }
            response = self._request('post', url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            # If that fails, try a different approach without the line number
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Failed to add line-specific comment, falling back to alternative approach: {e}")
            
            try:
                # Try alternative approach with position instead of line
                # Get the file diff
                diff_url = f"{self.base_url}/pulls/{pr_number}/files"
                diff_response = self._request('get', diff_url, headers=self.headers, timeout=30)
                diff_response.raise_for_status()
                files = diff_response.json()
                
                # Find the right file
                file_diff = None
                for file in files:
                    if file.get("filename") == path:
                        file_diff = file
                        break
                
                if file_diff and "patch" in file_diff:
                    # If we found the diff, create a regular PR comment with file reference
                    return self.add_comment(pr_number, f"**Comment for {path} (line {line}):**\n\n{body}")
                else:
                    # Otherwise just post a regular comment
                    return self.add_comment(pr_number, f"**Comment for {path} (line {line}):**\n\n{body}")
            except Exception as e2:
                logger.error(f"Alternative comment approach failed: {e2}")
                # Final fallback - post a regular comment
                return self.add_comment(pr_number, f"**Comment for {path} (line {line}):**\n\n{body}")
        
    def remove_user_as_watcher(self, pr_number: int, username: str) -> bool:
        """Remove a user as a watcher from a pull request in GitHub
        
        Args:
            pr_number: Pull request number
            username: Username to remove as watcher
            
        Returns:
            True if successful, False otherwise
        """
        # In GitHub, watching/unwatching is done at the repository level
        # and not at the PR level. For PRs, users can subscribe/unsubscribe.
        url = f"{self.base_url}/issues/{pr_number}/subscription"
        
        try:
            # Check if the user is subscribed
            response = self._request('get', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            # If subscribed, delete the subscription
            if response.status_code == 200 and response.json().get("subscribed", False):
                delete_response = self._request('delete', url, headers=self.headers, timeout=30)
                delete_response.raise_for_status()
                return True
                
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error removing user as watcher from PR #{pr_number}: {str(e)}")
            
        return False
        
    def get_commit_changed_files(self, commit_id: str) -> List[Dict[str, Any]]:
        """Get the files changed in a specific commit
        
        Args:
            commit_id: Commit SHA
            
        Returns:
            List of file changes in the commit
        """
        url = f"{self.base_url}/commits/{commit_id}"
        
        try:
            response = self._request('get', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            commit_data = response.json()
            
            # GitHub commit API doesn't directly return files, so we need to fetch them separately
            files_url = f"{url}/files"
            files_response = self._request('get', files_url, headers=self.headers, timeout=30)
            files_response.raise_for_status()
            
            return files_response.json()
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching changed files for commit {commit_id}: {str(e)}")
            return []
            
    def list_directory_contents(self, path: str, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """List contents of a directory
        
        Args:
            path: Directory path
            ref: Optional reference (branch/commit/tag)
            
        Returns:
            List of files and directories in the specified path
        """
        url = f"{self.base_url}/contents/{path}"
        params = {}
        if ref:
            params["ref"] = ref
            
        try:
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            contents = response.json()
            
            # GitHub returns a list for directories and a single object for files
            if isinstance(contents, list):
                return contents
            else:
                # If it's a file, return as a single-item list
                return [contents]
                
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
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
            "per_page": limit
        }
        
        try:
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            commits = response.json()
            
            # Process commits to extract just the necessary information
            return [
                {
                    "sha": commit.get("sha", ""),
                    "message": commit.get("commit", {}).get("message", ""),
                    "author": commit.get("commit", {}).get("author", {}).get("name", ""),
                    "date": commit.get("commit", {}).get("author", {}).get("date", "")
                }
                for commit in commits
            ]
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching file history for {path}: {str(e)}")
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
        
        # If it's in the root directory, use empty string
        directory = directory if directory else ""
        
        # Get all files in the same directory
        related_files = []
        
        try:
            # List contents of the directory
            directory_contents = self.list_directory_contents(directory, ref)
            file_name = os.path.basename(path)
            
            # Get file extension for finding files with similar types
            _, file_ext = os.path.splitext(file_name)
            
            for item in directory_contents:
                item_name = item.get("name", "")
                item_path = item.get("path", "")
                
                # Skip the original file
                if item_path == path:
                    continue
                    
                # Add all files with the same extension
                if item.get("type") == "file" and os.path.splitext(item_name)[1] == file_ext:
                    related_files.append(item_path)
                
                # Add files with similar name (simple heuristic)
                if item.get("type") == "file" and self._is_similar_filename(file_name, item_name):
                    related_files.append(item_path)
                        
            return related_files
            
        except Exception as e:
            from pr_reviewer.utils.logging import logger
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
        import os
        
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
        
    def get_pull_request_commits(self, pr_id: int) -> List[Dict[str, Any]]:
        """Get commits in a pull request with their messages
        
        Args:
            pr_id: Pull request number
            
        Returns:
            List of commits with 'message' and 'sha' fields
        """
        url = f"{self.base_url}/pulls/{pr_id}/commits"
        
        try:
            response = self._request('get', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            commits_data = response.json()
            
            # Extract commit messages and SHAs
            commits = []
            for commit in commits_data:
                commits.append({
                    'sha': commit.get('sha', ''),
                    'message': commit.get('commit', {}).get('message', ''),
                    'author': commit.get('commit', {}).get('author', {}).get('name', ''),
                    'date': commit.get('commit', {}).get('author', {}).get('date', '')
                })
            
            return commits
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
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
            from pr_reviewer.utils.logging import logger
            
            # Get general PR comments (issue comments) with pagination
            url = f"{self.base_url}/issues/{pr_id}/comments"
            page = 1
            per_page = 100
            
            while url:
                params = {'page': page, 'per_page': per_page}
                response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                response.raise_for_status()
                issue_comments = response.json()
                
                if not issue_comments:  # No more comments
                    break
                    
                logger.debug(f"Retrieved {len(issue_comments)} issue comments from page {page}")
                
                for comment in issue_comments:
                    comments.append({
                        'id': comment.get('id'),
                        'body': comment.get('body', ''),
                        'author': comment.get('user', {}).get('login', ''),
                        'created_at': comment.get('created_at', ''),
                        'updated_at': comment.get('updated_at', ''),
                        'file_path': None,  # General comments don't have file paths
                        'line_number': None,  # General comments don't have line numbers
                        'resolved': None  # GitHub doesn't track resolution for issue comments
                    })
                
                # Check if there are more pages (GitHub uses Link header)
                if len(issue_comments) < per_page:
                    break
                page += 1
            
            # Get review comments (line-specific comments) with pagination
            url = f"{self.base_url}/pulls/{pr_id}/comments"
            page = 1
            
            while url:
                params = {'page': page, 'per_page': per_page}
                response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                response.raise_for_status()
                review_comments = response.json()
                
                if not review_comments:  # No more comments
                    break
                    
                logger.debug(f"Retrieved {len(review_comments)} review comments from page {page}")
                
                for comment in review_comments:
                    comments.append({
                        'id': comment.get('id'),
                        'body': comment.get('body', ''),
                        'author': comment.get('user', {}).get('login', ''),
                        'created_at': comment.get('created_at', ''),
                        'updated_at': comment.get('updated_at', ''),
                        'file_path': comment.get('path'),
                        'line_number': comment.get('line') or comment.get('original_line'),
                        'resolved': None  # GitHub doesn't have a simple resolved flag for review comments
                    })
                
                # Check if there are more pages
                if len(review_comments) < per_page:
                    break
                page += 1
            
            logger.info(f"Retrieved {len(comments)} total comments from GitHub PR #{pr_id} across all pages")
            return comments
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching comments for PR #{pr_id}: {str(e)}")
            return []