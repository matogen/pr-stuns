"""Credential lookup for repository hosts.

Loads ``users.yml`` (a separate file from ``config.yaml``) and resolves
per-host credentials by user name. Repo entries in ``config.yaml`` only
need to reference a user; secrets live in one place.
"""

import os
from typing import Any, Dict, Optional

import yaml

from pr_reviewer.utils.logging import logger


class UserStore:
    """Reads ``users.yml`` and resolves credentials by user key."""

    def __init__(self, users_path: Optional[str]):
        self.users_path = users_path
        self._users: Dict[str, Dict[str, Any]] = {}
        self._jira: Dict[str, Any] = {}
        self._goby: Dict[str, Any] = {}
        self._loaded = False

        if users_path and os.path.exists(users_path):
            with open(users_path, "r") as f:
                data = yaml.safe_load(f) or {}
            self._users = data.get("users", {}) or {}
            self._jira = data.get("jira", {}) or {}
            self._goby = data.get("goby", {}) or {}
            self._loaded = True
            logger.info(f"Loaded {len(self._users)} user(s) from {users_path}")
        elif users_path:
            logger.info(
                f"users file {users_path} not found; falling back to inline credentials in config"
            )

    def get_jira_config(self, instance_name: str = "default") -> Dict[str, Any]:
        """Return the named Jira instance config (base_url/email/api_token).

        Jira instances are named rather than per-credential-owner like the
        git host lookups below, because different groups of repos can live
        on entirely different Jira sites (different org, different
        account). A repo opts into a specific instance via
        ``jira_instance:`` in config.yaml; repos that don't set it use
        "default". Returns an empty dict if the named instance isn't
        configured -- callers should treat that as "ticket context
        unavailable", not an error.
        """
        return self._jira.get(instance_name, {}) or {}

    def get_goby_config(self, instance_name: str = "default") -> Dict[str, Any]:
        """Return the named Goby instance config (base_url/api_key).

        Same shape as the Jira instances above, and named for the same
        reason with one extra twist: a Goby API key belongs to a single
        team, so "per project" means one named instance per Goby team
        whose repos get reviewed. A repo opts in via ``goby_instance:`` in
        config.yaml; repos that don't set it use "default". Returns an
        empty dict if the named instance isn't configured -- callers
        should treat that as "ticket context unavailable", not an error.
        """
        return self._goby.get(instance_name, {}) or {}

    def get(self, name: str, platform: str) -> Dict[str, Any]:
        """Return the credential dict for ``name`` on ``platform``.

        Returns an empty dict if the user or platform entry is missing.
        """
        if not name or not self._loaded:
            return {}
        user = self._users.get(name)
        if not user:
            logger.warning(f"User '{name}' not found in {self.users_path}")
            return {}
        return user.get(platform, {}) or {}

    def resolve_bitbucket(self, repo_config: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of ``repo_config`` with Bitbucket credentials filled in.

        Inline values in ``repo_config`` take precedence, so a half-migrated
        config still works. Also resolves entries inside ``fallback_users``.
        """
        resolved = dict(repo_config)
        creds = self.get(resolved.get("username", ""), "bitbucket")
        for field in ("api_token", "app_password", "email"):
            if not resolved.get(field) and creds.get(field):
                resolved[field] = creds[field]

        fallback_users = resolved.get("fallback_users") or []
        resolved_fallbacks = []
        for entry in fallback_users:
            entry_resolved = dict(entry)
            entry_creds = self.get(entry_resolved.get("username", ""), "bitbucket")
            for field in ("api_token", "app_password", "email"):
                if not entry_resolved.get(field) and entry_creds.get(field):
                    entry_resolved[field] = entry_creds[field]
            resolved_fallbacks.append(entry_resolved)
        if resolved_fallbacks:
            resolved["fallback_users"] = resolved_fallbacks

        return resolved

    def resolve_github(self, repo_config: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of ``repo_config`` with the GitHub token filled in."""
        resolved = dict(repo_config)
        if not resolved.get("token"):
            creds = self.get(resolved.get("user", ""), "github")
            if creds.get("token"):
                resolved["token"] = creds["token"]
        return resolved

    def resolve(self, repo_config: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch to the right per-platform resolver."""
        repo_type = (repo_config.get("type") or "github").lower()
        if repo_type == "bitbucket":
            return self.resolve_bitbucket(repo_config)
        if repo_type == "github":
            return self.resolve_github(repo_config)
        return dict(repo_config)
