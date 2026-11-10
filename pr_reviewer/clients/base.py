#!/usr/bin/env python3
"""
Base client for repository interactions.
Defines the common interface that all repository clients must implement.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple


class BaseRepoClient(ABC):
    """Abstract base class for repository clients"""
    
    @abstractmethod
    def get_pull_requests(self, **kwargs) -> List[Dict[str, Any]]:
        """Get pull requests for the repository
        
        Returns:
            List of pull request data
        """
        pass
    
    @abstractmethod
    def get_pull_request_files(self, pr_id: Any) -> List[Dict[str, Any]]:
        """Get files changed in a pull request
        
        Args:
            pr_id: Pull request identifier
            
        Returns:
            List of file changes
        """
        pass
    
    @abstractmethod
    def add_comment(self, pr_id: Any, body: str) -> Dict[str, Any]:
        """Add a comment to a pull request
        
        Args:
            pr_id: Pull request identifier
            body: Comment text
            
        Returns:
            Response data from the API
        """
        pass
    
    @abstractmethod
    def add_review_comment(self, pr_id: Any, commit_id: str, path: str, body: str, line: int) -> Dict[str, Any]:
        """Add a review comment to a specific line in a pull request
        
        Args:
            pr_id: Pull request identifier
            commit_id: Commit SHA
            path: File path
            body: Comment text
            line: Line number
            
        Returns:
            Response data from the API
        """
        pass
        
    @abstractmethod
    def remove_user_as_watcher(self, pr_id: Any, username: str) -> bool:
        """Remove a user as a watcher from a pull request
        
        Args:
            pr_id: Pull request identifier
            username: Username to remove as watcher
            
        Returns:
            True if successful, False otherwise
        """
        pass
        
    @abstractmethod
    def get_commit_changed_files(self, commit_id: str) -> List[Dict[str, Any]]:
        """Get the files changed in a specific commit
        
        Args:
            commit_id: Commit SHA
            
        Returns:
            List of file changes in the commit
        """
        pass
        
    @abstractmethod
    def get_pull_request_commits(self, pr_id: Any) -> List[Dict[str, Any]]:
        """Get commits in a pull request with their messages
        
        Args:
            pr_id: Pull request identifier
            
        Returns:
            List of commits with 'message' and 'sha' fields
        """
        pass
        
    @abstractmethod
    def get_pull_request_comments(self, pr_id: Any) -> List[Dict[str, Any]]:
        """Get all comments in a pull request
        
        Args:
            pr_id: Pull request identifier
            
        Returns:
            List of comments with standardized fields:
            - id: Comment ID
            - body: Comment text
            - author: Username of comment author
            - created_at: When comment was created
            - updated_at: When comment was last updated
            - file_path: File path (for line comments, None for general comments)
            - line_number: Line number (for line comments, None for general comments)
            - resolved: Whether comment is marked as resolved (if supported by platform)
        """
        pass
        
    @abstractmethod
    def get_file_content(self, path: str, ref: Optional[str] = None, pr_id: Optional[int] = None) -> str:
        """Get full content of a file at a specific reference (branch/commit)
        
        Args:
            path: Path to the file
            ref: Optional reference (branch/commit/tag), defaults to default branch
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            String content of the file
        """
        pass
        
    @abstractmethod
    def list_directory_contents(self, path: str, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """List contents of a directory
        
        Args:
            path: Directory path
            ref: Optional reference (branch/commit/tag)
            
        Returns:
            List of files and directories in the specified path
        """
        pass
        
    @abstractmethod
    def get_file_history(self, path: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get commit history for a specific file
        
        Args:
            path: File path
            limit: Maximum number of commits to retrieve
            
        Returns:
            List of commits affecting the file
        """
        pass
        
    @abstractmethod
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
        pass