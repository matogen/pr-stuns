#!/usr/bin/env python3
"""
Minimal read-only Goby client for pulling task context into PR reviews.

Goby (https://your-team.goby.chat/api/docs) is the project-management tool
some projects use instead of Jira. Its task keys have the same shape as
Jira issue keys ("OPS-42"), so the branch-name extraction in
pr_reviewer.clients.jira.extract_issue_key is reused as-is.

API facts this client depends on (from /api/docs/openapi.json):
- Base URL is <site>/api/v1.
- Auth is "Authorization: Bearer gk_<publicId>_<secret>". A key belongs to
  a single team and needs the tasks:read scope; a task from another team
  is a 404, not a 403.
- GET /tasks/{key} returns title, description (plain string or null),
  status {name, semantic}, labels [{name}], url.
"""

import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_API_SUFFIX = "/api/v1"


class GobyClient:
    """Read-only Goby client for the fields a code review needs."""

    def __init__(self, base_url: str, api_key: str):
        base_url = base_url.rstrip('/')
        # Accept either the site root or the full API base -- whichever the
        # user pasted into users.yml.
        if not base_url.endswith(_API_SUFFIX):
            base_url += _API_SUFFIX
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def get_task_context(self, task_key: str) -> Optional[Dict[str, Any]]:
        """Fetch title/description/status for a task, normalised to the same
        shape JiraClient.get_issue_context returns.

        Returns None (and logs) on any failure -- a missing/inaccessible
        task should degrade to "no ticket context available", not break
        the review it's supporting.
        """
        url = f"{self.base_url}/tasks/{task_key}"
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            if response.status_code == 404:
                logger.info(f"Goby task {task_key} not found or not visible to this API key")
                return None
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.warning(f"Error fetching Goby task {task_key}: {e}")
            return None

        status = data.get("status") or {}
        labels = [label.get("name", "") for label in (data.get("labels") or []) if label.get("name")]
        return {
            "key": data.get("key") or task_key,
            "summary": data.get("title") or "",
            "description": data.get("description") or "",
            "status": status.get("name") or "",
            # Goby has no issue-type field; labels are the closest equivalent.
            "issue_type": ", ".join(labels),
            "source": "goby",
            "url": data.get("url") or "",
        }
