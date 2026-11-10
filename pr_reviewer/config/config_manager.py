#!/usr/bin/env python3
"""
Configuration manager for PR Review Bot.
Handles loading and validation of configuration from YAML files.
"""

import os
from typing import Dict, List, Any, Optional
import yaml

from pr_reviewer.config.users import UserStore


class ConfigManager:
    """Configuration manager for the PR Review Bot"""

    def __init__(self, config_path: str, users_path: Optional[str] = None):
        """Load configuration from YAML file

        Args:
            config_path: Path to the YAML configuration file
            users_path: Path to the users credential file. Defaults to
                ``users.yml`` next to ``config_path``.

        Raises:
            FileNotFoundError: If the configuration file doesn't exist
            ValueError: If the configuration is invalid
        """
        self.config_path = config_path
        self.config = self._load_config()

        if users_path is None:
            users_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "users.yml")
        self.users = UserStore(users_path)
        
    def _load_config(self) -> Dict[str, Any]:
        """Load and validate configuration
        
        Returns:
            Dict containing the validated configuration
            
        Raises:
            FileNotFoundError: If the configuration file doesn't exist
            ValueError: If required fields are missing
        """
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Validate required fields
        required_fields = ['repositories', 'llm']
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Missing required configuration field: {field}")
        
        return config
    
    def get_repositories(self) -> List[Dict[str, Any]]:
        """Get the list of configured repositories with credentials resolved.

        Inline secrets in ``config.yaml`` still take precedence; missing
        fields are filled in from ``users.yml`` based on the ``username``
        (Bitbucket) or ``user`` (GitHub) reference.

        Returns:
            List of repository configurations
        """
        return [self.users.resolve(repo) for repo in self.config['repositories']]
    
    def get_llm_config(self) -> Dict[str, Any]:
        """Get the LLM configuration
        
        Returns:
            Dict containing LLM configuration
        """
        return self.config['llm']
    
    def get_comment_template(self) -> str:
        """Get the comment template for PR remarks
        
        Returns:
            String template for formatting comments
        """
        return self.config.get('comment_template', 
                          "### 🤖 LLM Code Review\n\n{remarks}\n\n_This comment was generated automatically by a local LLM._")
    
    def get_jira_config(self, instance_name: str = "default") -> Dict[str, Any]:
        """Get a named Jira instance config (base_url/email/api_token).

        Different repos can be on different Jira sites -- see
        UserStore.get_jira_config for how `jira_instance:` on a repo entry
        selects which one. Returns an empty dict if not configured --
        callers should treat that as "ticket context unavailable", not an
        error.
        """
        return self.users.get_jira_config(instance_name)

    def get_goby_config(self, instance_name: str = "default") -> Dict[str, Any]:
        """Get a named Goby instance config (base_url/api_key).

        See UserStore.get_goby_config for how `goby_instance:` on a repo
        entry selects which one. Returns an empty dict if not configured --
        callers should treat that as "ticket context unavailable", not an
        error.
        """
        return self.users.get_goby_config(instance_name)

    def get_flow_diagram_config(self) -> Any:
        """Get the top-level `flow_diagram:` block (a mapping, a bool, or None).

        See pr_reviewer.core.flow_diagram.resolve_flow_diagram_config for the
        defaults and for how a repo entry overrides it.
        """
        return self.config.get('flow_diagram')

    def get_monitoring_config(self) -> Dict[str, Any]:
        """Get the monitoring configuration
        
        Returns:
            Dict containing monitoring configuration with defaults
        """
        monitoring_config = self.config.get('monitoring', {})
        
        # Set defaults if not provided
        if 'enabled' not in monitoring_config:
            monitoring_config['enabled'] = False
        
        if 'interval_minutes' not in monitoring_config:
            monitoring_config['interval_minutes'] = 30  # Default to checking every 30 minutes
        
        if 'reset_processed_hours' not in monitoring_config:
            monitoring_config['reset_processed_hours'] = 24  # Default to reset processed PRs every 24 hours
            
        if 'max_runs' not in monitoring_config:
            monitoring_config['max_runs'] = 0  # Default to unlimited runs (0 = no limit)
            
        return monitoring_config


def create_sample_config(output_path: str = "config_sample.yaml") -> None:
    """Create a sample configuration file
    
    Args:
        output_path: Path where the sample configuration should be saved
    """
    config = {
        "repositories": [
            {
                "type": "github",
                "owner": "username",
                "repo": "repository-name",
                "token": "your-github-token",
                "max_prs": 5,
                "pr_state": "open",
                "pr_sort": "created",
                "pr_direction": "desc"
            },
            {
                "type": "gitlab",
                "url": "https://gitlab.example.com",
                "project_id": "12345",
                "token": "your-gitlab-token",
                "max_prs": 3,
                "pr_state": "opened",
                "pr_sort": "created_at"
            }
        ],
        "llm": {
            "model_name": "claude-opus-5-5",
            "api_key": "your-anthropic-api-key",
            "max_tokens": 4096,
            "temperature": 0.3,
            "system_prompt": "You are an expert code reviewer. "
        },
        "monitoring": {
            "enabled": True,                # Enable continuous monitoring
            "interval_minutes": 30,         # Check for new PRs every 30 minutes
            "reset_processed_hours": 24,    # Reset processed PRs every 24 hours to recheck them
            "max_runs": 0                   # 0 = run indefinitely, N = run N times and exit
        },
        "comment_template": "### 🤖 AI Code Review\n\n{remarks}\n\n_This comment was generated automatically by an AI code review tool._"
    }
    
    with open(output_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)