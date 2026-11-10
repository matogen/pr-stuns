"""
Data models for the PR Review Bot.
"""

from pr_reviewer.models.pull_request import PullRequest, FileChange

__all__ = ['PullRequest', 'FileChange']