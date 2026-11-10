#!/usr/bin/env python3
"""
Data models for pull request information.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class FileChange:
    """Representation of a file change in a pull request"""
    
    filename: str
    status: str  # 'added', 'modified', 'removed'
    patch: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    is_recent_change: bool = False  # Flag to indicate file was changed in the newest commit
    
    @staticmethod
    def from_github_data(data: Dict[str, Any]) -> 'FileChange':
        """Create a FileChange instance from GitHub API data
        
        Args:
            data: GitHub API file change data
            
        Returns:
            FileChange instance
        """
        return FileChange(
            filename=data.get("filename", ""),
            status=data.get("status", "modified"),
            patch=data.get("patch", ""),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changes=data.get("changes", 0)
        )
    
    @staticmethod
    def from_gitlab_data(data: Dict[str, Any]) -> 'FileChange':
        """Create a FileChange instance from GitLab API data
        
        Args:
            data: GitLab API file change data
            
        Returns:
            FileChange instance
        """
        return FileChange(
            filename=data.get("new_path", ""),
            status="renamed" if data.get("renamed_file", False) else 
                   "added" if data.get("new_file", False) else
                   "removed" if data.get("deleted_file", False) else "modified",
            patch=data.get("diff", ""),
            # GitLab doesn't provide these stats directly
            additions=0,
            deletions=0,
            changes=0
        )
    
    @staticmethod
    def from_bitbucket_data(data: Dict[str, Any]) -> 'FileChange':
        """Create a FileChange instance from Bitbucket API data
        
        Args:
            data: Bitbucket API file change data
            
        Returns:
            FileChange instance
        """
        return FileChange(
            filename=data.get("filename", ""),
            status=data.get("status", "modified"),
            patch=data.get("patch", ""),
            # Bitbucket doesn't provide these stats directly
            additions=0,
            deletions=0,
            changes=0
        )


@dataclass
class PullRequest:
    """Representation of a pull request"""
    
    id: Any  # Can be int or string depending on provider
    title: str
    author: str
    description: Optional[str] = None
    state: str = "open"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    head_commit: Optional[str] = None
    base_branch: Optional[str] = None
    head_branch: Optional[str] = None
    file_changes: List[FileChange] = field(default_factory=list)
    
    @staticmethod
    def from_github_data(data: Dict[str, Any]) -> 'PullRequest':
        """Create a PullRequest instance from GitHub API data
        
        Args:
            data: GitHub API pull request data
            
        Returns:
            PullRequest instance
        """
        return PullRequest(
            id=data.get("number"),
            title=data.get("title", ""),
            author=data.get("user", {}).get("login", ""),
            description=data.get("body", ""),
            state=data.get("state", "open"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            head_commit=data.get("head", {}).get("sha"),
            base_branch=data.get("base", {}).get("ref"),
            head_branch=data.get("head", {}).get("ref")
        )
    
    @staticmethod
    def from_gitlab_data(data: Dict[str, Any]) -> 'PullRequest':
        """Create a PullRequest instance from GitLab API data
        
        Args:
            data: GitLab API merge request data
            
        Returns:
            PullRequest instance
        """
        return PullRequest(
            id=data.get("iid"),
            title=data.get("title", ""),
            author=data.get("author", {}).get("username", ""),
            description=data.get("description", ""),
            state=data.get("state", "opened"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            head_commit=data.get("sha"),
            base_branch=data.get("target_branch"),
            head_branch=data.get("source_branch")
        )
    
    @staticmethod
    def from_bitbucket_data(data: Dict[str, Any]) -> 'PullRequest':
        """Create a PullRequest instance from Bitbucket API data
        
        Args:
            data: Bitbucket API pull request data
            
        Returns:
            PullRequest instance
        """
        from pr_reviewer.utils.logging import logger
        
        # Log the raw data to debug extraction issues
        logger.info(f"Creating PullRequest from Bitbucket data: {data.keys()}")
        
        extracted_id = data.get("id")
        extracted_head_commit = data.get("source", {}).get("commit", {}).get("hash")
        
        logger.info(f"Extracted PR ID: {extracted_id}, head_commit: {extracted_head_commit}")
        
        return PullRequest(
            id=extracted_id,
            title=data.get("title", ""),
            author=data.get("author", {}).get("display_name", ""),
            description=data.get("description", ""),
            state=data.get("state", "OPEN"),
            created_at=data.get("created_on"),
            updated_at=data.get("updated_on"),
            head_commit=extracted_head_commit,
            base_branch=data.get("destination", {}).get("branch", {}).get("name"),
            head_branch=data.get("source", {}).get("branch", {}).get("name")
        )