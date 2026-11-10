#!/usr/bin/env python3
"""
GitLab client implementation for repository interactions.
"""

import os
import base64
import time
from typing import List, Dict, Any, Optional
import requests
import urllib.parse
from pr_reviewer.clients.base import BaseRepoClient
from pr_reviewer.utils.logging import logger

# GitLab returns at most 100 items per page; loop until a short/empty page,
# capped to avoid runaway pagination against a misbehaving API.
_MAX_PAGES = 30


class GitLabClient(BaseRepoClient):
    """Client for interacting with GitLab API"""

    def __init__(self, token: str, gitlab_url: str, project_id: str):
        """Initialize GitLab client

        Args:
            token: GitLab API token
            gitlab_url: Base URL of GitLab instance
            project_id: Project ID or path
        """
        self.token = token
        self.gitlab_url = gitlab_url.rstrip('/')
        self.project_id = project_id
        self.base_url = f"{self.gitlab_url}/api/v4/projects/{project_id}"
        self.headers = {
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json"
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
                    f"GitLab API {response.status_code} on {method.upper()} {url}; "
                    f"retrying in {wait}s (attempt {attempt+1}/{max_retries})"
                )
                time.sleep(wait)
                delay *= 2
                continue
            return response
        return response

    def get_pull_requests(self, state: str = "opened", sort: str = "created_at") -> List[Dict[str, Any]]:
        """Get merge requests for the repository

        Args:
            state: Merge request state filter ('opened', 'closed', 'locked', 'merged')
            sort: Sort field ('created_at', 'updated_at')

        Returns:
            List of merge request data
        """
        url = f"{self.base_url}/merge_requests"
        merge_requests = []
        for page in range(1, _MAX_PAGES + 1):
            params = {"state": state, "order_by": sort, "sort": "desc", "page": page, "per_page": 100}
            response = self._request('get', url, headers=self.headers, params=params)
            response.raise_for_status()
            page_data = response.json()
            if not page_data:
                break
            merge_requests.extend(page_data)
            if len(page_data) < 100:
                break
        return merge_requests
    
    def get_pull_request_files(self, mr_iid: int) -> List[Dict[str, Any]]:
        """Get the changes in a merge request
        
        Args:
            mr_iid: Merge request internal ID
            
        Returns:
            List of file change information
        """
        url = f"{self.base_url}/merge_requests/{mr_iid}/changes"
        response = self._request('get', url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json().get("changes", [])
    
    def add_comment(self, mr_iid: int, body: str) -> Dict[str, Any]:
        """Add a comment to a merge request
        
        Args:
            mr_iid: Merge request internal ID
            body: Comment text
            
        Returns:
            Comment data from API response
        """
        url = f"{self.base_url}/merge_requests/{mr_iid}/notes"
        data = {"body": body}
        response = self._request('post', url, headers=self.headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def add_review_comment(self, mr_iid: int, commit_id: str, path: str, 
                           body: str, line: int) -> Dict[str, Any]:
        """Add a review comment to a specific line in a merge request
        
        Args:
            mr_iid: Merge request internal ID
            commit_id: Commit SHA
            path: File path
            body: Comment text
            line: Line number
            
        Returns:
            Comment data from API response
        """
        try:
            url = f"{self.base_url}/merge_requests/{mr_iid}/discussions"
            data = {
                "body": body,
                "position": {
                    "base_sha": commit_id,
                    "start_sha": commit_id,
                    "head_sha": commit_id,
                    "position_type": "text",
                    "new_path": path,
                    "new_line": line
                }
            }
            response = self._request('post', url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            # If that fails, try a different approach
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Failed to add line-specific comment, falling back to regular comment: {e}")
            
            # Try with different position_type
            try:
                url = f"{self.base_url}/merge_requests/{mr_iid}/discussions"
                data = {
                    "body": body,
                    "position": {
                        "base_sha": commit_id,
                        "start_sha": commit_id,
                        "head_sha": commit_id,
                        "position_type": "line",  # Try with line type instead
                        "new_path": path,
                        "new_line": line
                    }
                }
                response = self._request('post', url, headers=self.headers, json=data, timeout=30)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e2:
                logger.warning(f"Second approach failed: {e2}, falling back to regular comment")
                
                # Create a note with file reference instead
                url = f"{self.base_url}/merge_requests/{mr_iid}/notes"
                data = {"body": f"**Comment for {path} (line {line}):**\n\n{body}"}
                response = self._request('post', url, headers=self.headers, json=data, timeout=30)
                response.raise_for_status()
                return response.json()
        
    def remove_user_as_watcher(self, mr_iid: int, username: str) -> bool:
        """Remove a user as a watcher from a merge request in GitLab
        
        Args:
            mr_iid: Merge request internal ID
            username: Username to remove as watcher
            
        Returns:
            True if successful, False otherwise
        """
        # In GitLab, you need to unsubscribe from merge requests
        url = f"{self.base_url}/merge_requests/{mr_iid}/unsubscribe"
        
        try:
            # Unsubscribe from the merge request
            response = self._request('post', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            # If successful, GitLab returns the updated merge request
            if response.status_code == 200:
                return True
                
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error removing user as watcher from MR #{mr_iid}: {str(e)}")
            
        return False
        
    def get_commit_changed_files(self, commit_id: str) -> List[Dict[str, Any]]:
        """Get the files changed in a specific commit
        
        Args:
            commit_id: Commit SHA
            
        Returns:
            List of file changes in the commit
        """
        url = f"{self.base_url}/repository/commits/{commit_id}/diff"
        
        try:
            response = self._request('get', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            # GitLab returns a list of diff objects
            diff_data = response.json()
            
            # Convert to a standard format similar to what GitHub returns
            result = []
            for diff in diff_data:
                status = "modified"
                if diff.get("new_file", False):
                    status = "added"
                elif diff.get("deleted_file", False):
                    status = "removed"
                elif diff.get("renamed_file", False):
                    status = "renamed"
                    
                result.append({
                    "filename": diff.get("new_path", ""),
                    "previous_filename": diff.get("old_path", ""),
                    "status": status,
                    "additions": diff.get("diff", "").count("\n+") if diff.get("diff") else 0,
                    "deletions": diff.get("diff", "").count("\n-") if diff.get("diff") else 0,
                    "changes": 0,  # GitLab doesn't provide this directly
                    "patch": diff.get("diff", "")
                })
                
            return result
            
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
        # For GitLab, we need to prioritize the PR branch content for new files
        # Try multiple approaches in case the first one fails
        errors = []
        
        # URL encode the file path
        encoded_path = urllib.parse.quote_plus(path)
        
        # First attempt: Try with provided reference (usually PR head commit)
        if ref:
            url = f"{self.base_url}/repository/files/{encoded_path}"
            params = {"ref": ref}
            
            try:
                response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                response.raise_for_status()
                
                # GitLab returns a JSON object with the file content base64 encoded
                content_data = response.json()
                
                if "content" in content_data:
                    # Decode base64 content
                    content = base64.b64decode(content_data["content"]).decode('utf-8')
                    return content
                else:
                    errors.append(f"No content found with ref '{ref}'")
            except requests.RequestException as e:
                errors.append(f"Error with ref '{ref}': {str(e)}")
        
        # Second attempt: Try with default branch (main or master)
        for default_branch in ["main", "master", "development"]:
            url = f"{self.base_url}/repository/files/{encoded_path}"
            params = {"ref": default_branch}
            
            try:
                response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                if response.status_code == 200:
                    content_data = response.json()
                    
                    if "content" in content_data:
                        # Decode base64 content
                        content = base64.b64decode(content_data["content"]).decode('utf-8')
                        return content
            except:
                # Try next default branch
                pass
        
        errors.append("File not found in any default branch")
        
        # Last attempt: Try to find the file in MR source branches
        try:
            # Get open MRs
            url = f"{self.base_url}/merge_requests"
            params = {"state": "opened"}
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            
            if response.status_code == 200:
                mrs = response.json()
                
                # Look for MRs that might have this file
                for mr in mrs:
                    source_branch = mr.get("source_branch")
                    if source_branch:
                        url = f"{self.base_url}/repository/files/{encoded_path}"
                        params = {"ref": source_branch}
                        
                        try:
                            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                            if response.status_code == 200:
                                content_data = response.json()
                                
                                if "content" in content_data:
                                    # Decode base64 content
                                    content = base64.b64decode(content_data["content"]).decode('utf-8')
                                    return content
                        except:
                            # Continue to next MR
                            pass
        except Exception as e:
            errors.append(f"Error checking MRs: {str(e)}")
            
        # If all attempts failed, raise an error with details
        error_message = "; ".join(errors)
        logger.warning(f"All attempts to get file content for {path} failed: {error_message}")
        raise ValueError(f"Could not retrieve file content: {error_message}")
            
    def list_directory_contents(self, path: str, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """List contents of a directory
        
        Args:
            path: Directory path
            ref: Optional reference (branch/commit/tag)
            
        Returns:
            List of files and directories in the specified path
        """
        url = f"{self.base_url}/repository/tree"
        
        params = {
            "path": path if path else "",
            "ref": ref if ref else "main"  # Use default branch if not specified
        }
        
        try:
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            # GitLab returns a list of tree items
            tree_items = response.json()
            contents = []
            
            for item in tree_items:
                contents.append({
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "type": "file" if item.get("type") == "blob" else "dir",
                    "id": item.get("id", "")
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
        url = f"{self.base_url}/repository/commits"
        
        params = {
            "path": path,
            "per_page": limit
        }
        
        try:
            response = self._request('get', url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            # GitLab returns a list of commits
            commits = response.json()
            history = []
            
            for commit in commits:
                history.append({
                    "sha": commit.get("id", ""),
                    "message": commit.get("message", ""),
                    "author": commit.get("author_name", ""),
                    "date": commit.get("created_at", "")
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
                item_name = item.get("name", "")
                
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
        
    def get_pull_request_commits(self, pr_id: int) -> List[Dict[str, Any]]:
        """Get commits in a pull request with their messages
        
        Args:
            pr_id: Pull request (merge request) number
            
        Returns:
            List of commits with 'message' and 'sha' fields
        """
        url = f"{self.base_url}/merge_requests/{pr_id}/commits"
        
        try:
            response = self._request('get', url, headers=self.headers, timeout=30)
            response.raise_for_status()
            commits_data = response.json()
            
            # Extract commit messages and SHAs from GitLab response
            commits = []
            for commit in commits_data:
                commits.append({
                    'sha': commit.get('id', ''),
                    'message': commit.get('message', ''),
                    'author': commit.get('author_name', ''),
                    'date': commit.get('authored_date', '')
                })
            
            return commits
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching commits for MR #{pr_id}: {str(e)}")
            return []
            
    def get_pull_request_comments(self, pr_id: int) -> List[Dict[str, Any]]:
        """Get all comments in a merge request
        
        Args:
            pr_id: Merge request number
            
        Returns:
            List of comments with standardized fields
        """
        comments = []
        
        try:
            from pr_reviewer.utils.logging import logger
            
            # GitLab uses "notes" for all types of comments - handle pagination
            url = f"{self.base_url}/merge_requests/{pr_id}/notes"
            page = 1
            per_page = 100
            
            while url:
                params = {'page': page, 'per_page': per_page}
                response = self._request('get', url, headers=self.headers, params=params, timeout=30)
                response.raise_for_status()
                notes_data = response.json()
                
                if not notes_data:  # No more notes
                    break
                    
                logger.debug(f"Retrieved {len(notes_data)} notes from GitLab page {page}")
                
                for note in notes_data:
                    # Check if this is a diff note (line-specific comment)
                    file_path = None
                    line_number = None
                    if note.get('position'):
                        position = note['position']
                        file_path = position.get('new_path') or position.get('old_path')
                        line_number = position.get('new_line') or position.get('old_line')
                    
                    comments.append({
                        'id': note.get('id'),
                        'body': note.get('body', ''),
                        'author': note.get('author', {}).get('username', ''),
                        'created_at': note.get('created_at', ''),
                        'updated_at': note.get('updated_at', ''),
                        'file_path': file_path,
                        'line_number': line_number,
                        'resolved': note.get('resolved', False)  # GitLab supports resolved status
                    })
                
                # Check if there are more pages
                if len(notes_data) < per_page:
                    break
                page += 1
            
            logger.info(f"Retrieved {len(comments)} total comments from GitLab MR #{pr_id} across all pages")
            return comments
            
        except requests.RequestException as e:
            from pr_reviewer.utils.logging import logger
            logger.warning(f"Error fetching comments for MR #{pr_id}: {str(e)}")
            return []