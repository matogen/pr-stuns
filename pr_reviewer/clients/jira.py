#!/usr/bin/env python3
"""
Minimal read-only Jira Cloud client for pulling ticket context into PR reviews.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Jira issue keys: an uppercase project key (letters/digits, starting with a
# letter) followed by "-" and a number, e.g. "ERA-256". Branch names are
# usually lowercase ("feature/era-256-notification-proxy"), so matching is
# case-insensitive and the key is upper-cased for the API call.
_ISSUE_KEY_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b')


def extract_issue_key(branch_name: Optional[str]) -> Optional[str]:
    """Pull a Jira issue key (e.g. "ERA-256") out of a branch name.

    Returns None if no plausible key is found. Branches like "deps-123" or
    "v2-1" match the same shape a real Jira key does -- that ambiguity is
    inherent to key-in-branch-name conventions, not something a regex can
    fully resolve. A wrong/nonexistent key just means get_issue_context()
    gets a 404 and the review proceeds without ticket context.
    """
    if not branch_name:
        return None
    match = _ISSUE_KEY_RE.search(branch_name)
    return match.group(1).upper() if match else None


class JiraClient:
    """Read-only Jira Cloud client for the fields a code review needs."""

    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip('/')
        self.auth = (email, api_token)

    def get_issue_context(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch summary/description/status for an issue.

        Returns None (and logs) on any failure -- a missing/inaccessible
        ticket should degrade to "no ticket context available", not break
        the review it's supporting.
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        params = {"fields": "summary,description,status,issuetype"}
        try:
            response = requests.get(url, auth=self.auth, params=params, timeout=15)
            if response.status_code == 404:
                logger.info(f"Jira issue {issue_key} not found (branch name may not reference a real ticket)")
                return None
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.warning(f"Error fetching Jira issue {issue_key}: {e}")
            return None

        fields = data.get("fields", {}) or {}
        return {
            "key": issue_key,
            "summary": fields.get("summary", "") or "",
            "description": _adf_to_text(fields.get("description")),
            "status": ((fields.get("status") or {}).get("name")) or "",
            "issue_type": ((fields.get("issuetype") or {}).get("name")) or "",
        }


def _adf_to_text(node: Any) -> str:
    """Flatten Jira's Atlassian Document Format (rich-text JSON) into plain text.

    Jira Cloud's API v3 returns issue descriptions as ADF, not a plain
    string. This walks the node tree and concatenates every "text" leaf --
    formatting fidelity doesn't matter for feeding a description into an
    LLM prompt, only the words.
    """
    if not node:
        return ""
    if isinstance(node, str):
        return node

    parts: List[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            for child in n.get("content", []) or []:
                walk(child)
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return " ".join(p for p in parts if p).strip()
