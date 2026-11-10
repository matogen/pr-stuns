"""
Repository API client implementations.
"""

from typing import Dict, Any, Optional

from pr_reviewer.clients.base import BaseRepoClient
from pr_reviewer.clients.github import GitHubClient
from pr_reviewer.clients.gitlab import GitLabClient
from pr_reviewer.clients.bitbucket import BitbucketClient


def create_client(repo_config: Dict[str, Any]) -> BaseRepoClient:
    """Factory function to create appropriate repository client
    
    Args:
        repo_config: Repository configuration dictionary
        
    Returns:
        Repository client instance
        
    Raises:
        ValueError: If repository type is not supported
    """
    repo_type = repo_config.get("type", "github").lower()
    
    if repo_type == "github":
        return GitHubClient(
            token=repo_config["token"],
            owner=repo_config["owner"],
            repo=repo_config["repo"]
        )
    elif repo_type == "gitlab":
        return GitLabClient(
            token=repo_config["token"],
            gitlab_url=repo_config["url"],
            project_id=repo_config["project_id"]
        )
    elif repo_type == "bitbucket":
        # Check for fallback users configuration
        fallback_users = repo_config.get("fallback_users", [])
        return BitbucketClient(
            workspace=repo_config["workspace"],
            repo_slug=repo_config["repo_slug"],
            username=repo_config.get("username"),
            app_password=repo_config.get("app_password"),
            api_token=repo_config.get("api_token"),
            email=repo_config.get("email"),
            bitbucket_url=repo_config.get("url", "https://api.bitbucket.org/2.0"),
            fallback_users=fallback_users,
        )
    else:
        raise ValueError(f"Unsupported repository type: {repo_type}")


__all__ = ['BaseRepoClient', 'GitHubClient', 'GitLabClient', 'BitbucketClient', 'create_client']